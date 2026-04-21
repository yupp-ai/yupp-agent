"""GitHub authentication tools for the harness MCP server.

Implements the GitHub Device Flow so PRs can be attributed to the human user
rather than the AHS bot.  Tokens are stored in encrypted Redis and reused
across sessions for the same user.
"""

from __future__ import annotations
import asyncio
import os
import time

import httpx

from ypl.agent_harness_service.tools.github_token_storage import (
    GitHubTokenData,
    RefreshResult,
    get_github_token_data,
    refresh_github_token,
    remove_github_token,
    store_github_token_data,
)
from ypl.agent_harness_service.tools.mcp_instance import (
    _get_current_message_user_id,
    _session_auth_terminal_states,
    _session_polling_tasks,
    _validate_session_id,
    mcp,
)
from ypl.backend.llm.db_helpers import get_user_id_by_github_username
from ypl.backend.utils.async_utils import create_background_task
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# GitHub App credentials (set in environment)
# ---------------------------------------------------------------------------

GITHUB_APP_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "")
# Client secret is needed for refreshing tokens when "User-to-server token expiration" is enabled
GITHUB_APP_CLIENT_SECRET = os.environ.get("AHS_GITHUB_APP_CLIENT_SECRET", "")

# ---------------------------------------------------------------------------
# Per-user async locks to prevent token refresh races within one AHS instance.
# TODO: For multi-instance deployments, consider distributed locking (Redis SETNX).
# ---------------------------------------------------------------------------

_user_github_tokens_locks: dict[str, asyncio.Lock] = {}

# Terminal auth state string constants
AUTH_STATE_EXPIRED = "expired"
AUTH_STATE_DENIED = "denied"
AUTH_STATE_ERROR = "error"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_github_token(token: str) -> tuple[bool, str | None, bool]:
    """Validate a GitHub token by calling the /user API.

    Returns:
        Tuple of (is_valid, github_username or None, is_auth_failure).
        is_auth_failure is True for 401/403 (token definitely invalid),
        False for network errors or other transient failures.
    """
    try:
        resp = httpx.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("login"), False
        # 401/403 means token is definitely invalid (revoked, expired, wrong scope)
        if resp.status_code in (401, 403):
            return False, None, True
        # Other status codes (5xx, etc.) are transient - don't evict token
        logger.warning("GitHub API returned unexpected status", status_code=resp.status_code)
        return False, None, False
    except Exception as e:
        # Network errors are transient - don't evict token
        logger.warning("GitHub token validation failed (transient)", error=str(e))
        return False, None, False


async def _validate_github_token_async(token: str) -> tuple[bool, str | None, bool]:
    """Async variant of token validation for use inside async MCP tools."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("login"), False
        if resp.status_code in (401, 403):
            return False, None, True
        logger.warning("GitHub API returned unexpected status", status_code=resp.status_code)
        return False, None, False
    except Exception as e:
        logger.warning("GitHub token validation failed (transient)", error=str(e))
        return False, None, False


async def _get_github_user_info_async(token: str) -> dict[str, str | None] | None:
    """Get GitHub user info (username, name, email) using the user's token.

    Returns:
        Dict with github_username, github_name, github_email, or None if token is invalid.
        github_email falls back to the GitHub noreply email if the user's email is private.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        username = data.get("login")
        user_id = data.get("id")
        name = data.get("name") or username  # Fall back to username if name not set
        email = data.get("email")
        # If email is private, use GitHub's noreply email format
        if not email and username and user_id:
            email = f"{user_id}+{username}@users.noreply.github.com"
        return {
            "github_username": username,
            "github_name": name,
            "github_email": email,
        }
    except Exception as e:
        logger.warning("Failed to get GitHub user info", error=str(e))
        return None


async def _try_refresh_token_with_lock(
    user_id: str, token_data: GitHubTokenData
) -> tuple[RefreshResult, GitHubTokenData | None]:
    """Attempt to refresh an expired token under lock.

    This helper encapsulates the common pattern of:
    1. Acquiring the per-user lock to avoid races
    2. Re-fetching token data under lock (double-check pattern)
    3. Calling refresh_github_token if still needed
    4. Evicting the token on auth failure

    Args:
        user_id: The Yupp user_id.
        token_data: The current token data (used to decide if refresh is needed).

    Returns:
        Tuple of (RefreshResult, refreshed_token_data).
        - SUCCESS: new token_data is returned
        - AUTH_FAILURE: token was evicted from Redis
        - TRANSIENT_ERROR: token kept for retry, returns original token_data
    """
    lock = _user_github_tokens_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        # Re-fetch under lock to avoid race
        current_data = await get_github_token_data(user_id)
        if not current_data or not current_data.is_access_token_expired() or not current_data.can_refresh():
            # Token was refreshed by another caller, or no longer needs refresh
            return RefreshResult.SUCCESS, current_data

        result, refreshed = await refresh_github_token(
            user_id=user_id,
            client_id=GITHUB_APP_CLIENT_ID or "",
            client_secret=GITHUB_APP_CLIENT_SECRET,
            token_data=current_data,
        )
        if result == RefreshResult.SUCCESS and refreshed:
            return RefreshResult.SUCCESS, refreshed
        if result == RefreshResult.AUTH_FAILURE:
            # Token is invalid/revoked - evict it
            await remove_github_token(user_id)
            logger.info("GitHub token refresh auth failure, removed from Redis", user_id=user_id)
            return RefreshResult.AUTH_FAILURE, None
        # TRANSIENT_ERROR: keep token for retry
        logger.info("GitHub token refresh transient error, preserving for retry", user_id=user_id)
        return RefreshResult.TRANSIENT_ERROR, current_data


