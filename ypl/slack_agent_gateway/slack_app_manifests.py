"""Slack App Manifests API async client for Bot Father.

Creates and manages Slack apps programmatically via the Manifests API.
Requires an App Configuration Refresh Token to obtain access tokens.

Access tokens are:
- Obtained on-demand using the refresh token
- Cached in Redis (encrypted) with expiry tracking
- Automatically refreshed when expired

Note on undocumented APIs:
    This module uses some undocumented Slack APIs (developer.apps.owners.*)
    for collaborator management. These are the same APIs used by the official
    Slack CLI (https://github.com/slackapi/slack-cli). Undocumented APIs may
    change without notice per Slack's API Terms of Service.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from ypl.backend.config import settings
from ypl.db.redis import get_redis_client
from ypl.slack_agent_gateway.crypto import decrypt_token_data, encrypt_token_data
from ypl.slack_agent_gateway.token_storage import invalidate_refresh_token, save_bot_father_refresh_token
from ypl.structured_logger import get_logger

logger = get_logger()

# Slack Manifests API base URL
_MANIFESTS_API_BASE = "https://slack.com/api"

# Redis key for cached access token
_REDIS_KEY_APP_CONFIG_TOKEN = "slack_agent_gw:bot_father:app_config_token"

# Buffer time before expiry to trigger refresh (5 minutes)
_TOKEN_EXPIRY_BUFFER_SECONDS = 5 * 60

# Default token TTL if Slack doesn't provide expiry (11 hours, tokens typically expire in 12)
_DEFAULT_TOKEN_TTL_SECONDS = 11 * 60 * 60

# Lock for token refresh to prevent concurrent refresh attempts
_token_refresh_lock = asyncio.Lock()

# OAuth scopes required by all SAG-managed bots. ``files:read`` is needed to
# download files attached to Slack messages — without it, Slack 403s the
# ``url_private_download`` URL and attachments silently drop.
_BOT_SCOPES = [
    "app_mentions:read",
    "channels:read",
    "channels:history",
    "chat:write",
    "files:read",
    "groups:read",
    "groups:history",
    "incoming-webhook",
    "reactions:read",
    "reactions:write",
]


async def _get_cached_token() -> str | None:
    """Get a valid cached access token from Redis.

    Returns:
        Valid access token or None if not cached or expired.
    """
    try:
        redis = await get_redis_client()
        encrypted: str | None = await redis.get(_REDIS_KEY_APP_CONFIG_TOKEN)
        if not encrypted:
            return None

        # Try new key first, fall back to legacy key for one-time migration
        result = decrypt_token_data(encrypted)
        used_legacy_key = False
        if not result:
            result = decrypt_token_data(encrypted, use_legacy_key=True)
            used_legacy_key = True
        if not result:
            return None

        token, expires_at = result
        now = datetime.now(UTC)

        # Re-cache with new key if we had to use legacy key (one-time migration)
        if used_legacy_key:
            remaining_ttl = int((expires_at - now).total_seconds())
            if remaining_ttl > 0:
                logger.info("Re-caching token with new encryption key", remaining_ttl=remaining_ttl)
                await _cache_token(token, expires_in_seconds=remaining_ttl)

        # Check if token is expired or about to expire
        if expires_at <= now + timedelta(seconds=_TOKEN_EXPIRY_BUFFER_SECONDS):
            logger.info("Cached token expired or expiring soon", expires_at=expires_at.isoformat())
            return None

        return token
    except Exception as e:
        logger.warning("Failed to get cached token from Redis", error=str(e))
        return None


async def _cache_token(token: str, expires_in_seconds: int | None = None) -> None:
    """Cache an access token in Redis.

    Args:
        token: The access token to cache.
        expires_in_seconds: Token TTL from Slack, or None to use default.
    """
    try:
        ttl = expires_in_seconds or _DEFAULT_TOKEN_TTL_SECONDS
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        encrypted = encrypt_token_data(token, expires_at)

        redis = await get_redis_client()
        # Set with TTL slightly longer than token expiry to allow for clock skew
        await redis.set(_REDIS_KEY_APP_CONFIG_TOKEN, encrypted, ex=ttl + 60)

        logger.info("Cached access token in Redis", expires_at=expires_at.isoformat(), ttl_seconds=ttl)
    except Exception as e:
        logger.warning("Failed to cache token in Redis", error=str(e))


async def _refresh_access_token(refresh_token: str) -> str:
    """Refresh the access token using the refresh token.

    Calls tooling.tokens.rotate with the refresh token to get a new access token.
    The new token is cached in Redis.

    Args:
        refresh_token: The refresh token (xoxe-1-...).

    Returns:
        New App Configuration Token (xoxe-...).

    Raises:
        RuntimeError: If the token refresh fails.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_MANIFESTS_API_BASE}/tooling.tokens.rotate",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"refresh_token": refresh_token},
        )
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        logger.error("tooling.tokens.rotate (refresh) failed", error=error, response=data)

        # If the refresh token is invalid, clear the stale cached/stored token
        # so the next request will reload from Secret Manager.
        if error == "invalid_refresh_token":
            await invalidate_refresh_token()

        raise RuntimeError(f"Failed to refresh app config token: {error}")

    new_token: str = data["token"]
    new_refresh_token: str | None = data.get("refresh_token")

    # Slack returns exp as a Unix epoch timestamp, not seconds-until-expiry
    expires_in: int | None = None
    if exp := data.get("exp"):
        expires_in = max(int(exp) - int(time.time()), 60)  # at least 60s TTL

    # CRITICAL: If Slack issues a new refresh token, the old one is invalidated.
    # This must be persisted to durable storage or all future refreshes will fail
    # after the process restarts (or Redis cache expires).
    if new_refresh_token:
        try:
            await save_bot_father_refresh_token(new_refresh_token)
            logger.info(
                "Bot Father refresh token saved to DB",
                new_refresh_token_prefix=new_refresh_token[:20] + "...",
            )
        except Exception as e:
            # Log critical error but don't fail the token refresh - the new access token
            # is still valid and can be used. Manual intervention will be needed before
            # next process restart.
            logger.critical(
                "Failed to save refresh token to DB - MANUAL UPDATE REQUIRED",
                new_refresh_token_prefix=new_refresh_token[:20] + "...",
                error=str(e),
            )

    # Cache the new token
    await _cache_token(new_token, expires_in)

    logger.info(
        "App config token refreshed successfully",
        has_new_refresh_token=new_refresh_token is not None,
    )
    return new_token


