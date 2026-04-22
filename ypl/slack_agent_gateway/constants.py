"""Configuration management for Slack Agent Gateway.

Agent registry and per-agent secrets both live in the ``slack_agents`` table:
``app_id``, ``agent_name``, ``bot_name``, ``display_name`` are plain columns;
``bot_token_encrypted`` and ``signing_secret_encrypted`` are Fernet-encrypted
with ``settings.SLACK_AGENT_GW_ENCRYPTION_KEY`` via
``ypl.slack_agent_gateway.crypto``.

For imported rows (no encrypted payload), the gateway falls back to env vars
(``SLACK_AGENT_GATEWAY_<BOT_NAME_UPPER>_BOT_TOKEN`` / ``_SIGNING_SECRET``) so
operators can mount secrets from Vault / SSM / k8s secrets / whatever without
re-encrypting into the DB.
"""

from sqlmodel import select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.db import all_models as _  # noqa: F401  # Ensure all models are loaded for mapper config
from ypl.db.slack_agent import SlackAgent, SlackAgentStatus
from ypl.slack_agent_gateway.crypto import decrypt_secret
from ypl.slack_agent_gateway.secrets import get_env_var_secret
from ypl.slack_agent_gateway.types import AgentAppConfig
from ypl.structured_logger import get_logger
from ypl.utils import async_timed_cache

logger = get_logger()

# Default session expiration in seconds (30 minutes)
DEFAULT_SESSION_EXPIRATION_SECONDS = 30 * 60

# Redis TTL for sessions (24 hours) - longer than soft expiration for reactivation
SESSION_REDIS_TTL_SECONDS = 24 * 60 * 60

# Redis TTL for event deduplication (5 minutes)
EVENT_DEDUP_TTL_SECONDS = 5 * 60

# Redis TTL for append buffer (5 minutes)
BUFFER_TTL_SECONDS = 5 * 60

# Redis TTL for message queue (1 hour)
QUEUE_TTL_SECONDS = 60 * 60

# Max queue length per session to prevent unbounded growth
MAX_QUEUE_LENGTH = 100

# Default flush interval for append buffer (1.2 seconds to stay within Slack Tier 3 limits)
DEFAULT_FLUSH_INTERVAL_SECONDS = 1.2

# Max buffer size before force flush (characters)
MAX_BUFFER_SIZE_CHARS = 500

# Slack max message length
SLACK_MAX_MESSAGE_LENGTH = 40000

# Redis key prefixes
REDIS_KEY_PREFIX_SESSION = "slack_agent_gw:session"
REDIS_KEY_PREFIX_EVENT = "slack_agent_gw:event"
REDIS_KEY_PREFIX_BUFFER = "slack_agent_gw:buffer"
REDIS_KEY_PREFIX_BUFFER_TYPE = "slack_agent_gw:buffer_type"
REDIS_KEY_PREFIX_BUFFER_TS = "slack_agent_gw:buffer_ts"
REDIS_KEY_PREFIX_QUEUE = "slack_agent_gw:queue"
REDIS_KEY_PREFIX_FLUSH_SCHEDULE = "slack_agent_gw:flush_schedule"
REDIS_KEY_PREFIX_REPLY = "slack_agent_gw:reply"
REDIS_KEY_PREFIX_FEEDBACK_REQUESTED = "slack_agent_gw:feedback_requested"
REDIS_KEY_PREFIX_SURVEY_RESPONSE = "slack_agent_gw:survey_response"
REDIS_KEY_PREFIX_THREAD_SESSION = "slack_agent_gw:thread_session"
REDIS_KEY_PREFIX_STATUS_RATELIMIT = "slack_agent_gw:status_ratelimit"
REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE = "slack_agent_gw:status_flush_schedule"
REDIS_KEY_PREFIX_TOOL_ENTRIES = "slack_agent_gw:tool_entries"
REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING = "slack_agent_gw:tool_cluster_pending"

# Redis TTL for thread→AHS-session mapping (24 hours, matching session TTL)
THREAD_SESSION_MAPPING_TTL_SECONDS = 24 * 60 * 60