async def _get_valid_github_token(session_id: str) -> str | None:
    """Get GitHub token for the user who initiated this session.

    Looks up the current user from session context (the person asking for the PR),
    then checks if they have a valid GitHub token stored in encrypted Redis.
    If the token is expired but has a valid refresh token, attempts to refresh it.
    """
    # Get the current user from in-memory storage (set by service.py before each turn)
    user_id = await _get_current_message_user_id(session_id)
    if not user_id:
        return None

    token_data = await get_github_token_data(user_id)
    if not token_data:
        return None

    # Check if access token is expired (or about to expire) and try to refresh
    if token_data.is_access_token_expired() and token_data.can_refresh():
        logger.info("GitHub access token expired, attempting refresh", user_id=user_id)
        result, refreshed_data = await _try_refresh_token_with_lock(user_id, token_data)
        if result == RefreshResult.SUCCESS:
            token_data = refreshed_data
        elif result == RefreshResult.AUTH_FAILURE:
            _session_auth_terminal_states[session_id] = "expired"
            return None
        else:
            # TRANSIENT_ERROR: refresh failed transiently.
            # is_access_token_expired() uses a 5-minute buffer, so the token may still be
            # valid for a few more minutes. Fall back to using it if not actually expired.
            if token_data.expires_at is not None and time.time() >= token_data.expires_at:
                # Token is actually expired, can't use it
                logger.info(
                    "GitHub token refresh transient error and token actually expired",
                    user_id=user_id,
                )
                return None
            # Token is in the buffer window but still valid — continue to validation
            logger.info(
                "GitHub token refresh transient error, falling back to existing token",
                user_id=user_id,
            )

    if not token_data:
        return None

    # Validate the token with GitHub API
    is_valid, username, is_auth_failure = await _validate_github_token_async(token_data.access_token)
    if not is_valid:
        if is_auth_failure:
            # Atomically remove the token only if it's the one we just validated.
            lock = _user_github_tokens_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                current_data = await get_github_token_data(user_id)
                if current_data and current_data.access_token == token_data.access_token:
                    await remove_github_token(user_id)
            _session_auth_terminal_states[session_id] = "expired"
            logger.info("GitHub token invalid, removed from Redis", user_id=user_id)
        return None

    logger.debug("GitHub token validated", user_id=user_id, github_user=username)
    return token_data.access_token


