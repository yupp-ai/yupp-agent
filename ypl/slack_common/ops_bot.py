"""OpsBot — the workspace-wide default Slack app used for reads and write fallback.

Policy (agreed across AHS + MCP server):

- **All reads** (thread fetch, search, user resolution) use OpsBot's user token.
  The user token reads every channel the installing human can see, so we don't
  need to invite a bot to each channel. ``users.info`` still requires a bot
  token (``users:read``) until the user token is granted the same scope.
- **Writes** follow the initiating agent's bot first and fall back to OpsBot's
  bot token when the agent has no Slack presence. The fallback is logged so
  every occurrence is visible in ops logs.

Both tokens are lazy module-level singletons, so changes to the environment
require a process restart.
"""

import os
from typing import Any

from cachetools import TTLCache
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.structured_logger import get_logger

logger = get_logger()


BOT_TOKEN_ENV = "SLACK_MCP_SERVER_APP_BOT_TOKEN"
USER_TOKEN_ENV = "SLACK_MCP_SERVER_APP_USER_TOKEN"


# Shared cache for user-ID → display-name resolution (1 hour, 2k entries).
_user_name_cache: TTLCache[str, str] = TTLCache(maxsize=2000, ttl=3600)

_ops_bot_write_client: AsyncWebClient | None = None
_ops_bot_user_client: AsyncWebClient | None = None


def get_ops_bot_write_client() -> AsyncWebClient:
    """Return (lazily create) the singleton OpsBot bot-token client.

    Used for: posting as OpsBot when an agent has no Slack presence, and for
    ``users.info`` (``users:read``) during user-ID resolution.
    """
    global _ops_bot_write_client
    if _ops_bot_write_client is None:
        token = os.environ.get(BOT_TOKEN_ENV)
        if not token:
            raise ValueError(f"{BOT_TOKEN_ENV} environment variable is not set")
        _ops_bot_write_client = AsyncWebClient(
            token=token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=2)],
        )
    return _ops_bot_write_client


def get_ops_bot_user_client() -> AsyncWebClient:
    """Return (lazily create) the singleton OpsBot user-token client.

    Used for: ``conversations.replies`` / ``search.messages`` — any read that
    should see every channel the installing human can see.
    """
    global _ops_bot_user_client
    if _ops_bot_user_client is None:
        token = os.environ.get(USER_TOKEN_ENV)
        if not token:
            raise ValueError(f"{USER_TOKEN_ENV} environment variable is not set")
        _ops_bot_user_client = AsyncWebClient(
            token=token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=2)],
        )
    return _ops_bot_user_client


# Back-compat alias — earlier call sites used ``get_ops_bot_read_client`` in
# their in-file helpers. Prefer :func:`get_ops_bot_user_client` in new code.
get_ops_bot_read_client = get_ops_bot_user_client


async def resolve_display_name(user_id: str) -> str:
    """Resolve a Slack user ID to a display name, caching successful lookups.

    Uses the OpsBot bot-token client because ``users.info`` requires
    ``users:read`` and OpsBot's user token only holds ``search:read``. On any
    failure, returns the raw ``user_id`` so callers always get a string and
    can retry next time (failures aren't cached).
    """
    if user_id in _user_name_cache:
        return str(_user_name_cache[user_id])

    client = get_ops_bot_write_client()
    try:
        response = await client.users_info(user=user_id)
        if response.get("ok") and response.get("user"):
            user = response["user"]
            profile: dict[str, Any] = user.get("profile", {}) or {}
            # display_name is often "" when unset — rely on falsy-chain
            # fallthrough to real_name / name.
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            _user_name_cache[user_id] = display_name
            return str(display_name)
    except SlackApiError as e:
        logger.warning("Failed to resolve Slack user", user_id=user_id, error=str(e))

    return user_id


async def agent_has_slack_presence(db: AsyncSession, agent_name: str | None) -> bool:
    """Return True iff ``agent_name`` has a usable Slack bot identity.

    A presence is "usable" when the row in ``slack_agents`` either carries an
    encrypted bot token or has an env-var fallback token configured. We don't
    decrypt or fetch the actual token here — SAG does that at post time; we
    only need to know whether to attempt the per-agent path.

    Called by the ``send_slack_message`` write fallback. Cheap DB lookup
    (indexed unique on ``slack_agents.agent_name``).
    """
    if not agent_name:
        return False

    row = (
        await db.execute(
            text(
                "SELECT bot_name, bot_token_encrypted "
                "FROM slack_agents "
                "WHERE agent_name = :agent_name AND deleted_at IS NULL AND status = 'ACTIVE' "
                "LIMIT 1"
            ),
            {"agent_name": agent_name},
        )
    ).fetchone()

    if row is None:
        return False

    bot_name, bot_token_encrypted = row
    if bot_token_encrypted:
        return True

    # No encrypted token — SAG's env-var fallback uses SLACK_AGENT_GATEWAY_<BOT>_BOT_TOKEN.
    fallback_env = f"SLACK_AGENT_GATEWAY_{str(bot_name).upper()}_BOT_TOKEN"
    return bool(os.environ.get(fallback_env))