async def get_valid_app_config_token(refresh_token: str) -> str:
    """Get a valid App Configuration Token, refreshing if needed.

    Checks Redis cache first. If not found or expired, refreshes using
    the refresh token. Uses a lock to prevent concurrent refresh attempts.

    Args:
        refresh_token: The refresh token for obtaining new access tokens.

    Returns:
        A valid App Configuration Token.

    Raises:
        RuntimeError: If token refresh fails.
    """
    # Try cached token first
    cached_token = await _get_cached_token()
    if cached_token:
        return cached_token

    # Need to refresh - acquire lock to prevent concurrent refreshes
    async with _token_refresh_lock:
        # Double-check after acquiring lock
        cached_token = await _get_cached_token()
        if cached_token:
            return cached_token

        logger.info("No valid cached token, refreshing")
        return await _refresh_access_token(refresh_token)


async def clear_token_cache() -> None:
    """Clear the cached app config token from Redis.

    Call this when token is known to be invalid (e.g., after token_expired error).
    """
    try:
        redis = await get_redis_client()
        await redis.delete(_REDIS_KEY_APP_CONFIG_TOKEN)
        logger.info("Cleared cached access token from Redis")
    except Exception as e:
        logger.warning("Failed to clear cached token from Redis", error=str(e))


def _sag_slack_base_url() -> str:
    """Return the public SAG base URL used inside Slack app manifests / OAuth.

    Sourced from ``settings.GATEWAY_BASE_URL``; the ``/api/v1/slack`` suffix is
    appended because that's what SAG's FastAPI router mounts.
    """
    gateway_url = settings.GATEWAY_BASE_URL.rstrip("/")
    if not gateway_url:
        raise RuntimeError(
            "GATEWAY_BASE_URL is not configured — cannot build Slack app manifest URLs. "
            "Set GATEWAY_BASE_URL in the environment to the public SAG URL."
        )
    return f"{gateway_url}/api/v1/slack"


