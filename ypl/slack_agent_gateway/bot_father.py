"""Bot Father — core orchestration for automated Slack bot creation.

Handles the full lifecycle of creating a Slack bot for an agent:
  request → approval → app creation → secret storage → Redis settings update.

Redis keys:
  slack_agent_gw:bot_creation:{request_id}         — BotCreationRecord (7-day TTL)
  slack_agent_gw:bot_creation_by_agent:{agent_name} — dedup key pointing to request_id (7-day TTL)
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlmodel import select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session, retry_db
from ypl.backend.llm.db_helpers import get_user_email, get_user_id_by_email
from ypl.backend.llm.yuppster_helpers import slack_id_to_yupp_user_id
from ypl.backend.utils.soul_utils import has_permission_cached
from ypl.db.redis import get_redis_client
from ypl.db.slack_agent import SlackAgent, SlackAgentStatus
from ypl.db.soul_rbac import SoulPermission
from ypl.slack_agent_gateway.bot_father_types import (
    BotApprovalStatus,
    BotCreationRecord,
    BotCreationRequest,
    BotCreationResponse,
    BotStatusResponse,
)
from ypl.slack_agent_gateway.constants import get_agent_config_by_name, get_bot_father_config
from ypl.slack_agent_gateway.crypto import decrypt_secret, encrypt_secret
from ypl.slack_agent_gateway.secrets import create_agent_secret
from ypl.slack_agent_gateway.slack_app_manifests import (
    add_app_collaborator,
    build_manifest,
    build_oauth_install_url,
    create_slack_app,
    delete_slack_app,
    exchange_oauth_code,
)
from ypl.slack_agent_gateway.token_storage import get_bot_father_refresh_token
from ypl.structured_logger import get_logger

logger = get_logger()

# Redis TTL for bot creation records: 7 days
_BOT_CREATION_TTL_SECONDS = 7 * 24 * 60 * 60

# Redis key prefixes
_REDIS_KEY_BOT_CREATION = "slack_agent_gw:bot_creation"
_REDIS_KEY_BOT_CREATION_BY_AGENT = "slack_agent_gw:bot_creation_by_agent"

# Slack user ID pattern: starts with U or W, followed by alphanumeric (typically 8-11 chars total)
_SLACK_USER_ID_PATTERN = re.compile(r"^[UW][A-Z0-9]{8,}$", re.IGNORECASE)


def _is_slack_user_id(value: str) -> bool:
    """Check if a value looks like a Slack user ID (e.g., 'U123ABC456')."""
    return bool(_SLACK_USER_ID_PATTERN.match(value))


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


async def _save_record(record: BotCreationRecord) -> None:
    """Persist a BotCreationRecord to Redis with 7-day TTL.

    Encrypts oauth_client_secret before storage for security.
    """
    redis = await get_redis_client()
    key = f"{_REDIS_KEY_BOT_CREATION}:{record.request_id}"

    # Encrypt oauth_client_secret if present
    data = record.model_dump()
    if data.get("oauth_client_secret"):
        data["oauth_client_secret"] = encrypt_secret(data["oauth_client_secret"])

    await redis.set(key, BotCreationRecord.model_validate(data).model_dump_json(), ex=_BOT_CREATION_TTL_SECONDS)


async def _load_record(request_id: str) -> BotCreationRecord | None:
    """Load a BotCreationRecord from Redis.

    Decrypts oauth_client_secret after loading.
    """
    redis = await get_redis_client()
    key = f"{_REDIS_KEY_BOT_CREATION}:{request_id}"
    raw_data: str | None = await redis.get(key)
    if not raw_data:
        return None

    record = BotCreationRecord.model_validate_json(raw_data)

    # Decrypt oauth_client_secret if present
    if record.oauth_client_secret:
        decrypted = decrypt_secret(record.oauth_client_secret)
        if decrypted:
            record.oauth_client_secret = decrypted
        else:
            # Decryption failed - secret may be corrupted or from old unencrypted format
            logger.warning("Failed to decrypt oauth_client_secret", request_id=request_id)
            record.oauth_client_secret = None

    return record


async def _load_record_by_agent(agent_name: str) -> BotCreationRecord | None:
    """Load the most-recent BotCreationRecord for an agent (via dedup key)."""
    redis = await get_redis_client()
    dedup_key = f"{_REDIS_KEY_BOT_CREATION_BY_AGENT}:{agent_name.lower()}"
    request_id: str | None = await redis.get(dedup_key)
    if not request_id:
        return None
    return await _load_record(request_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def request_bot_creation(request: BotCreationRequest) -> BotCreationResponse:
    """Validate and store a new bot creation request, then post for approval.

    Rejects if:
    - The agent already has a live Slack bot in dynamic settings.
    - There is already a PENDING request for this agent.

    Args:
        request: Incoming BotCreationRequest from the war room.

    Returns:
        BotCreationResponse with request_id and PENDING status.

    Raises:
        ValueError: If a duplicate/active bot already exists.
        PermissionError: If the requester lacks CREATE_AGENT permission.
    """
    agent_name = request.agent_name.lower()

    # Permission check: requester must have CREATE_AGENT soul permission
    # Resolve email from either direct email or via Slack user ID -> Yupp user ID -> email
    requester_email = request.requested_by_email
    requester_user_id: str | None = None

    if request.requested_by:
        if _is_slack_user_id(request.requested_by):
            # Slack user ID -> resolve to Yupp user ID -> get email
            requester_user_id = await slack_id_to_yupp_user_id(request.requested_by)
            if requester_user_id and not requester_email:
                requester_email = await get_user_email(requester_user_id)
        else:
            # Assume it's a Yupp user ID
            requester_user_id = request.requested_by
            if not requester_email:
                requester_email = await get_user_email(requester_user_id)

    # Permission check: requester must have USE_MCP soul permission
    if requester_email:
        if not await has_permission_cached(requester_email, SoulPermission.CREATE_AGENT):
            raise PermissionError(f"User {requester_email} lacks CREATE_AGENT permission to create bots.")
    else:
        # Fail closed: deny if we cannot verify the requester's identity
        raise PermissionError("Cannot verify permissions: unable to resolve requester email.")

    # Check if agent already has a live bot in the database
    async with get_async_session() as session:
        stmt = select(SlackAgent).where(
            SlackAgent.agent_name == agent_name,
            SlackAgent.status == SlackAgentStatus.ACTIVE,
            SlackAgent.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        result = await session.exec(stmt)
        existing_agent = result.first()
        if existing_agent:
            raise ValueError(f"Agent '{agent_name}' already has a live Slack bot.")

    # Create the record first, then atomically claim the dedup key with SET NX
    redis = await get_redis_client()
    dedup_key = f"{_REDIS_KEY_BOT_CREATION_BY_AGENT}:{agent_name}"
    request_id = str(uuid.uuid4())
    record = BotCreationRecord(
        request_id=request_id,
        agent_name=agent_name,
        slack_name=request.slack_name.lower(),
        display_name=request.display_name,
        requested_by=request.requested_by,
        requested_by_email=requester_email,
        requested_by_user_id=requester_user_id,
        description=request.description,
        system_prompt=request.system_prompt,
        status=BotApprovalStatus.PENDING,
    )

    # Atomically claim the dedup key — prevents race condition from concurrent requests
    claimed = await redis.set(dedup_key, request_id, ex=_BOT_CREATION_TTL_SECONDS, nx=True)
    if not claimed:
        # Another request won the race — check if it's still in-flight
        existing_request_id: str | None = await redis.get(dedup_key)
        if existing_request_id:
            existing = await _load_record(existing_request_id)
            # Block if request is in any non-terminal state (PENDING, APPROVED, AWAITING_INSTALLATION)
            if existing and existing.status in (
                BotApprovalStatus.PENDING,
                BotApprovalStatus.APPROVED,
                BotApprovalStatus.AWAITING_INSTALLATION,
            ):
                raise ValueError(
                    f"A bot creation request is already in progress for agent '{agent_name}' "
                    f"(status={existing.status}, request_id={existing_request_id})."
                )
        # Terminal state (COMPLETED, DENIED, FAILED) — delete stale dedup key and reclaim
        await redis.delete(dedup_key)
        claimed = await redis.set(dedup_key, request_id, ex=_BOT_CREATION_TTL_SECONDS, nx=True)
        if not claimed:
            raise ValueError(f"Concurrent bot creation in progress for agent '{agent_name}'.")

    # Store the record
    await _save_record(record)

    logger.info(
        "Bot creation request stored",
        request_id=request_id,
        agent_name=agent_name,
        slack_name=request.slack_name,
    )

    # Post approval message to Slack
    try:
        config = get_bot_father_config()
        bot_father_token = config["bot_token"]
        approval_channel = config["approval_channel"]

        if bot_father_token and approval_channel:
            blocks = build_approval_blocks(record)
            client = AsyncWebClient(token=bot_father_token)
            resp = await client.chat_postMessage(
                channel=approval_channel,
                text=f"Bot creation request for agent '{agent_name}' (request_id={request_id})",
                blocks=blocks,
            )
            # Store the message ts for later updates
            record.approval_message_ts = resp.get("ts")
            record.approval_message_channel = approval_channel
            await _save_record(record)
            logger.info(
                "Approval message posted",
                request_id=request_id,
                channel=approval_channel,
                message_ts=record.approval_message_ts,
            )
        else:
            logger.warning(
                "Bot Father bot token or approval channel not configured, skipping approval message",
                request_id=request_id,
            )
    except SlackApiError as e:
        logger.error(
            "Failed to post approval message to Slack",
            request_id=request_id,
            error=str(e),
        )

    return BotCreationResponse(
        request_id=request_id,
        status=BotApprovalStatus.PENDING,
        message=f"Bot creation request submitted (request_id={request_id}). Awaiting approval.",
    )


async def handle_approval(request_id: str, approver_slack_user_id: str) -> None:
    """Handle an approval button click from Slack.

    Uses SET NX for claim deduplication so double-clicks are safe.

    Args:
        request_id: The bot creation request ID.
        approver_slack_user_id: Slack user ID of the approver.

    Raises:
        ValueError: If the request is not found or not in PENDING status.
    """
    # Claim the approval atomically (SET NX) — prevents double-processing
    redis = await get_redis_client()
    claim_key = f"{_REDIS_KEY_BOT_CREATION}:{request_id}:approval_claim"
    claimed = await redis.set(claim_key, approver_slack_user_id, ex=300, nx=True)
    if not claimed:
        logger.info("Approval already claimed, ignoring duplicate", request_id=request_id)
        return

    try:
        record = await _load_record(request_id)
        if not record:
            raise ValueError(f"Bot creation request not found: {request_id}")
        if record.status != BotApprovalStatus.PENDING:
            raise ValueError(f"Bot creation request {request_id} is not PENDING (status={record.status})")

        # Transition to APPROVED
        now = datetime.now(UTC)
        record.status = BotApprovalStatus.APPROVED
        record.approver_slack_user_id = approver_slack_user_id
        record.approved_at = now
        record.updated_at = now
        await _save_record(record)

        logger.info("Bot creation request approved", request_id=request_id, approver=approver_slack_user_id)

        # Proceed to create the bot
        await create_slack_bot(record)
    except Exception:
        # Release the claim on failure so retries can proceed
        await redis.delete(claim_key)
        raise


async def handle_denial(request_id: str, approver_slack_user_id: str) -> None:
    """Handle a denial button click from Slack.

    Uses SET NX for claim deduplication so double-clicks are safe.

    Args:
        request_id: The bot creation request ID.
        approver_slack_user_id: Slack user ID of the denier.

    Raises:
        ValueError: If the request is not found or not in PENDING status.
    """
    # Claim the denial atomically (SET NX) — prevents double-processing
    redis = await get_redis_client()
    claim_key = f"{_REDIS_KEY_BOT_CREATION}:{request_id}:denial_claim"
    claimed = await redis.set(claim_key, approver_slack_user_id, ex=300, nx=True)
    if not claimed:
        logger.info("Denial already claimed, ignoring duplicate", request_id=request_id)
        return

    record = await _load_record(request_id)
    if not record:
        raise ValueError(f"Bot creation request not found: {request_id}")
    if record.status != BotApprovalStatus.PENDING:
        # Already processed (approved/denied/etc.) — silently ignore rather than raise
        logger.info("Request not pending, ignoring denial", request_id=request_id, status=record.status)
        return

    now = datetime.now(UTC)
    record.status = BotApprovalStatus.DENIED
    record.approver_slack_user_id = approver_slack_user_id
    record.updated_at = now
    await _save_record(record)

    logger.info("Bot creation request denied", request_id=request_id, denier=approver_slack_user_id)

    # Update the Slack approval message
    await _update_approval_message(record, text=f"❌ Denied by <@{approver_slack_user_id}>")


async def create_slack_bot(record: BotCreationRecord) -> None:
    """Phase 1: Create the Slack app and generate an OAuth install URL.

    Bot tokens cannot be obtained programmatically after creating an app via the
    manifest API (see https://github.com/slackapi/python-slack-sdk/issues/1192).
    An admin must complete the OAuth flow by clicking the install URL.

    Steps:
    1. Build and submit app manifest (creates Slack app).
    2. Store signing_secret in GCP Secret Manager.
    3. Generate OAuth install URL.
    4. Post the install URL to the approval channel.
    5. Mark status as AWAITING_INSTALLATION.

    Phase 2 (complete_oauth_installation) is triggered by the OAuth callback.

    Args:
        record: The approved BotCreationRecord.
    """
    refresh_token = await get_bot_father_refresh_token()
    environment = settings.ENVIRONMENT

    app_id: str | None = None

    try:
        # Step 1: Create Slack app via Manifests API
        manifest = build_manifest(record.slack_name, record.display_name, environment)
        creation_result = await create_slack_app(manifest, refresh_token)
        app_id = creation_result["app_id"]
        credentials: dict[str, str] = creation_result["credentials"]
        signing_secret = credentials.get("signing_secret", "")
        oauth_client_id = credentials.get("client_id", "")
        oauth_client_secret = credentials.get("client_secret", "")

        if not oauth_client_id or not oauth_client_secret:
            raise RuntimeError("Slack app created but missing OAuth credentials")

        logger.info("Slack app created", app_id=app_id, agent_name=record.agent_name)

        # Step 2: Add requester as app collaborator (owner)
        if record.requested_by_email:
            try:
                await add_app_collaborator(
                    app_id=app_id,
                    user_email=record.requested_by_email,
                    permission_type="owner",
                    refresh_token=refresh_token,
                )
                logger.info(
                    "Added requester as app collaborator",
                    app_id=app_id,
                    user_email=record.requested_by_email,
                )
            except Exception as collab_err:
                # Log but don't fail the entire flow - collaborator access is nice-to-have
                logger.warning(
                    "Failed to add requester as app collaborator",
                    app_id=app_id,
                    user_email=record.requested_by_email,
                    error=str(collab_err),
                    exc_info=True,
                )
        else:
            logger.warning(
                "No requester email available, skipping collaborator addition",
                request_id=record.request_id,
                agent_name=record.agent_name,
            )

        # Step 3: Store signing_secret in GCP Secret Manager (bot_token added after OAuth)
        await create_agent_secret(record.slack_name, "signing-secret", signing_secret, environment)
        await create_agent_secret(record.slack_name, "app-id", app_id, environment)
        logger.info("Stored signing secret in GCP", slack_name=record.slack_name)

        # Step 4: Generate OAuth install URL with request_id as state for CSRF
        oauth_install_url = build_oauth_install_url(oauth_client_id, environment, state=record.request_id)

        # Step 5: Update record with OAuth credentials
        now = datetime.now(UTC)
        record.slack_app_id = app_id
        record.oauth_client_id = oauth_client_id
        record.oauth_client_secret = oauth_client_secret  # Stored temporarily for OAuth exchange
        record.oauth_install_url = oauth_install_url
        record.status = BotApprovalStatus.AWAITING_INSTALLATION
        record.updated_at = now
        await _save_record(record)

        logger.info(
            "Slack app created, awaiting OAuth installation",
            request_id=record.request_id,
            agent_name=record.agent_name,
            app_id=app_id,
        )

        # Step 6: Post install URL to the approval channel
        await _update_approval_message(
            record,
            text=(
                f"✅ Slack app `{record.slack_name}` created! App ID: `{app_id}`\n\n"
                f"*Action Required:* Click the link below to complete installation:\n"
                f"<{oauth_install_url}|Install {record.display_name} to Slack>"
            ),
        )

    except Exception as e:
        logger.error(
            "Bot creation failed",
            request_id=record.request_id,
            agent_name=record.agent_name,
            app_id=app_id,
            error=str(e),
            exc_info=True,
        )

        # Best-effort cleanup: delete the Slack app if we created it
        if app_id:
            try:
                await delete_slack_app(app_id, refresh_token)
                logger.info("Cleaned up Slack app after failure", app_id=app_id)
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to clean up Slack app after creation failure",
                    app_id=app_id,
                    error=str(cleanup_err),
                )

        now = datetime.now(UTC)
        record.status = BotApprovalStatus.FAILED
        record.error = str(e)
        record.updated_at = now
        await _save_record(record)

        await _update_approval_message(
            record,
            text="❌ Bot creation failed. Check logs for details.",
        )


async def complete_oauth_installation(code: str, state: str) -> BotCreationRecord:
    """Phase 2: Complete the OAuth flow after admin clicks the install URL.

    Called by the OAuth callback route. Exchanges the auth code for a bot token,
    stores it in GCP secrets, and adds the agent to the database.

    Args:
        code: Authorization code from Slack OAuth redirect.
        state: State parameter (request_id) for CSRF protection.

    Returns:
        Updated BotCreationRecord with COMPLETED status.

    Raises:
        ValueError: If the request is not found or not in AWAITING_INSTALLATION status.
        RuntimeError: If the OAuth exchange fails.
    """
    request_id = state

    # Claim the OAuth completion atomically (SET NX) — prevents double-processing from retries
    redis = await get_redis_client()
    claim_key = f"{_REDIS_KEY_BOT_CREATION}:{request_id}:oauth_claim"
    claimed = await redis.set(claim_key, "completing", ex=300, nx=True)
    if not claimed:
        logger.info("OAuth completion already claimed, ignoring duplicate", request_id=request_id)
        # Only return if the first attempt completed successfully
        record = await _load_record(request_id)
        if record:
            if record.status == BotApprovalStatus.COMPLETED:
                return record
            # First attempt is still in progress or failed — don't return success
            raise ValueError(
                f"OAuth completion already in progress or failed for {request_id} (status={record.status})"
            )
        raise ValueError(f"Bot creation request not found: {request_id}")

    record = await _load_record(request_id)
    if not record:
        await redis.delete(claim_key)
        raise ValueError(f"Bot creation request not found: {request_id}")
    if record.status != BotApprovalStatus.AWAITING_INSTALLATION:
        await redis.delete(claim_key)
        raise ValueError(f"Bot creation request {request_id} is not AWAITING_INSTALLATION (status={record.status})")
    if not record.oauth_client_id or not record.oauth_client_secret:
        await redis.delete(claim_key)
        raise ValueError(f"Bot creation request {request_id} missing OAuth credentials")

    environment = settings.ENVIRONMENT

    try:
        # Step 1: Exchange code for bot token
        oauth_result = await exchange_oauth_code(
            code=code,
            client_id=record.oauth_client_id,
            client_secret=record.oauth_client_secret,
            environment=environment,
        )
        bot_token = oauth_result["access_token"]

        logger.info(
            "OAuth code exchanged for bot token",
            request_id=request_id,
            app_id=record.slack_app_id,
        )

        # Step 2: Store bot token in GCP Secret Manager
        await create_agent_secret(record.slack_name, "bot-token", bot_token, environment)
        logger.info("Stored bot token in GCP", slack_name=record.slack_name)

        # Step 3: Add agent to the database
        # Use the pre-resolved user ID from the record, with fallback for old records or email-only requests
        created_by_user_id = record.requested_by_user_id
        if not created_by_user_id and record.requested_by_email:
            created_by_user_id = await get_user_id_by_email(record.requested_by_email)
        await _add_agent_to_database(
            app_id=record.slack_app_id or "",
            agent_name=record.agent_name,
            bot_name=record.slack_name,
            display_name=record.display_name,
            created_by_user_id=created_by_user_id,
            bot_creation_request_id=record.request_id,
        )

        # Step 4: Create AHS agent
        ahs_created = await _create_ahs_agent(record, created_by_user_id)
        if not ahs_created:
            logger.warning(
                "AHS agent creation failed, Slack bot will work but agent config may need manual setup",
                agent_name=record.agent_name,
                request_id=record.request_id,
            )

        # Step 5: Mark COMPLETED and clear sensitive data
        now = datetime.now(UTC)
        record.status = BotApprovalStatus.COMPLETED
        record.completed_at = now
        record.updated_at = now
        record.oauth_client_secret = None  # Clear sensitive data
        await _save_record(record)

        logger.info(
            "Bot creation completed via OAuth",
            request_id=record.request_id,
            agent_name=record.agent_name,
            app_id=record.slack_app_id,
        )

        # Step 6: Update Slack approval message
        await _update_approval_message(
            record,
            text=(
                f"✅ Bot `{record.slack_name}` is now fully installed and ready! App ID: `{record.slack_app_id}`"
                f"\n\n📷 *To set a custom icon*, visit: "
                f"<https://api.slack.com/apps/{record.slack_app_id}/general|App Settings>"
            ),
        )

        return record

    except Exception as e:
        logger.error(
            "OAuth installation failed",
            request_id=request_id,
            agent_name=record.agent_name,
            app_id=record.slack_app_id,
            error=str(e),
            exc_info=True,
        )

        now = datetime.now(UTC)
        record.status = BotApprovalStatus.FAILED
        record.error = f"OAuth installation failed: {e}"
        record.updated_at = now
        record.oauth_client_secret = None  # Clear sensitive data on failure
        await _save_record(record)

        await _update_approval_message(
            record,
            text="❌ OAuth installation failed. Check logs for details.",
        )

        raise


async def handle_oauth_cancellation(state: str, error: str) -> None:
    """Handle OAuth cancellation or denial by the user.

    Updates the record to FAILED status and clears sensitive data.

    Args:
        state: State parameter (request_id) from the OAuth callback.
        error: Error code from Slack (e.g., 'access_denied').
    """
    if not state:
        logger.warning("OAuth cancellation without state parameter", error=error)
        return

    request_id = state
    record = await _load_record(request_id)
    if not record:
        logger.warning("OAuth cancellation for unknown request", request_id=request_id, error=error)
        return

    if record.status not in (BotApprovalStatus.AWAITING_INSTALLATION, BotApprovalStatus.APPROVED):
        logger.info(
            "OAuth cancellation for request not awaiting installation",
            request_id=request_id,
            status=record.status,
            error=error,
        )
        return

    # Clean up orphaned Slack app if it was created
    if record.slack_app_id:
        try:
            refresh_token = await get_bot_father_refresh_token()
            await delete_slack_app(record.slack_app_id, refresh_token)
            logger.info(
                "Cleaned up orphaned Slack app after OAuth denial",
                app_id=record.slack_app_id,
                request_id=request_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to clean up orphaned Slack app after OAuth denial",
                app_id=record.slack_app_id,
                request_id=request_id,
                error=str(e),
            )

    # Update record to FAILED
    now = datetime.now(UTC)
    record.status = BotApprovalStatus.FAILED
    record.error = f"OAuth denied: {error}"
    record.updated_at = now
    record.oauth_client_secret = None  # Clear sensitive data
    record.slack_app_id = None  # Clear app ID since it's been deleted
    await _save_record(record)

    await _update_approval_message(
        record,
        text=f"❌ OAuth installation was cancelled or denied. Error: {error}",
    )

    logger.info(
        "OAuth cancellation handled",
        request_id=request_id,
        agent_name=record.agent_name,
        error=error,
    )


async def get_bot_status(agent_name: str) -> BotStatusResponse:
    """Get the status of Slack bot setup for an agent.

    Checks both the database (for live bots) and Redis (for pending requests).

    Args:
        agent_name: AHS agent name.

    Returns:
        BotStatusResponse.
    """
    agent_name = agent_name.lower()

    # Check if agent already has a live bot via agent config lookup
    live_bot_config = await get_agent_config_by_name(agent_name)

    # Check for pending/recent request in Redis
    pending_record = await _load_record_by_agent(agent_name)

    return BotStatusResponse(
        agent_name=agent_name,
        has_slack_bot=live_bot_config is not None,
        pending_request=pending_record,
        slack_app_id=live_bot_config.app_id if live_bot_config else None,
        slack_name=live_bot_config.slack_name if live_bot_config else None,
    )


def build_approval_blocks(record: BotCreationRecord) -> list[dict[str, Any]]:
    """Build Slack Block Kit message for bot creation approval.

    Args:
        record: The BotCreationRecord to build the message for.

    Returns:
        List of Slack block dicts.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🤖 New Slack Bot Request",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Agent:*\n`{record.agent_name}`"},
                {"type": "mrkdwn", "text": f"*Slack Name:*\n`{record.slack_name}`"},
                {"type": "mrkdwn", "text": f"*Display Name:*\n{record.display_name}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Requested By:*\n{_format_requester(record)}",
                },
            ],
        },
    ]

    if record.description:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description:*\n{record.description}",
                },
            }
        )

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Request ID:* `{record.request_id}`",
            },
        }
    )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "bot_father_approve",
                    "value": record.request_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve Bot Creation"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Create Slack bot `{record.slack_name}` for agent `{record.agent_name}`?",
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Deny", "emoji": True},
                    "style": "danger",
                    "action_id": "bot_father_deny",
                    "value": record.request_id,
                },
            ],
        }
    )

    return blocks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_requester(record: BotCreationRecord) -> str:
    """Format the requester for display in Slack messages.

    Prefers Slack mention if available, falls back to email.
    """
    if record.requested_by and _is_slack_user_id(record.requested_by):
        return f"<@{record.requested_by}>"
    if record.requested_by_email:
        return record.requested_by_email
    if record.requested_by:
        # Yupp user ID - not as readable but better than nothing
        return f"User ID: {record.requested_by}"
    return "Unknown"


