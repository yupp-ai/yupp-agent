"""DB resolution helpers shared across service submodules."""

import os
import re
import uuid

from sqlalchemy import func, text
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.agent_harness_service.common.config import (
    AgentConfig,
    load_agent_config,
    load_agent_config_from_db,
)
from ypl.agent_harness_service.common.types import AttachmentInfo
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
)
from ypl.structured_logger import get_logger

logger = get_logger()


async def _resolve_user_name_from_db(user_id: str) -> str | None:
    """Look up a user's name from the users table."""
    async with get_async_session() as session:
        result = await session.execute(text("SELECT name FROM users WHERE user_id = :uid"), {"uid": user_id})
        row = result.first()
        return str(row[0]) if row and row[0] else None


async def _resolve_personal_agent_for_user(base_agent_name: str, user_id: str) -> tuple[str | None, str | None]:
    """Resolve a personal agent name and display name for a user.

    Returns:
        Tuple of (personal_agent_name, display_name) if the user has a personal
        agent, or (None, None) to fall back to the base agent.
    """
    async with get_async_session() as session:
        # Look up user email
        result = await session.execute(text("SELECT email, name FROM users WHERE user_id = :uid"), {"uid": user_id})
        row = result.first()
        if not row or not row[0]:
            logger.warning(
                "Could not resolve email for personal agent",
                user_id=user_id,
                base_agent_name=base_agent_name,
            )
            return None, None

        email = str(row[0])
        user_name = str(row[1]) if row[1] else None

        username = re.sub(r"[^a-z0-9-]", "-", email.split("@")[0].lower()).strip("-")
        personal_agent_name = f"{base_agent_name}-{username}"

        # Check if the personal agent exists in DB
        check = await session.execute(text("SELECT 1 FROM agents WHERE name = :name"), {"name": personal_agent_name})
        if not check.first():
            logger.info(
                "No personal agent found, using base agent",
                base_agent_name=base_agent_name,
                personal_agent_name=personal_agent_name,
                user_id=user_id,
            )
            return None, None

    # Derive display name: "Alice's yClaw"
    first_name = user_name.split()[0] if user_name else username.capitalize()
    display_name = f"{first_name}\u2019s yClaw"

    logger.info(
        "Resolved personal agent",
        base_agent_name=base_agent_name,
        personal_agent_name=personal_agent_name,
        display_name=display_name,
        user_id=user_id,
    )
    return personal_agent_name, display_name


async def _resolve_agent(session: AsyncSession, agent_name: str) -> Agent | None:
    """Look up an agent by name."""
    result = await session.exec(select(Agent).where(Agent.name == agent_name))
    return result.one_or_none()


async def _load_agent_config_with_db_fallback(agent_name: str) -> AgentConfig | None:
    """Load agent config from disk, falling back to DB for DB-only agents (e.g. personal agents).

    This is the primary way to obtain an AgentConfig at runtime. Disk-based agents
    (with config.json on disk) are loaded first; if not found, the Agent DB record's
    config JSONB is used instead.
    """
    cfg = load_agent_config(agent_name)
    if cfg:
        return cfg
    try:
        async with get_async_session() as session:
            db_agent = await _resolve_agent(session, agent_name)
            if db_agent and db_agent.config:
                return load_agent_config_from_db(db_agent)
    except Exception:
        logger.error("Failed to load agent config from DB", name=agent_name, exc_info=True)
    return None


async def _resolve_session(session: AsyncSession, session_id: str) -> AgentSession | None:
    """Look up a session by UUID or slack_session_id."""
    # Try UUID first
    try:
        session_uuid = uuid.UUID(session_id)
        result = await session.exec(select(AgentSession).where(AgentSession.agent_session_id == session_uuid))
        agent_session = result.one_or_none()
        if agent_session:
            return agent_session
    except ValueError:
        pass

    # Try slack_session_id
    result = await session.exec(select(AgentSession).where(AgentSession.slack_session_id == session_id))
    return result.one_or_none()


async def _next_turn_number(session: AsyncSession, agent_session_id: uuid.UUID) -> int:
    """Get the next turn number for a session."""
    result = await session.exec(
        select(func.coalesce(func.max(AgentSessionMessage.turn_number), 0)).where(
            AgentSessionMessage.agent_session_id == agent_session_id
        )
    )
    current_max = result.one()
    return current_max + 1