async def _poll_for_token(
    device_code: str, interval: int, expires_in: int, session_id: str, expected_user_id: str | None
) -> None:
    """Poll GitHub until user authorizes. Store token in encrypted Redis.

    This runs as an async background task via create_background_task().
    The task can be cancelled cleanly when the session ends.

    Args:
        expected_user_id: The Yupp user_id of the user who initiated the auth flow.
            If provided, verifies the GitHub user maps to this user.
    """
    deadline = time.time() + expires_in
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while time.time() < deadline:
                await asyncio.sleep(interval)

                # Check if session was cleaned up (task cancelled)
                if session_id not in _session_polling_tasks:
                    logger.info("Polling aborted (session cleaned up)", session_id=session_id)
                    return

                try:
                    resp = await client.post(
                        "https://github.com/login/oauth/access_token",
                        data={
                            "client_id": GITHUB_APP_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        },
                        headers={"Accept": "application/json"},
                    )
                    data = resp.json()
                except Exception as e:
                    logger.warning("Device flow poll error", error=str(e))
                    continue

                if "access_token" in data:
                    # Parse full token data including optional refresh token
                    token_data = GitHubTokenData.from_oauth_response(data)

                    # Validate token to get GitHub username
                    is_valid, github_username, _ = await _validate_github_token_async(token_data.access_token)
                    if not is_valid or not github_username:
                        logger.warning("Token obtained but validation failed", session_id=session_id)
                        _session_auth_terminal_states[session_id] = AUTH_STATE_ERROR
                        return

                    # Map GitHub username to internal user_id via users.github_username.
                    user_id = await get_user_id_by_github_username(github_username)
                    if not user_id:
                        logger.warning("Could not map GitHub user to user_id", github_username=github_username)
                        _session_auth_terminal_states[session_id] = AUTH_STATE_ERROR
                        return

                    # Verify user_id matches expected user (if provided)
                    if expected_user_id and user_id != expected_user_id:
                        logger.warning(
                            "GitHub user mapped to different Yupp user than expected",
                            github_username=github_username,
                            mapped_user_id=user_id,
                            expected_user_id=expected_user_id,
                        )
                        _session_auth_terminal_states[session_id] = AUTH_STATE_ERROR
                        return

                    # Store the token data in encrypted Redis under lock to avoid race with removal
                    lock = _user_github_tokens_locks.setdefault(user_id, asyncio.Lock())
                    async with lock:
                        stored = await store_github_token_data(user_id, token_data)
                    if stored:
                        logger.info(
                            "GitHub token stored in Redis",
                            user_id=user_id,
                            github_user=github_username,
                            has_refresh_token=token_data.refresh_token is not None,
                            expires_at=token_data.expires_at,
                        )
                        _session_auth_terminal_states.pop(session_id, None)
                    else:
                        # Storage disabled (no encryption key) - set terminal state
                        logger.warning(
                            "GitHub token storage disabled; auth succeeded but token not persisted",
                            user_id=user_id,
                            github_user=github_username,
                        )
                        _session_auth_terminal_states[session_id] = AUTH_STATE_ERROR
                    return

                if data.get("error") == "slow_down":
                    interval = data.get("interval", interval + 5)
                    continue

                error = data.get("error")
                if error != "authorization_pending":
                    # Terminal error - store state so check_github_auth_status can report it
                    if error == "expired_token":
                        _session_auth_terminal_states[session_id] = AUTH_STATE_EXPIRED
                    elif error == "access_denied":
                        _session_auth_terminal_states[session_id] = AUTH_STATE_DENIED
                    else:
                        _session_auth_terminal_states[session_id] = AUTH_STATE_ERROR
                    logger.warning("Device flow failed", error=error, session_id=session_id)
                    return

        # Deadline reached without authorization
        _session_auth_terminal_states[session_id] = AUTH_STATE_EXPIRED
        logger.info("Device flow expired (deadline reached)", session_id=session_id)
    except asyncio.CancelledError:
        logger.info("Polling cancelled (session ended)", session_id=session_id)
        raise  # Re-raise to let create_background_task handle it
    finally:
        # Remove task from tracking dict
        _session_polling_tasks.pop(session_id, None)