@retry_db
async def _add_agent_to_database(
    app_id: str,
    agent_name: str,
    bot_name: str,
    display_name: str,
    created_by_user_id: str | None = None,
    bot_creation_request_id: str | None = None,
) -> None:
    """Add a newly created agent to the database.

    Args:
        app_id: Slack app ID.
        agent_name: AHS agent name.
        bot_name: Bot name used for GCP secret lookup.
        display_name: Human-readable display name.
        created_by_user_id: Yupp user ID of the creator.
        bot_creation_request_id: Bot Father request ID.
    """
    async with get_async_session() as session:
        # Check if already present (idempotent)
        stmt = select(SlackAgent).where(
            SlackAgent.app_id == app_id,
            SlackAgent.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        result = await session.exec(stmt)
        existing = result.first()
        if existing:
            logger.info("Agent already present in database, skipping", app_id=app_id, agent_name=agent_name)
            return

        slack_agent = SlackAgent(
            app_id=app_id,
            agent_name=agent_name.lower(),
            bot_name=bot_name.lower(),
            display_name=display_name,
            status=SlackAgentStatus.ACTIVE,
            created_by_user_id=created_by_user_id,
            bot_creation_request_id=bot_creation_request_id,
        )
        session.add(slack_agent)
        await session.commit()

    logger.info(
        "Agent added to database",
        app_id=app_id,
        agent_name=agent_name,
        bot_name=bot_name,
    )


async def _update_approval_message(record: BotCreationRecord, text: str) -> None:
    """Update the Slack approval message with a new status text.

    Args:
        record: The BotCreationRecord (must have approval_message_ts set).
        text: New status text.
    """
    if not record.approval_message_ts or not record.approval_message_channel:
        return

    config = get_bot_father_config()
    bot_father_token = config["bot_token"]
    if not bot_father_token:
        return

    try:
        client = AsyncWebClient(token=bot_father_token)
        await client.chat_update(
            channel=record.approval_message_channel,
            ts=record.approval_message_ts,
            text=text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text},
                }
            ],
        )
    except SlackApiError as e:
        logger.warning(
            "Failed to update approval message",
            request_id=record.request_id,
            error=str(e),
        )


