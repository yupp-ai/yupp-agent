"""GCP Secret Manager integration for Slack Agent Gateway.

Fetches agent secrets from GCP Secret Manager at runtime, with fallback to
environment variables for local development.
"""

import os
from functools import lru_cache

from google.api_core import exceptions as core_exceptions
from google.api_core import retry_async
from google.cloud import secretmanager_v1 as secretmanager

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


@lru_cache
def _get_async_secret_manager_client() -> secretmanager.SecretManagerServiceAsyncClient:
    """Get cached async Secret Manager client."""
    return secretmanager.SecretManagerServiceAsyncClient()


def _build_secret_name(agent_name: str, secret_type: str, environment: str) -> str:
    """Build GCP secret name from naming convention.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"
        environment: Environment name (e.g., "staging", "production")

    Returns:
        Full secret name (e.g., "ym-slack-agent-gateway-giladovski-app-id-staging")
    """
    return f"ym-slack-agent-gateway-{agent_name.lower()}-{secret_type}-{environment}"


def _build_env_var_name(agent_name: str, secret_type: str) -> str:
    """Build environment variable name for a secret.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"

    Returns:
        Env var name (e.g., "SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID")
    """
    env_var_suffix = secret_type.upper().replace("-", "_")
    # Agent names can contain dashes (e.g. "eng-raccoon" or bot_name "eng-raccoon");
    # env var names cannot — normalise to underscores to match what operators
    # put in .env.
    agent_suffix = agent_name.upper().replace("-", "_")
    return f"SLACK_AGENT_GATEWAY_{agent_suffix}_{env_var_suffix}"


def _get_env_var_secret(agent_name: str, secret_type: str) -> str | None:
    """Try to get secret from environment variable.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"

    Returns:
        Secret value or None if not found
    """
    env_var_name = _build_env_var_name(agent_name, secret_type)
    return os.environ.get(env_var_name) or getattr(settings, env_var_name, None) or None


def _cache_secret_in_env(agent_name: str, secret_type: str, value: str) -> None:
    """Cache a fetched secret in environment variable for process lifetime.

    This avoids repeated GCP Secret Manager calls for the same secret.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"
        value: The secret value to cache
    """
    env_var_name = _build_env_var_name(agent_name, secret_type)
    os.environ[env_var_name] = value


async def _fetch_gcp_secret(secret_name: str) -> str | None:
    """Fetch a secret from GCP Secret Manager with retry logic.

    Args:
        secret_name: The secret name (without project path)

    Returns:
        Secret value or None if not found
    """
    if not settings.GCP_PROJECT_ID:
        logger.debug("GCP_PROJECT_ID not set, skipping GCP secret fetch")
        return None

    try:
        client = _get_async_secret_manager_client()
        name = f"projects/{settings.GCP_PROJECT_ID}/secrets/{secret_name}/versions/latest"

        retry_config = retry_async.AsyncRetry(
            initial=0.5,
            maximum=5.0,
            multiplier=2.0,
            predicate=retry_async.if_exception_type(
                core_exceptions.ResourceExhausted,
                core_exceptions.DeadlineExceeded,
                core_exceptions.ServiceUnavailable,
            ),
            deadline=10.0,
            reraise=True,
        )

        response = await client.access_secret_version(request={"name": name}, retry=retry_config, timeout=5.0)
        return response.payload.data.decode("UTF-8")
    except core_exceptions.NotFound:
        logger.warning("Secret not found in GCP", secret_name=secret_name)
        return None
    except Exception as e:
        logger.error("Failed to fetch secret from GCP", secret_name=secret_name, error=str(e), exc_info=True)
        return None


