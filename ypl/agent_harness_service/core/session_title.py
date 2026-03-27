"""Auto-generate short titles for agent sessions using LLM."""

import os
import uuid

import anthropic
from sqlmodel import col, select

from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageRole,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# Title is generated on turn 1 and refreshed on turn 4.
TITLE_GENERATE_TURNS = {1, 4}

_TITLE_MODEL = "claude-haiku-4-5-20251001"

_TITLE_PROMPT = (
    "Generate a very short title (max 8 words) for the conversation below. "
    "The input contains only user messages sent to an AI coding assistant. "
    "Capture the user's key intention and the main topic of their questions. "
    "Use proper title case (e.g. 'Fix Login Bug in Auth Service'). "
    "If the messages are empty or lack enough information to generate a meaningful title, "
    "output exactly [NO TITLE]. Never ask questions or request clarification. "
    "Output ONLY the title, no quotes, no punctuation at the end.\n\n"
    "User messages:\n{messages}"
)

_TRIGGER_PREFIX: dict[str, str] = {
    AgentSessionTrigger.CRON.value: "[CRON] ",
    AgentSessionTrigger.TASK.value: "[TASK] ",
}


async def _generate_title_text(user_messages: list[str]) -> str | None:
    """Call Haiku to produce a short title from user messages."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping title generation")
        return None

    combined = "\n---\n".join(msg[:500] for msg in user_messages[:5])
    prompt = _TITLE_PROMPT.format(messages=combined)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=_TITLE_MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    first_block = response.content[0] if response.content else None
    if not first_block or not hasattr(first_block, "text"):
        return None
    title = first_block.text.strip()
    if not title or title == "[NO TITLE]":
        return None
    return title


async def maybe_generate_session_title(
    agent_session_id: uuid.UUID,
    turn_number: int,
    trigger: AgentSessionTrigger | None = None,
) -> None:
    """Generate or refresh the session title if this is the right turn.

    Called after a user message is stored. Generates on turn 1, refreshes on turn 4.
    Runs as fire-and-forget so it never blocks the agent turn.
    """
    if turn_number not in TITLE_GENERATE_TURNS:
        return

    try:
        async with get_async_session() as session:
            # Fetch user messages for this session (up to first 5)
            result = await session.exec(
                select(AgentSessionMessage.content)
                .where(
                    AgentSessionMessage.agent_session_id == agent_session_id,
                    col(AgentSessionMessage.role) == AgentSessionMessageRole.USER,
                    AgentSessionMessage.content.isnot(None),  # type: ignore[union-attr]
                )
                .order_by(col(AgentSessionMessage.turn_number))
                .limit(5)
            )
            user_messages = [row for row in result.all() if row]

        if not user_messages:
            return

        title = await _generate_title_text(user_messages)
        if not title:
            return

        # Add trigger prefix
        if trigger:
            prefix = _TRIGGER_PREFIX.get(trigger.value, "")
            if prefix:
                title = prefix + title

        # Persist
        async with get_async_session() as session:
            agent_session = await session.get(AgentSession, agent_session_id)
            if agent_session:
                agent_session.title = title
                await session.commit()
                logger.info(
                    "Session title updated",
                    session_id=str(agent_session_id),
                    title=title,
                    turn_number=turn_number,
                )

    except Exception:
        logger.warning(
            "Failed to generate session title",
            session_id=str(agent_session_id),
            turn_number=turn_number,
            exc_info=True,
        )