async def _create_ahs_agent(record: BotCreationRecord, created_by_user_id: str | None) -> bool:
    """Create an AHS agent for the newly created Slack bot.

    Args:
        record: The BotCreationRecord with agent details.
        created_by_user_id: Yupp user ID of the creator.

    Returns:
        True if agent was created successfully, False otherwise.
    """
    # Lazy import to avoid circular dependencies
    from ypl.agent_harness_service.common.types import AgentCreateRequest
    from ypl.agent_harness_service.service import create_agent as create_ahs_agent_impl

    if not created_by_user_id:
        logger.warning(
            "Cannot create AHS agent without user_id",
            agent_name=record.agent_name,
            request_id=record.request_id,
        )
        return False

    try:
        request = AgentCreateRequest(
            name=record.agent_name,
            user_id=created_by_user_id,
            display_name=record.display_name,
            description=record.description,
            additional_system_prompt=record.system_prompt,
            # Use sensible defaults for other fields
            allowed_gateways=["slack"],  # Only allow Slack gateway for Bot Father agents
        )

        await create_ahs_agent_impl(request)

        logger.info(
            "AHS agent created",
            agent_name=record.agent_name,
            request_id=record.request_id,
            user_id=created_by_user_id,
        )
        return True

    except ValueError as e:
        # Agent might already exist
        logger.warning(
            "Failed to create AHS agent",
            agent_name=record.agent_name,
            request_id=record.request_id,
            error=str(e),
        )
        return False
    except Exception as e:
        logger.error(
            "Unexpected error creating AHS agent",
            agent_name=record.agent_name,
            request_id=record.request_id,
            error=str(e),
            exc_info=True,
        )
        return False