async def _has_inflight_turn(session: AsyncSession, agent_session_id: uuid.UUID) -> bool:
    """Check if there's an inbound turn (USER or FELLOW_AGENT) with no completed response yet.

    Must be called under FOR UPDATE lock on the session row.

    FELLOW_AGENT turns originate from A2A messaging (trigger=AGENT) and must be treated
    as inflight sentinels on equal footing with USER turns so that:
    - Concurrent AGENT messages are serialized rather than spawning parallel tasks.
    - stop_session() correctly detects and cancels inflight A2A-triggered turns.
    """
    # Find the latest turn that has an inbound (USER or FELLOW_AGENT) message
    latest_user_turn = await session.exec(
        select(func.max(AgentSessionMessage.turn_number)).where(
            AgentSessionMessage.agent_session_id == agent_session_id,
            col(AgentSessionMessage.role).in_([AgentSessionMessageRole.USER, AgentSessionMessageRole.FELLOW_AGENT]),
        )
    )
    max_user_turn: int | None = latest_user_turn.one()
    if max_user_turn is None:
        return False

    # Check if that turn has a completed response.  Exclude both inbound roles
    # (USER and FELLOW_AGENT) — a FELLOW_AGENT message is the *request*, not the
    # response, and its default completion_status (SUCCESS) would otherwise make
    # it count as a completed response the instant it is committed, causing
    # _has_inflight_turn to always return False for A2A-triggered turns.
    # AGENT eager-persist draft rows carry IN_PROGRESS and are excluded via the
    # completion_status filter; SYSTEM messages are always terminal.
    response_result = await session.exec(
        select(func.count()).where(
            AgentSessionMessage.agent_session_id == agent_session_id,
            AgentSessionMessage.turn_number == max_user_turn,
            col(AgentSessionMessage.role).not_in([AgentSessionMessageRole.USER, AgentSessionMessageRole.FELLOW_AGENT]),
            col(AgentSessionMessage.completion_status) != AgentSessionMessageCompletionStatus.IN_PROGRESS,
        )
    )
    response_count = response_result.one()
    return response_count == 0


async def _download_attachments_to_workspace(
    attachments: list[AttachmentInfo],
    workspace: str,
) -> list[str]:
    """Download attachments from the blob store into the session workspace.

    Files are saved to ``{workspace}/attachments/{filename}``. The source
    path is the logical ``AttachmentInfo.blob_path`` (e.g.,
    ``attachments/{session_id}/file.png``); the configured ``BlobStore``
    resolves it against local storage or GCS.

    Args:
        attachments: Attachment metadata with logical blob paths.
        workspace: Session workspace root directory.

    Returns:
        List of absolute paths (e.g., ``/data/sessions/{id}/attachments/screenshot.png``) for
        successfully downloaded files. Absolute paths are used so the agent can pass them
        directly to the Read tool without guessing the workspace root.
    """
    from ypl.backend.utils.blob_store import get_blob_store

    store = get_blob_store()
    attachments_dir = os.path.join(workspace, "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    downloaded_paths: list[str] = []
    for att in attachments:
        # Sanitize: use only the basename and reject path separators / ".." / "."
        safe_name = os.path.basename(att.filename)
        if not safe_name or safe_name in ("..", "."):
            logger.warning("Skipping attachment with unsafe filename", filename=att.filename)
            continue
        local_path = os.path.join(attachments_dir, safe_name)

        try:
            data = await store.download(att.blob_path)
            with open(local_path, "wb") as f:
                f.write(data)
            downloaded_paths.append(local_path)
            logger.info(
                "Downloaded attachment to workspace",
                filename=att.filename,
                size=len(data),
                workspace=workspace,
            )
        except Exception:
            logger.error(
                "Failed to download attachment from blob store",
                filename=att.filename,
                blob_path=att.blob_path,
                exc_info=True,
            )

    return downloaded_paths


def _prepend_attachment_paths(message: str, paths: list[str]) -> str:
    """Prepend attachment file paths to the message text.

    The paths are listed so the agent knows which files are available
    in its workspace and can read them with the `read` tool.
    """
    if not paths:
        return message
    paths_str = ", ".join(paths)
    return f"[Attached files: {paths_str}]\n\n{message}"


async def _mark_session_completed(
    session: AsyncSession,
    agent_session_id: uuid.UUID,
) -> None:
    """Mark a session COMPLETED regardless of trigger type.

    All triggers (SLACK, CRON, API, TASK, WEBHOOK) transition to COMPLETED after
    each successful turn.  SLACK sessions are multi-turn: when a follow-up message
    arrives, send_message() re-activates the session to ACTIVE before the new turn
    starts, so the status correctly reflects in-progress vs idle state.
    """
    s = await session.get(AgentSession, agent_session_id)
    if s:
        s.status = AgentSessionStatus.COMPLETED