# Redis TTL for reply-to-session mapping (24 hours, matching session TTL)
REPLY_MAPPING_TTL_SECONDS = 24 * 60 * 60

# Redis TTL for feedback-requested dedup key (24 hours, matching session TTL)
FEEDBACK_REQUESTED_TTL_SECONDS = 24 * 60 * 60

# Redis TTL for survey response dedup key (1 hour)
SURVEY_RESPONSE_TTL_SECONDS = 60 * 60

# Status update rate limit — max one Slack API call per this many seconds
STATUS_RATELIMIT_SECONDS = 2

# Universal Slack API rate limit interval (per app × method [× channel]).
# Slack docs: chat.update is Tier 3 (50+/min ≈ 1.2s), chat.postMessage is ~1/sec per channel.
# A single conservative value covers both and leaves headroom against edge bursts.
SLACK_RATELIMIT_INTERVAL_SECONDS = 1.2

# Upper bound on how long a Retry-After we'll honor on one call before giving up.
# Slack rarely asks for more than a few seconds; capping prevents pathological stalls.
SLACK_RATELIMIT_MAX_RETRY_AFTER_SECONDS = 30

# Redis key prefix for the universal Slack rate-limit gate.
REDIS_KEY_PREFIX_SLACK_RATELIMIT = "slack_agent_gw:slack_ratelimit"

# Redis TTL for pending status text (1 minute — short-lived hints)
STATUS_PENDING_TTL_SECONDS = 60

# Redis TTL for tool entries list (24 hours, matching session TTL)
TOOL_ENTRIES_TTL_SECONDS = 24 * 60 * 60


def _resolve_agent_secrets(agent: SlackAgent) -> tuple[str | None, str | None]:
    """Return ``(bot_token, signing_secret)`` for an agent row.

    Prefers the encrypted columns on the row; falls back to environment
    variables keyed off ``bot_name`` when a column is null or decryption
    fails. Any irrecoverable error is logged and returns ``None`` for that
    specific secret (the caller treats null secrets as "skip this agent").
    """

    def _decrypt(encrypted: str | None, which: str) -> str | None:
        if not encrypted:
            return None
        try:
            plaintext = decrypt_secret(encrypted)
        except Exception as exc:  # pragma: no cover — defensive, log and fallback
            logger.warning(
                "Failed to decrypt slack_agents secret",
                slack_name=agent.bot_name,
                secret=which,
                error=str(exc),
            )
            return None
        return plaintext

    bot_token = _decrypt(agent.bot_token_encrypted, "bot-token") or get_env_var_secret(agent.bot_name, "bot-token")
    signing_secret = _decrypt(agent.signing_secret_encrypted, "signing-secret") or get_env_var_secret(
        agent.bot_name, "signing-secret"
    )
    return bot_token, signing_secret