def build_manifest(
    slack_name: str,
    display_name: str,
) -> dict[str, Any]:
    """Build a Slack app manifest for a new SAG-managed bot.

    Args:
        slack_name: The Slack bot username (e.g., 'giladovski'). Lowercase, alphanumeric + hyphens.
        display_name: Human-readable display name (e.g., 'Giladovski').

    Returns:
        Slack app manifest dict suitable for apps.manifest.create.

    Note:
        Slack's manifest schema does not support icon_url in display_information.
        App icons must be set via the Slack app settings UI after creation.
        See: https://docs.slack.dev/reference/app-manifest/
    """
    base_url = _sag_slack_base_url()
    events_url = f"{base_url}/events"
    interactions_url = f"{base_url}/interactions"
    oauth_redirect_url = f"{base_url}/oauth/callback"

    return {
        "display_information": {"name": display_name},
        "features": {
            "bot_user": {
                "display_name": display_name,
                "always_online": True,
            },
        },
        "oauth_config": {
            "redirect_urls": [oauth_redirect_url],
            "scopes": {
                "bot": _BOT_SCOPES,
            },
        },
        "settings": {
            "event_subscriptions": {
                "request_url": events_url,
                "bot_events": [
                    "app_mention",
                    "reaction_added",
                ],
            },
            "interactivity": {
                "is_enabled": True,
                "request_url": interactions_url,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


async def create_slack_app(
    manifest: dict[str, Any],
    refresh_token: str,
) -> dict[str, Any]:
    """Create a new Slack app using the App Manifests API.

    Calls apps.manifest.create with the provided manifest.
    Automatically refreshes the token and retries if token_expired error occurs.

    Args:
        manifest: The app manifest dict (from build_manifest).
        refresh_token: Refresh token for obtaining access tokens.

    Returns:
        Dict with keys: app_id, credentials (signing_secret, verification_token,
        oauth_client_id, oauth_client_secret).

    Raises:
        RuntimeError: If the Slack API call fails.
    """
    token = await get_valid_app_config_token(refresh_token)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_MANIFESTS_API_BASE}/apps.manifest.create",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            content=json.dumps({"manifest": manifest}),
        )
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")

        # Auto-refresh on token_expired and retry once
        if error == "token_expired":
            logger.info("Token expired, clearing cache and retrying")
            await clear_token_cache()
            token = await get_valid_app_config_token(refresh_token)

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{_MANIFESTS_API_BASE}/apps.manifest.create",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    content=json.dumps({"manifest": manifest}),
                )
                response.raise_for_status()
                data = response.json()

            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                logger.error("apps.manifest.create failed after token refresh", error=error, response=data)
                raise RuntimeError(f"apps.manifest.create failed: {error}")
        else:
            logger.error("apps.manifest.create failed", error=error, response=data)
            raise RuntimeError(f"apps.manifest.create failed: {error}")

    app_id: str = data["app_id"]
    credentials: dict[str, str] = data.get("credentials", {})

    logger.info("Created Slack app via Manifests API", app_id=app_id)
    return {
        "app_id": app_id,
        "credentials": credentials,
    }


def build_oauth_install_url(
    client_id: str,
    state: str | None = None,
) -> str:
    """Build the OAuth install URL for a newly created Slack app.

    This URL is clicked by an admin to complete the OAuth flow and grant
    the bot token. Slack will redirect to our callback with an auth code.

    Args:
        client_id: OAuth client ID from apps.manifest.create credentials.
        state: Optional state parameter for CSRF protection (e.g., request_id).

    Returns:
        OAuth authorize URL that the admin should click.
    """
    redirect_uri = f"{_sag_slack_base_url()}/oauth/callback"
    scopes = ",".join(_BOT_SCOPES)

    params: dict[str, str] = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"