async def _initiate_device_flow(session_id: str, user_id: str | None) -> dict[str, str]:
    """Initiate GitHub device flow and start polling.

    Args:
        session_id: The harness session ID.
        user_id: The Yupp user_id to verify against (if provided).

    Returns:
        Dict with status, verification_uri, user_code, and instructions.
    """
    if not GITHUB_APP_CLIENT_ID:
        return {
            "status": "error",
            "error": "GitHub App Client ID not configured. Cannot create user-attributed PRs.",
        }

    # Check if polling is already in progress for this session
    existing_task = _session_polling_tasks.get(session_id)
    if existing_task and not existing_task.done():
        return {
            "status": "pending",
            "message": "Authorization already in progress. Use check_github_auth_status to monitor.",
        }

    # Clear any previous terminal state when starting a new auth attempt
    _session_auth_terminal_states.pop(session_id, None)

    # Request device code from GitHub
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://github.com/login/device/code",
                data={"client_id": GITHUB_APP_CLIENT_ID},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.error("Device flow initiation failed", exc_info=True)
        return {"status": "error", "error": "Failed to initiate device flow due to an internal error."}

    if "error" in data:
        return {"status": "error", "error": data.get("error_description", data["error"])}

    # Start background polling task (token stored in encrypted Redis, never returned to agent)
    # Pass user_id to verify the GitHub account maps to the expected Yupp user
    task = create_background_task(
        _poll_for_token(data["device_code"], data["interval"], data["expires_in"], session_id, user_id)
    )
    _session_polling_tasks[session_id] = task

    # Only return what's safe to show the user
    return {
        "status": "pending",
        "verification_uri": data["verification_uri"],  # https://github.com/login/device
        "user_code": data["user_code"],  # e.g. "ABCD-1234"
        "expires_in_seconds": str(data["expires_in"]),
        "instructions": (f"Ask the user to visit {data['verification_uri']} and enter code: {data['user_code']}"),
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="authorize_github_user",
    description=(
        "Initiate GitHub device flow so the PR is attributed to the user instead of the bot. "
        "Returns a verification URL and code for the user to enter in their browser. "
        "Once authorized, subsequent create_pr calls in this session will be attributed to the user. "
        "The user has about 15 minutes to complete authorization."
    ),
)
async def authorize_github_user(session_id: str) -> dict[str, str]:
    """Initiate GitHub device flow so the PR is attributed to the user.

    Args:
        session_id: Your harness session ID (provided in the system prompt).

    Returns:
        Dict with verification_uri, user_code, and instructions.
    """
    logger.info("MCP tool: authorize_github_user", session_id=session_id)
    _validate_session_id(session_id)

    # Check if the current user already has a valid token (cross-session reuse from Redis)
    user_id = await _get_current_message_user_id(session_id)
    if user_id:
        existing_data = await get_github_token_data(user_id)
        if existing_data:
            # Try to refresh if expired but has refresh token
            if existing_data.is_access_token_expired() and existing_data.can_refresh():
                logger.info("Existing token expired, attempting refresh", user_id=user_id)
                result, refreshed_data = await _try_refresh_token_with_lock(user_id, existing_data)
                if result == RefreshResult.SUCCESS:
                    existing_data = refreshed_data
                elif result == RefreshResult.AUTH_FAILURE:
                    existing_data = None  # Will fall through to device flow
                elif result == RefreshResult.TRANSIENT_ERROR:
                    # Refresh failed transiently but refresh token is still valid.
                    # Don't start a new device flow — token will be refreshed on next use.
                    return {
                        "status": "already_authorized",
                        "message": "GitHub token refresh temporarily unavailable, will retry automatically.",
                    }

            # Validate the token (may have been refreshed above)
            if existing_data and not existing_data.is_access_token_expired():
                is_valid, github_username, is_auth_failure = await _validate_github_token_async(
                    existing_data.access_token
                )
                if is_valid:
                    return {
                        "status": "already_authorized",
                        "message": f"GitHub authorization already exists (GitHub username: {github_username}).",
                    }
                if is_auth_failure:
                    # Atomically remove the token only if it's the one we just validated.
                    lock = _user_github_tokens_locks.setdefault(user_id, asyncio.Lock())
                    async with lock:
                        current_data = await get_github_token_data(user_id)
                        if current_data and current_data.access_token == existing_data.access_token:
                            await remove_github_token(user_id)
                    logger.info("Existing token invalid, removed from Redis", user_id=user_id)

    return await _initiate_device_flow(session_id, user_id)


@mcp.tool(
    name="check_github_auth_status",
    description=(
        "Check if GitHub authorization has been completed for this session. "
        "Use this after calling authorize_github_user to check if the user "
        "has finished authorizing. Returns 'authorized', 'pending', 'expired', 'denied', or 'error'. "
        "If status is 'expired' or 'denied', call authorize_github_user again to restart. "
        "When status is 'authorized', also returns github_username, github_name, and github_email "
        "for use in git commit authorship configuration."
    ),
)
async def check_github_auth_status(session_id: str) -> dict[str, str]:
    """Check if GitHub authorization has been completed.

    Args:
        session_id: Your harness session ID (provided in the system prompt).

    Returns:
        Dict with status ('authorized', 'pending', 'expired', 'denied', or 'error').
        When authorized, also includes github_username, github_name, and github_email.
    """
    logger.info("MCP tool: check_github_auth_status", session_id=session_id)
    _validate_session_id(session_id)

    # Check if the current user has a valid token in Redis
    user_id = await _get_current_message_user_id(session_id)
    if user_id:
        token_data = await get_github_token_data(user_id)
        is_authorized = False

        # If token is valid and not expired, we're authorized
        if token_data and not token_data.is_access_token_expired():
            is_authorized = True
        # If expired but can refresh, try to refresh before declaring authorized
        elif token_data and token_data.can_refresh():
            refresh_result, refreshed_data = await _try_refresh_token_with_lock(user_id, token_data)
            if refresh_result == RefreshResult.SUCCESS and refreshed_data:
                token_data = refreshed_data
                is_authorized = True
            # On auth failure or transient error, token_data may be stale/invalid

        if is_authorized and token_data:
            result: dict[str, str] = {"status": "authorized"}
            # Fetch GitHub user info for commit authorship
            user_info = await _get_github_user_info_async(token_data.access_token)
            if user_info:
                if user_info.get("github_username"):
                    result["github_username"] = user_info["github_username"]  # type: ignore[assignment]
                if user_info.get("github_name"):
                    result["github_name"] = user_info["github_name"]  # type: ignore[assignment]
                if user_info.get("github_email"):
                    result["github_email"] = user_info["github_email"]  # type: ignore[assignment]
            return result

    # Check for terminal states
    terminal_state = _session_auth_terminal_states.get(session_id)
    if terminal_state:
        return {"status": terminal_state}

    return {"status": "pending"}