@retry_db
async def _load_agent_configs_from_database() -> dict[str, AgentAppConfig]:
    """Load active agent configurations from the ``slack_agents`` table.

    Secrets resolve from the encrypted columns first, then env-var fallback.
    Rows missing both are skipped with a warning.
    """
    try:
        async with get_async_session_read_replica() as session:
            stmt = select(SlackAgent).where(
                SlackAgent.status == SlackAgentStatus.ACTIVE,
                SlackAgent.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            result = await session.exec(stmt)
            agents = result.all()
    except Exception as e:
        logger.exception("Failed to query slack_agents table", exc_info=e)
        raise

    if not agents:
        logger.info("No active agents found in database")
        return {}

    configs: dict[str, AgentAppConfig] = {}

    for agent in agents:
        bot_token, signing_secret = _resolve_agent_secrets(agent)

        if not bot_token or not signing_secret:
            logger.warning(
                "Incomplete secrets for agent, skipping",
                slack_name=agent.bot_name,
                has_bot_token=bool(bot_token),
                has_signing_secret=bool(signing_secret),
            )
            continue

        config = AgentAppConfig(
            app_id=agent.app_id,
            agent_name=agent.agent_name.lower(),
            slack_name=agent.bot_name.lower(),
            bot_token=bot_token,
            signing_secret=signing_secret,
            display_name=agent.display_name,
        )
        configs[agent.app_id] = config
        logger.info(
            "Loaded agent configuration from database",
            slack_name=agent.bot_name,
            agent_name=agent.agent_name,
            app_id=agent.app_id,
            display_name=agent.display_name,
        )

    return configs


@async_timed_cache(seconds=60)  # Cache for 60 seconds
async def get_agent_configs() -> dict[str, AgentAppConfig]:
    """Get all agent configurations (cached 60s, stale-while-revalidate).

    The ``slack_agents`` table is the sole source of truth for the registry
    across all environments. Per-agent secrets come from ``fetch_agent_secret``
    — environment variables for GCP-free environments, GCP Secret Manager for
    deployed environments.

    Note on caching behavior:
    - The 60s TTL applies to the agent list from the database.
    - Individual GCP secrets are cached in env vars for process lifetime to reduce
      Secret Manager API calls. Secret rotation requires a process restart.
    - Uses stale-while-revalidate: after TTL, returns cached value immediately and
      refreshes in background. Newly added agents may miss the first webhook within
      60s of being added to the database.

    Returns:
        Dict mapping app_id to AgentAppConfig
    """
    configs = await _load_agent_configs_from_database()
    if not configs:
        logger.warning("No agents configured for Slack Agent Gateway")
    return configs


async def get_agent_config_by_app_id(app_id: str) -> AgentAppConfig | None:
    """Get agent configuration by Slack app ID.

    Args:
        app_id: Slack app ID (e.g., 'A123...')

    Returns:
        AgentAppConfig if found, None otherwise
    """
    configs: dict[str, AgentAppConfig] = await get_agent_configs()
    return configs.get(app_id)


async def get_agent_config_by_name(agent_name: str) -> AgentAppConfig | None:
    """Get agent configuration by agent name.

    Args:
        agent_name: Internal agent name (e.g., 'giladovski')

    Returns:
        AgentAppConfig if found, None otherwise
    """
    configs: dict[str, AgentAppConfig] = await get_agent_configs()
    for config in configs.values():
        if config.agent_name == agent_name.lower():
            return config
    return None


async def get_all_signing_secrets() -> list[str]:
    """Get all configured signing secrets for signature verification.

    Since we can't read api_app_id until after verification, we try
    verification against all configured signing secrets.

    Includes both agent secrets and the Bot Father signing secret.

    Returns:
        List of signing secrets from all configured agents and Bot Father
    """
    secrets: list[str] = []

    # Add agent signing secrets
    try:
        configs = await get_agent_configs()
        secrets.extend(config.signing_secret for config in configs.values())
    except Exception as e:
        logger.exception("Failed to get agent configs in get_all_signing_secrets", exc_info=e)
        raise

    # Add Bot Father signing secret (for approval/denial interactions)
    bot_father_secret = settings.SLACK_BOT_FATHER_SIGNING_SECRET
    if bot_father_secret:
        secrets.append(bot_father_secret)

    return secrets


def get_bot_father_config() -> dict[str, str]:
    """Get Bot Father configuration from settings.

    Returns a dict with keys: bot_token, signing_secret, app_config_refresh_token,
    approval_channel.

    Note: app_config_token is not included - access tokens are obtained on-demand
    using the refresh token and cached in Redis.

    All values are loaded from environment variables / GCP secrets via pydantic-settings.

    Returns:
        Dict with Bot Father configuration values.
    """
    return {
        "bot_token": settings.SLACK_BOT_FATHER_BOT_TOKEN,
        "signing_secret": settings.SLACK_BOT_FATHER_SIGNING_SECRET,
        "app_config_refresh_token": settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN,
        "approval_channel": settings.SLACK_BOT_FATHER_APPROVAL_CHANNEL,
    }


def get_agent_service_url() -> str:
    """Get the Agent Service URL for outbound calls.

    Returns:
        Agent Service base URL or empty string if not configured
    """
    return settings.AGENT_HARNESS_SERVICE_BASE_URL


def clear_config_cache() -> None:
    """Clear the configuration cache. Useful for testing."""
    get_agent_configs.cache_clear()