async def exchange_oauth_code(
    code: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Exchange an OAuth authorization code for access tokens.

    Called from the OAuth callback after admin installs the app.

    Args:
        code: Authorization code from Slack OAuth redirect.
        client_id: OAuth client ID.
        client_secret: OAuth client secret.

    Returns:
        Dict with: access_token (bot token), team, authed_user, etc.

    Raises:
        RuntimeError: If the OAuth exchange fails.
    """
    redirect_uri = f"{_sag_slack_base_url()}/oauth/callback"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_MANIFESTS_API_BASE}/oauth.v2.access",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        logger.error("oauth.v2.access failed", error=error, response=data)
        raise RuntimeError(f"OAuth code exchange failed: {error}")

    # Extract bot token from response
    access_token = data.get("access_token")
    if not access_token:
        logger.error("No access_token in OAuth response", response=data)
        raise RuntimeError("OAuth response missing access_token")

    logger.info(
        "OAuth code exchanged successfully",
        team_id=data.get("team", {}).get("id"),
        app_id=data.get("app_id"),
    )

    return {
        "access_token": access_token,  # This is the bot token (xoxb-...)
        "app_id": data.get("app_id"),
        "team": data.get("team", {}),
        "authed_user": data.get("authed_user", {}),
        "bot_user_id": data.get("bot_user_id"),
    }


async def delete_slack_app(app_id: str, refresh_token: str) -> None:
    """Delete a Slack app via the App Manifests API (cleanup on failure).

    Calls apps.manifest.delete.
    Automatically refreshes the config token and retries if token_expired error occurs.

    Args:
        app_id: Slack app ID to delete.
        refresh_token: Refresh token for obtaining access tokens.

    Raises:
        RuntimeError: If the Slack API call fails.
    """
    token = await get_valid_app_config_token(refresh_token)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_MANIFESTS_API_BASE}/apps.manifest.delete",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"app_id": app_id},
        )
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")

        # Auto-refresh on token_expired and retry once
        if error == "token_expired":
            logger.info("Token expired during app deletion, clearing cache and retrying")
            await clear_token_cache()
            token = await get_valid_app_config_token(refresh_token)

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{_MANIFESTS_API_BASE}/apps.manifest.delete",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"app_id": app_id},
                )
                response.raise_for_status()
                data = response.json()

            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                logger.error("apps.manifest.delete failed after token refresh", app_id=app_id, error=error)
                raise RuntimeError(f"apps.manifest.delete failed: {error}")
        else:
            logger.error("apps.manifest.delete failed", app_id=app_id, error=error)
            raise RuntimeError(f"apps.manifest.delete failed: {error}")

    logger.info("Deleted Slack app", app_id=app_id)


# ---------------------------------------------------------------------------
# App Collaborators (UNDOCUMENTED API - developer.apps.owners.*)
#
# These APIs are not officially documented by Slack but are used by the
# official Slack CLI. Discovered via: https://github.com/slackapi/slack-cli
#
# WARNING: Undocumented APIs may change without notice per Slack's ToS.
# ---------------------------------------------------------------------------


async def add_app_collaborator(
    app_id: str,
    user_email: str,
    permission_type: Literal["owner", "reader"],
    refresh_token: str,
) -> None:
    """Add a collaborator to a Slack app.

    WARNING: This uses an undocumented Slack API (developer.apps.owners.add).
    The API is used by the official Slack CLI but is not publicly documented
    and may change without notice.

    See: https://github.com/slackapi/slack-cli/blob/main/internal/api/collaborators.go

    Args:
        app_id: Slack app ID.
        user_email: Email address of the user to add as collaborator.
            Must be a member of the same Slack workspace.
        permission_type: Permission level - "owner" or "reader".
        refresh_token: Refresh token for obtaining access tokens.

    Raises:
        RuntimeError: If the API call fails.
    """
    # Retry loop: first attempt with cached token, second with fresh token after cache clear
    for attempt in range(2):
        token = await get_valid_app_config_token(refresh_token)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{_MANIFESTS_API_BASE}/developer.apps.owners.add",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                # NOTE: This undocumented API expects token in form body, not Authorization header.
                # This differs from documented APIs (apps.manifest.*) which use Bearer auth.
                # See: https://github.com/slackapi/slack-cli/blob/main/internal/api/collaborators.go
                data={
                    "token": token,
                    "app_id": app_id,
                    "user_email": user_email,
                    "permission_type": permission_type,
                },
            )
            response.raise_for_status()
            data = response.json()

        if data.get("ok"):
            break

        error = data.get("error", "unknown_error")
        if error == "token_expired" and attempt == 0:
            logger.info("Token expired while adding collaborator, clearing cache and retrying")
            await clear_token_cache()
            continue

        # Log and raise on final failure
        logger.error(
            "developer.apps.owners.add failed",
            app_id=app_id,
            user_email=user_email,
            error=error,
            response=data,
            attempt=attempt + 1,
        )
        raise RuntimeError(f"Failed to add collaborator: {error}")

    logger.info(
        "Added collaborator to Slack app",
        app_id=app_id,
        user_email=user_email,
        permission_type=permission_type,
    )