async def create_agent_secret(
    agent_name: str,
    secret_type: str,
    value: str,
    environment: str | None = None,
) -> None:
    """Create a new GCP secret and store its initial value.

    Uses the same naming convention as fetch_agent_secret:
      ym-slack-agent-gateway-{agent_name}-{secret_type}-{environment}

    This is a no-op in local/test environments.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"
        value: The secret value to store
        environment: Override for the environment (defaults to settings.ENVIRONMENT)

    Raises:
        RuntimeError: If the GCP call fails.
    """
    if settings.ENVIRONMENT in ("local", "test"):
        logger.debug(
            "Skipping GCP secret creation in local/test environment",
            agent_name=agent_name,
            secret_type=secret_type,
        )
        return

    if not settings.GCP_PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID not set; cannot create secret")

    env = environment or settings.ENVIRONMENT
    secret_name = _build_secret_name(agent_name, secret_type, env)
    project_path = f"projects/{settings.GCP_PROJECT_ID}"
    secret_resource_name = f"{project_path}/secrets/{secret_name}"

    client = _get_async_secret_manager_client()

    # Create the secret resource (idempotent — ignore AlreadyExists)
    try:
        await client.create_secret(
            request={
                "parent": project_path,
                "secret_id": secret_name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        logger.info("GCP secret resource created", secret_name=secret_name)
    except core_exceptions.AlreadyExists:
        logger.debug("GCP secret already exists, adding new version", secret_name=secret_name)
    except Exception as e:
        logger.error("Failed to create GCP secret resource", secret_name=secret_name, error=str(e))
        raise RuntimeError(f"Failed to create GCP secret '{secret_name}': {e}") from e

    # Add the secret version
    try:
        await client.add_secret_version(
            request={
                "parent": secret_resource_name,
                "payload": {"data": value.encode("UTF-8")},
            }
        )
        logger.info("GCP secret version added", secret_name=secret_name)
    except Exception as e:
        logger.error("Failed to add GCP secret version", secret_name=secret_name, error=str(e))
        raise RuntimeError(f"Failed to add version to GCP secret '{secret_name}': {e}") from e

    # Cache the new secret in env var for immediate availability
    _cache_secret_in_env(agent_name, secret_type, value)


async def update_bot_father_refresh_token(new_refresh_token: str) -> None:
    """Update the Bot Father refresh token in GCP Secret Manager.

    Called automatically when Slack issues a new refresh token during token rotation.
    The old refresh token is invalidated, so this must be persisted to GCP.

    This is a no-op in local/test environments.

    Args:
        new_refresh_token: The new refresh token from Slack (xoxe-1-...).

    Raises:
        RuntimeError: If the GCP call fails.
    """
    if settings.ENVIRONMENT in ("local", "test"):
        logger.debug("Skipping GCP secret update in local/test environment")
        return

    if not settings.GCP_PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID not set; cannot update secret")

    secret_name = f"ym-bot-father-app-config-refresh-token-{settings.ENVIRONMENT}"
    secret_resource_name = f"projects/{settings.GCP_PROJECT_ID}/secrets/{secret_name}"

    client = _get_async_secret_manager_client()

    try:
        await client.add_secret_version(
            request={
                "parent": secret_resource_name,
                "payload": {"data": new_refresh_token.encode("UTF-8")},
            }
        )
        # Also update in-memory so subsequent refreshes within this process use the new token
        settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = new_refresh_token
        logger.info(
            "Bot Father refresh token updated in GCP Secret Manager and in-memory",
            secret_name=secret_name,
            new_token_prefix=new_refresh_token[:20] + "...",
        )
    except Exception as e:
        logger.error(
            "Failed to update Bot Father refresh token in GCP",
            secret_name=secret_name,
            error=str(e),
        )
        raise RuntimeError(f"Failed to update Bot Father refresh token: {e}") from e


async def fetch_agent_secret(agent_name: str, secret_type: str) -> str | None:
    """Fetch an agent secret.

    Order of precedence:
    - Local/test: Environment variable only
    - Staging/production: Environment variable (cached), then GCP Secret Manager

    Successfully fetched GCP secrets are cached in environment variables to avoid
    repeated API calls for the process lifetime.

    Args:
        agent_name: Agent name (e.g., "giladovski")
        secret_type: One of "app-id", "bot-token", "signing-secret"

    Returns:
        Secret value or None if not found
    """
    # Check env var first (serves as cache for previously fetched secrets)
    env_value = _get_env_var_secret(agent_name, secret_type)
    if env_value:
        return env_value

    # For local/test, don't try GCP
    if settings.ENVIRONMENT in ("local", "test"):
        return None

    # Fetch from GCP Secret Manager
    secret_name = _build_secret_name(agent_name, secret_type, settings.ENVIRONMENT)
    gcp_value = await _fetch_gcp_secret(secret_name)

    # Cache successfully fetched secret in env var for process lifetime
    if gcp_value:
        _cache_secret_in_env(agent_name, secret_type, gcp_value)

    return gcp_value
