"""In-process HTTP MCP server for Agent Harness Service.

Provides harness-level operations that agents can invoke as tools
during a Claude Code session. Mounted inside the AHS FastAPI server
at /mcp/harness via streamable-http transport — no subprocess needed.

Tools:
- request_write_access: Create a git worktree for write access
- list_available_repos: List available repos
- create_pr: Push branch and open a PR
- authorize_github_user: Initiate GitHub device flow for user-attributed PRs
- check_github_auth_status: Check if GitHub authorization is complete
- request_feedback: Post a feedback survey to Slack
- send_slack_message: Send a proactive message to a Slack channel
- new_task: Spawn a subagent in a new session (v2)
- route_model: Pick executor model(s) for upcoming tasks (v2)
- list_agents: List available agent configs (v2)
- schedule_agent_call: Schedule a one-time agent call
- schedule_recurring_agent_call: Schedule a recurring agent call
- linear_list_teams: List Linear teams/projects
- linear_get_workflow_states: Get workflow states for a team
- linear_get_issue: Fetch a single Linear issue
- linear_search_issues: Search Linear issues with filters
- linear_create_issue: Create a new Linear issue
- linear_update_issue: Update an existing Linear issue

Session context is passed as a tool parameter (session_id) by the agent.
The runner injects the session_id into the system prompt so the agent
knows which value to pass.
"""

import asyncio
import os
import re
import time
import uuid as _uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import File, Image
from sqlalchemy import text
from sqlmodel import select

from ypl.agent_harness_service.common.agent_registry import get_agent_spec, list_predefined_agents
from ypl.agent_harness_service.common.config import load_agent_config
from ypl.agent_harness_service.common.constants import (
    AHS_SESSIONS_DIR,
    MAX_WEBSEARCH_CALLS_PER_SESSION,
    MAX_WEBSEARCH_CALLS_PER_TURN,
    SESSION_INFRA_DIRS,
    is_personal_agent,
    mcp_session_id_var,
)
from ypl.agent_harness_service.tools.github_token_storage import (
    GitHubTokenData,
    RefreshResult,
    get_github_token_data,
    refresh_github_token,
    remove_github_token,
    store_github_token_data,
)
from ypl.agent_harness_service.tools.repo_manager import (
    create_worktree,
    push_and_create_pr,
)
from ypl.agent_harness_service.tools.repo_manager import (
    list_repos as _list_repos,
)
from ypl.agent_harness_service.tools.workspace_tools import (
    BinaryFileResult,
    edit_file,
    fetch_url,
    get_command_handler_manager,
    list_files,
    read_file,
    run_command,
    search_files,
    search_web,
    write_file,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica
from ypl.backend.llm.yuppster_helpers import github_username_to_yupp_user_id
from ypl.backend.utils.async_utils import create_background_task
from ypl.db.agent_harness import (
    Agent,
    AgentProject,
    AgentScheduleType,
    AgentSession,
)
from ypl.mcp_common.scheduled_agent_call_helpers import (
    compute_next_run_for_cron,
    create_agent_schedule,
    parse_execute_at,
    parse_schedule_context,
    resolve_yuppster_from_context,
    validate_cron_expression,
    validate_timezone,
)
from ypl.structured_logger import get_logger

logger = get_logger()

mcp = FastMCP("harness")

# ---------------------------------------------------------------------------
# Dependency injection: orchestration callbacks registered by server.py at
# startup. This avoids tools/ importing from the root wiring layer.
# ---------------------------------------------------------------------------

_run_subagent_fn: Callable[..., Any] | None = None
_route_model_stub_fn: Callable[..., list[str]] | None = None


def register_orchestration_callbacks(
    run_subagent: Callable[..., Any],
    route_model_stub: Callable[..., list[str]],
) -> None:
    """Called by server.py at startup to wire orchestration functions."""
    global _run_subagent_fn, _route_model_stub_fn
    _run_subagent_fn = run_subagent
    _route_model_stub_fn = route_model_stub


# Session sandbox registry: maps session_id → stack of bwrap_enabled values.
# Stack-based so that nested scopes (parent agent → subagent) don't clobber each other:
# service.py pushes the parent setting, orchestration.py pushes the subagent setting,
# and each pops on cleanup — restoring the parent's value automatically.
_session_sandbox: dict[str, list[bool]] = {}

# Per-turn and per-session websearch call counters.
_turn_websearch_count: dict[str, int] = {}
_session_websearch_count: dict[str, int] = {}
_session_websearch_locks: dict[str, asyncio.Lock] = {}


def set_session_sandbox(session_id: str, bwrap_enabled: bool) -> None:
    """Push a bwrap_enabled value onto the session's sandbox stack."""
    _session_sandbox.setdefault(session_id, []).append(bwrap_enabled)


def clear_session_sandbox(session_id: str) -> None:
    """Pop the top sandbox entry for a session. Remove key when stack is empty."""
    stack = _session_sandbox.get(session_id)
    if stack:
        stack.pop()
        if not stack:
            del _session_sandbox[session_id]


def reset_turn_websearch_count(session_id: str) -> None:
    """Reset the per-turn websearch counter for a new turn."""
    _turn_websearch_count.pop(session_id, None)


def clear_session_websearch_count(session_id: str) -> None:
    """Remove all websearch counters and lock for a session."""
    _turn_websearch_count.pop(session_id, None)
    _session_websearch_count.pop(session_id, None)
    _session_websearch_locks.pop(session_id, None)


def _validate_session_id(session_id: str) -> str:
    """Validate that session_id is a proper UUID to prevent path traversal."""
    if not session_id:
        raise ValueError("session_id is required")
    try:
        _uuid.UUID(session_id)
    except ValueError:
        raise ValueError(f"session_id is not a valid UUID: {session_id!r}") from None
    return session_id


# --- Existing tools ---


@mcp.tool(
    name="request_write_access",
    description=(
        "Request write access to a repository by creating a git worktree. "
        "Creates an isolated worktree for the current session. The new workspace "
        "will be available on your next turn (after the current turn completes). "
        "Use list_available_repos to see available repos first."
    ),
)
def request_write_access(session_id: str, repo: str, branch: str | None = None) -> dict[str, str]:
    """Request write access to a repo by creating a git worktree.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        repo: Repository name (e.g., 'yupp-mind').
        branch: Optional branch name. Defaults to 'agent/{session_id}'.

    Returns:
        Dict with workspace path, branch name, and status.
    """
    logger.info("MCP tool: request_write_access", session_id=session_id, repo=repo, branch=branch)
    _validate_session_id(session_id)
    return create_worktree(repo=repo, session_id=session_id, branch=branch)


@mcp.tool(
    name="list_available_repos",
    description=(
        "List all available repositories that can be used with request_write_access "
        "and create_pr. Returns each repo's name and filesystem path."
    ),
)
def list_available_repos() -> list[dict[str, str]]:
    """List all available repositories.

    Returns:
        List of dicts with repo name and path for each available repo.
    """
    logger.info("MCP tool: list_available_repos")
    return _list_repos()


@mcp.tool(
    name="create_pr",
    description=(
        "Push the current branch and create a pull request. "
        "Pushes all committed changes on the session's worktree branch and "
        "opens a PR against the specified base branch (or repository default) via the GitHub CLI. "
        "PRs are created in draft mode by default. "
        "You must call request_write_access first to create a worktree. "
        "If the session has multiple worktrees, specify the 'repo' parameter. "
        "For stacked PRs, specify 'base' as the parent branch name."
    ),
)
async def create_pr(
    session_id: str,
    title: str,
    body: str,
    repo: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    draft: bool = True,
) -> dict[str, str]:
    """Push the current branch and create a pull request.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        title: PR title.
        body: PR description body.
        repo: Repository name. Required if the session has multiple worktrees.
        branch: Branch name override. Defaults to the current branch in the workspace.
        base: Base branch for the PR (e.g., parent branch for stacked PRs).
            Defaults to the repository's default branch (main).
        draft: If True, create the PR in draft mode. Defaults to True.

    Returns:
        Dict with PR URL, branch name, and status.
    """
    logger.info(
        "MCP tool: create_pr", session_id=session_id, title=title, repo=repo, branch=branch, base=base, draft=draft
    )
    _validate_session_id(session_id)

    workspace_base = os.path.join(AHS_SESSIONS_DIR, session_id)
    if not os.path.isdir(workspace_base):
        return {"status": "error", "error": "No worktree found. Call request_write_access first."}

    # Find worktree directories: skip symlinks (repo references), dotfiles, and infrastructure dirs
    worktree_entries = sorted(
        e
        for e in os.listdir(workspace_base)
        if not e.startswith(".")
        and e not in SESSION_INFRA_DIRS
        and os.path.isdir(os.path.join(workspace_base, e))
        and not os.path.islink(os.path.join(workspace_base, e))
    )
    if not worktree_entries:
        return {"status": "error", "error": "No worktree found. Call request_write_access first."}

    if repo:
        # Match worktrees starting with "{repo}-" (new slug-suffixed naming)
        matching = [e for e in worktree_entries if e.startswith(f"{repo}-")]
        if not matching:
            return {"status": "error", "error": f"No worktree found for repo '{repo}'. Available: {worktree_entries}"}
        if len(matching) > 1:
            return {"status": "error", "error": f"Multiple worktrees for '{repo}': {matching}. Specify exact name."}
        workspace = os.path.join(workspace_base, matching[0])
    elif len(worktree_entries) == 1:
        workspace = os.path.join(workspace_base, worktree_entries[0])
    else:
        return {
            "status": "error",
            "error": f"Multiple worktrees found: {worktree_entries}. Specify 'repo' parameter.",
        }

    # Get user GitHub token from memory (if authorized via device flow and not expired)
    user_github_token = await _get_valid_github_token(session_id)

    if not user_github_token:
        # No valid token - initiate device flow and return error with auth details
        user_id = await _get_current_message_user_id(session_id)
        device_flow_result = await _initiate_device_flow(session_id, user_id)

        if device_flow_result.get("status") == "error":
            return {
                "status": "error",
                "error": f"GitHub authorization required but failed to initiate: {device_flow_result.get('error')}",
            }

        # Polling already in progress (e.g. from a prior create_pr or authorize_github_user call):
        # _initiate_device_flow returns {"status": "pending", "message": "..."} with no verification_uri.
        # Return a clear "check status and retry" message rather than falling through with empty strings.
        if "verification_uri" not in device_flow_result:
            return {
                "status": "error",
                "error": "GitHub authorization required to create PRs.",
                "auth_required": "true",
                "message": device_flow_result.get("message", "Authorization already in progress."),
                "next_step": "Call check_github_auth_status to verify, then retry create_pr.",
            }

        return {
            "status": "error",
            "error": "GitHub authorization required to create PRs.",
            "auth_required": "true",
            "verification_uri": device_flow_result.get("verification_uri", ""),
            "user_code": device_flow_result.get("user_code", ""),
            "expires_in_seconds": device_flow_result.get("expires_in_seconds", ""),
            "instructions": device_flow_result.get("instructions", ""),
            "next_step": (
                "After the user completes authorization, call check_github_auth_status to verify, then retry create_pr."
            ),
        }

    return push_and_create_pr(
        workspace=workspace,
        title=title,
        body=body,
        branch=branch,
        base=base,
        user_github_token=user_github_token,
        draft=draft,
    )


# GitHub App credentials for device flow and token refresh (set in environment)
GITHUB_APP_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "")
# Client secret is needed for refreshing tokens when "User-to-server token expiration" is enabled
GITHUB_APP_CLIENT_SECRET = os.environ.get("AHS_GITHUB_APP_CLIENT_SECRET", "")

# GitHub tokens are stored in encrypted Redis via github_token_storage module.
# This enables cross-session token reuse for the same user with persistence across restarts.
# Local asyncio locks prevent race conditions within a single AHS instance.
# TODO: For multi-instance deployments, consider distributed locking (e.g., Redis SETNX).
_user_github_tokens_locks: dict[str, asyncio.Lock] = {}

# Track terminal auth states per session.
# These are session-specific since they relate to the current auth attempt.
AUTH_STATE_EXPIRED = "expired"
AUTH_STATE_DENIED = "denied"
AUTH_STATE_ERROR = "error"
_session_auth_terminal_states: dict[str, str] = {}

# Track active polling tasks per session.
_session_polling_tasks: dict[str, asyncio.Task[None]] = {}

# Track the Yupp user_id of the user who sent the most recent message in each session.
# Set by service.py before each agent turn, read by MCP tools to identify the requester.
# In-memory to avoid DB race conditions with concurrent messages.
_session_current_user: dict[str, str] = {}


def set_session_current_user(session_id: str, user_id: str) -> None:
    """Set the current message sender for a session. Called before each turn."""
    _session_current_user[session_id] = user_id


async def _get_current_message_user_id(session_id: str) -> str | None:
    """Get the Yupp user_id of the user who sent the most recent message in this session.

    Reads from in-memory storage set by service.py before each turn.
    Falls back to session creator if not found (e.g., for older sessions).
    """
    user_id = _session_current_user.get(session_id)
    if user_id:
        return user_id

    # Fallback: look up session creator from DB
    # TODO: For subagent sessions, propagate the parent session's current_user_id to avoid
    # misattribution when subagent calls create_pr in a multi-user thread (see PR #10877).
    logger.warning("current_message_user_id not in memory, falling back to session creator", session_id=session_id)
    try:
        session_uuid = _uuid.UUID(session_id)
    except ValueError:
        logger.warning("session_id is not a valid UUID, cannot look up creator", session_id=session_id)
        return None
    async with get_async_session() as db:
        result = await db.execute(
            select(AgentSession.creator_user_id).where(AgentSession.agent_session_id == session_uuid)
        )
        return result.scalar_one_or_none()


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


def clear_session_state(session_id: str) -> None:
    """Clear all session-specific state when session ends.

    Cleans up auth terminal states, current user tracking, and polling tasks.
    Note: User GitHub tokens persist across sessions (keyed by user_id, not session_id).
    """
    _session_auth_terminal_states.pop(session_id, None)
    _session_current_user.pop(session_id, None)
    task = _session_polling_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()


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

                    # Map GitHub username to Yupp user_id
                    user_id = await github_username_to_yupp_user_id(github_username)
                    if not user_id:
                        logger.warning("Could not map GitHub user to Yupp user_id", github_username=github_username)
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


async def _resolve_parent_session(harness_session_id: str) -> dict[str, Any]:
    """Look up parent session metadata for subagent spawning.

    Args:
        harness_session_id: The harness session UUID.

    Returns:
        Dict with agent_name, model, workspace, and permissions (any may be None).
    """
    from ypl.backend.db import get_async_session

    async with get_async_session() as db:
        result = await db.execute(
            text(
                "SELECT a.name, s.model, s.workspace, s.context FROM agent_sessions s "
                "JOIN agents a ON s.agent_id = a.agent_id "
                "WHERE s.agent_session_id = :sid"
            ),
            {"sid": harness_session_id},
        )
        row = result.fetchone()
        if not row:
            return {"agent_name": None, "model": None, "workspace": None, "permissions": None}
        context = row[3] or {}
        permissions: dict[str, Any] | None = context.get("permissions")
        subagent_depth: int = context.get("subagent_depth", 0)
        return {
            "agent_name": row[0],
            "model": row[1],
            "workspace": row[2],
            "permissions": permissions,
            "subagent_depth": subagent_depth,
        }


async def _resolve_slack_session_id(harness_session_id: str) -> str | None:
    """Look up the Slack session ID for a harness session UUID.

    Args:
        harness_session_id: The harness session UUID.

    Returns:
        The slack_session_id (channel:thread_ts:app_id) or None.
    """
    from ypl.backend.db import get_async_session

    async with get_async_session() as db:
        result = await db.execute(
            text("SELECT slack_session_id FROM agent_sessions WHERE agent_session_id = :sid"),
            {"sid": harness_session_id},
        )
        row = result.fetchone()
        return row[0] if row else None


@mcp.tool(
    name="request_feedback",
    description=(
        "Request user feedback on the current session. Posts a short survey "
        "to the Slack thread asking the user to rate the experience (Good/OK/Bad) "
        "with optional comments. Use this when your work was complex, nuanced, "
        "when the conversation was long and deep, or when you think user feedback would be valuable. "
        "Only works for Slack sessions."
    ),
)
def request_feedback(session_id: str) -> dict[str, str]:
    """Request a feedback survey be posted to the Slack thread.

    Args:
        session_id: Your harness session ID (provided in the system prompt).

    Returns:
        Dict with status and optional error message.
    """
    from ypl.agent_harness_service.gateway import GatewayRegistry

    logger.info("MCP tool: request_feedback", session_id=session_id)
    _validate_session_id(session_id)

    # Resolve harness UUID → Slack composite session_id (channel:thread_ts:app_id)
    slack_session_id = asyncio.run(_resolve_slack_session_id(session_id))

    if not slack_session_id:
        return {"status": "error", "error": "No Slack session found for this harness session"}

    gateway = GatewayRegistry.get_instance().get("slack")
    if not gateway:
        return {"status": "error", "error": "Slack gateway not registered"}

    try:
        success = asyncio.run(gateway.request_feedback(slack_session_id))
        if not success:
            return {"status": "error", "error": "Gateway rejected feedback request"}
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(
    name="ask_question",
    description=(
        "Ask the user a multiple-choice question in the Slack thread. "
        "Renders a question with clickable answer buttons so the user can respond "
        "without typing. The selected choice (or a typed reply) is returned to you "
        "as the next message in the conversation. "
        "Use this to gather structured input, clarify intent, or present options — "
        "for example: asking which environment to deploy to, or which topic to focus on. "
        "Only works for Slack sessions. Max 5 choices. "
        "Set allow_free_text=True (default) to show a hint that the user can also type a custom answer."
    ),
)
def ask_question(
    session_id: str,
    question_id: str,
    text: str,
    choices: list[str],
    allow_free_text: bool = True,
) -> dict[str, str]:
    """Post a multiple-choice question to the Slack thread.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        question_id: A short alphanumeric identifier for this question (e.g. "deploy_env", "topic").
                     Used internally to route the response; not shown to the user.
        text: The question text shown to the user (Slack mrkdwn supported).
        choices: List of choice labels (plain text, max 5). Each becomes a button.
        allow_free_text: If True (default), show a hint that the user can type a free answer.

    Returns:
        Dict with status and optional error message.
    """
    from ypl.agent_harness_service.gateway import GatewayRegistry

    logger.info("MCP tool: ask_question", session_id=session_id, question_id=question_id, num_choices=len(choices))
    _validate_session_id(session_id)

    if not choices:
        return {"status": "error", "error": "choices must not be empty"}
    if len(choices) > 5:
        return {"status": "error", "error": "choices must have at most 5 items (Slack limit)"}

    # Resolve harness UUID → Slack composite session_id (channel:thread_ts:app_id)
    slack_session_id = asyncio.run(_resolve_slack_session_id(session_id))

    if not slack_session_id:
        return {"status": "error", "error": "No Slack session found for this harness session"}

    gateway = GatewayRegistry.get_instance().get("slack")
    if not gateway:
        return {"status": "error", "error": "Slack gateway not registered"}

    # Convert plain-text choice labels into the {label, value} dicts SAG expects
    choice_dicts = [{"label": c, "value": c} for c in choices]

    try:
        success = asyncio.run(
            gateway.send_questionnaire(
                session_id=slack_session_id,
                question_id=question_id,
                text=text,
                choices=choice_dicts,
                allow_free_text=allow_free_text,
            )
        )
        if not success:
            return {"status": "error", "error": "Gateway rejected questionnaire request"}
        return {"status": "ok", "message": "Question posted. Awaiting user response."}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(
    name="send_slack_message",
    description=(
        "Send a proactive message to a Slack channel. Use this to notify users, "
        "post updates, or initiate conversations in Slack channels where your agent "
        "has access. Unlike replying in threads, this creates a new message or "
        "replies in an existing thread by specifying thread_ts. "
        "Your agent must have a Slack presence configured to use this tool. "
        "IMPORTANT: The text parameter must be in Slack mrkdwn format, NOT standard Markdown. "
        "Key differences: bold is *text*, italic is _text_, links are <url|label>, "
        "code blocks use ``` with no language tag, no headings (#), no tables, no numbered lists. "
        "Pass project_id to auto-route to the project's updates thread — no manual thread_ts lookup needed. "
        "If channel is also omitted, the project's slack_channel is used automatically. "
        "NOTE: In Slack sessions, your text output is automatically relayed to Slack by the harness — "
        "only call this tool when explicitly instructed to post to a specific channel or thread. "
        "If you do use it, do not also produce text output with the same content — the harness will relay both, "
        "causing the user to see duplicates."
    ),
)
async def send_slack_message(
    text: str,
    channel: str | None = None,
    thread_ts: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    """Send a message to a Slack channel.

    Args:
        text: Message content in Slack mrkdwn format (not standard Markdown).
        channel: Slack channel name or ID (e.g., 'alert-backend', '#general', or 'C123ABC').
            Optional when project_id is given — the project's slack_channel is used instead.
        thread_ts: Optional thread timestamp to reply in an existing thread.
            Optional when project_id is given — the project's updates_thread_ts is used instead.
        project_id: AHS project UUID. When provided and thread_ts is omitted, the tool
            auto-routes to the project's updates thread (reads updates_thread_ts from
            project shared_state). Also provides the channel if channel is omitted.
        session_id: The calling agent's harness session ID, used to resolve the agent name.

    Returns:
        Dict with status, message_ts, channel, and optional error message.
    """
    import uuid as _uuid_mod

    from ypl.agent_harness_service.gateway import GatewayRegistry

    # Prefer session_id from the HTTP header (injected by runner into .mcp.json).
    # Fall back to the tool parameter (passed by LLM) for backward compatibility.
    effective_session_id = mcp_session_id_var.get() or session_id
    if not effective_session_id:
        return {"status": "error", "error": "session_id is required", "channel": channel or ""}
    _validate_session_id(effective_session_id)

    effective_channel = channel
    effective_thread_ts = thread_ts

    # If project_id given: auto-resolve channel and/or updates_thread_ts.
    if project_id:
        try:
            async with get_async_session() as db:
                project = await db.get(AgentProject, _uuid_mod.UUID(project_id))
            if project:
                channel_from_project = not effective_channel and bool(project.slack_channel)
                if channel_from_project:
                    effective_channel = project.slack_channel
                # Only auto-fill thread_ts when channel was also resolved from the project,
                # otherwise we'd try to reply in a thread that belongs to a different channel.
                if not effective_thread_ts and (channel_from_project or effective_channel == project.slack_channel):
                    effective_thread_ts = (project.shared_state or {}).get("updates_thread_ts")
        except Exception:
            logger.warning(
                "send_slack_message: failed to resolve project info (continuing without)",
                project_id=project_id,
                exc_info=True,
            )

    if not effective_channel:
        return {"status": "error", "error": "channel is required (or pass project_id to auto-resolve)", "channel": ""}

    logger.info(
        "MCP tool: send_slack_message",
        session_id=effective_session_id,
        channel=effective_channel,
        text_length=len(text),
        in_thread=effective_thread_ts is not None,
        project_id=project_id,
    )

    # Resolve harness UUID → agent name
    parent_info = await _resolve_parent_session(effective_session_id)
    agent_name = parent_info.get("agent_name")

    if not agent_name:
        return {"status": "error", "error": "Could not resolve agent name from session"}

    # Call gateway to send the message
    gateway = GatewayRegistry.get_instance().get("slack")
    if not gateway:
        return {"status": "error", "error": "Slack gateway not registered", "channel": effective_channel}

    result = await gateway.send_message(
        agent_name=agent_name,
        destination=effective_channel,
        text=text,
        thread_id=effective_thread_ts,
        ahs_session_id=effective_session_id,
    )

    if not result.success:
        return {
            "status": "error",
            "error": result.error or "Unknown error",
            "channel": effective_channel,
        }

    returned_channel = result.destination or effective_channel

    # Persist the thread mapping to AgentSession.context["registered_threads"] (best-effort).
    # This makes thread→session mappings queryable from the DB, not just SAG Redis.
    # Only record top-level threads (not replies) to stay in sync with SAG semantics.
    # The root thread TS is effective_thread_ts when replying, else result.message_id
    # (the new message is itself the thread root).
    root_thread_ts = effective_thread_ts or result.message_id
    if root_thread_ts and not effective_thread_ts:
        # Only persist new top-level threads; replies don't create new thread→session mappings.
        try:
            import datetime

            async with get_async_session() as db:
                db_session = await db.get(AgentSession, _uuid_mod.UUID(effective_session_id))
                if db_session is not None:
                    ctx = dict(db_session.context or {})
                    registered: list[dict] = list(ctx.get("registered_threads", []))
                    registered.append(
                        {
                            "channel_id": returned_channel,
                            "thread_ts": root_thread_ts,
                            "registered_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        }
                    )
                    ctx["registered_threads"] = registered
                    db_session.context = ctx
                    await db.commit()
        except Exception:
            logger.warning(
                "send_slack_message: failed to persist registered_thread to DB (non-fatal)",
                session_id=effective_session_id,
                exc_info=True,
            )

    return {
        "status": "ok",
        "message_ts": result.message_id or "",
        "channel": returned_channel,
    }


# --- Orchestration tools ---


@mcp.tool(
    name="new_task",
    description=(
        "Spawn a subagent in a new session. The subagent runs asynchronously — "
        "this call returns immediately with a session_id and status='spawned'. "
        "The subagent's result will arrive as a follow-up message in this session "
        "once it completes. Use this to delegate tasks to specialized agents "
        "(e.g., reviewer, fixer, coordinator, sre). "
        "Multiple new_task calls in a single response execute in parallel."
    ),
)
async def new_task(
    agent_type: str,
    prompt: str,
    description: str = "",
    model: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Spawn a subagent asynchronously and return immediately.

    Args:
        agent_type: Which agent config to use (e.g., 'reviewer', 'fixer', 'coordinator').
            Available agents can be listed with list_agents.
        prompt: The task for the subagent to perform.
        description: Short (3-5 words) description for tracking.
        model: Optional model override (e.g., 'anthropic/claude-sonnet-4-6').
            If not specified, uses the agent config's default model.
        session_id: The calling agent's harness session ID, used to link parent-child sessions.

    Returns:
        Dict with session_id, agent_name, model, status='spawned', and a note explaining
        that the result will arrive as a follow-up message.
    """
    # Prefer session_id from the HTTP header (injected by runner into .mcp.json).
    # Fall back to the tool parameter (passed by LLM) for backward compatibility.
    effective_session_id = mcp_session_id_var.get() or session_id

    logger.info(
        "MCP tool: new_task",
        agent_type=agent_type,
        description=description,
        model=model,
        parent_session_id=effective_session_id,
    )

    # Resolve parent session context (model, workspace, agent name, depth) from DB
    parent_info: dict[str, Any] = {
        "agent_name": None,
        "model": None,
        "workspace": None,
        "permissions": None,
        "subagent_depth": 0,
    }
    if effective_session_id:
        _validate_session_id(effective_session_id)
        parent_info = await _resolve_parent_session(effective_session_id)

    parent_model = parent_info["model"]
    parent_workspace = parent_info["workspace"]
    parent_depth: int = parent_info.get("subagent_depth", 0)

    # Enforce allowed_subagents
    parent_agent_name = parent_info["agent_name"]
    if parent_agent_name and effective_session_id:
        allowed: list[str] | None = None
        parent_spec = get_agent_spec(parent_agent_name)
        if parent_spec:
            allowed = parent_spec.allowed_subagents
        else:
            parent_config = load_agent_config(parent_agent_name)
            if parent_config:
                allowed = parent_config.allowed_subagents

        if allowed is not None and "*" not in allowed and agent_type not in allowed:
            return {
                "error": (
                    f"Agent '{parent_agent_name}' is not allowed to spawn "
                    f"subagent '{agent_type}'. Allowed: {allowed or '(none)'}."
                ),
                "status": "error",
            }

    parent_permissions = parent_info.get("permissions") if effective_session_id else None

    if _run_subagent_fn is None:
        return {
            "error": (
                "orchestration callbacks not registered"
                " — server.py must call register_orchestration_callbacks() at startup."
            ),
            "status": "error",
        }

    # Pre-generate session UUID so the caller gets it immediately
    pre_session_id = str(_uuid.uuid4())
    preview_model = model or parent_model or "anthropic/claude-sonnet-4-6"

    # Fire-and-forget: run subagent in background; result delivered via subagent queue
    _subagent_task = asyncio.ensure_future(
        _run_subagent_fn(
            agent_type=agent_type,
            prompt=prompt,
            description=description,
            model=model,
            parent_session_id=effective_session_id,
            parent_model=parent_model,
            parent_workspace=parent_workspace,
            parent_permissions=parent_permissions,
            parent_depth=parent_depth,
            pre_session_id=pre_session_id,
        )
    )
    # Prevent "Task exception was never retrieved" warnings if the coroutine raises
    # before run_subagent's own try/except (e.g. during argument validation).
    _captured_pre_session_id = pre_session_id
    _captured_agent_type = agent_type

    def _log_subagent_exception(t: asyncio.Task[None]) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "Unhandled exception in fire-and-forget subagent task",
                pre_session_id=_captured_pre_session_id,
                agent_type=_captured_agent_type,
                exc_info=t.exception(),
            )

    _subagent_task.add_done_callback(_log_subagent_exception)

    return {
        "session_id": pre_session_id,
        "agent_name": agent_type,
        "model": preview_model,
        "status": "spawned",
        "description": description or prompt[:120],
        "note": "Running asynchronously. Result will arrive as a follow-up message in this session.",
    }


@mcp.tool(
    name="route_model",
    description=(
        "Pick executor model(s) for upcoming tasks. Returns a list of model IDs "
        "in '{provider}/{model_id}' format. When count > 1, guarantees diversity "
        "(models from different providers). Use this before new_task to select "
        "which model(s) to use."
    ),
)
def route_model(
    task_description: str,
    count: int = 1,
    candidates: list[str] | None = None,
) -> list[str]:
    """Pick model(s) for a task.

    Args:
        task_description: What the task is about (e.g., 'code review', 'bug fix').
        count: How many models to return (default 1). Use count=2 for dual-review.
        candidates: Optional list to restrict selection to these models.

    Returns:
        List of model IDs (e.g., ['anthropic/claude-sonnet-4-6', 'openai/gpt-4o']).
    """
    logger.info("MCP tool: route_model", task=task_description, count=count)
    if _route_model_stub_fn is None:
        return ["ERROR: orchestration callbacks not registered"]
    return _route_model_stub_fn(task_description, count, candidates)


@mcp.tool(
    name="list_agents",
    description="List all available agent configurations. Use agent names with new_task.",
)
def list_agents() -> list[dict[str, str]]:
    """List all available agent configs.

    Returns:
        List of dicts with name, description, executor type, and model for each agent.
    """
    logger.info("MCP tool: list_agents")

    return [
        {
            "name": config.name,
            "description": config.description or "",
            "executor_type": config.executor.type,
            "model": config.executor.model or "(assigned at spawn time)",
        }
        for config in list_predefined_agents()
    ]


@mcp.tool(
    name="create_agent",
    description=(
        "Create a new personal agent. Used by creation-helper agents to set up "
        "user-specific agents (e.g., yuppclaw-alice). The agent is registered in "
        "the database and immediately available for use."
    ),
)
async def create_agent_tool(
    display_name: str,
    persona: str,
    user_id: str,
    owner_name: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Create a new personal agent.

    The agent name is auto-derived from the user's email prefix (e.g. alice@yupp.ai → yuppclaw-alice).
    Pass user_id and owner_name from the session context.

    Args:
        display_name: Human-readable display name for the agent (e.g., "Alice's Claw").
        persona: The agent's personality, vibe, and any user preferences.
        user_id: The user ID of the agent's owner (from session context).
        owner_name: The owner's display name (from session context). Falls back to 'your owner'.
        description: Optional description of the agent.

    Returns:
        Dict with success status, agent name, and instructions to share with the user.
    """
    from ypl.agent_harness_service.common.constants import PERSONAL_AGENT_DEFAULT_CONFIG, PERSONAL_AGENT_PREFIXES
    from ypl.agent_harness_service.common.types import AgentCreateRequest, ExecutorConfigRequest, SandboxConfigRequest
    from ypl.agent_harness_service.service import create_agent as service_create_agent

    if not user_id:
        return {"error": "user_id is required to create a personal agent"}

    # Resolve email prefix from user_id to derive the agent name
    try:
        async with get_async_session() as db_session:
            result = await db_session.execute(
                text("SELECT email FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            )
            row = result.first()
    except Exception:
        logger.error("Failed to look up user email for agent name", user_id=user_id, exc_info=True)
        return {"error": "Failed to look up user email"}

    if not row or not row[0]:
        return {"error": f"No email found for user_id '{user_id}'"}

    email = str(row[0])
    if not email.endswith("@yupp.ai"):
        return {"error": "Only @yupp.ai users can create personal agents"}

    email_prefix = re.sub(r"[^a-z0-9-]", "-", email.split("@")[0].lower()).strip("-")
    # Use the first personal agent prefix (yuppclaw)
    base_prefix = sorted(PERSONAL_AGENT_PREFIXES)[0]
    name = f"{base_prefix}-{email_prefix}"

    logger.info("MCP tool: create_agent", name=name, display_name=display_name, user_id=user_id)

    owner_name = owner_name or "your owner"

    # Build the additional_system_prompt with identity, owner info, and persona.
    # Default to first name for addressing; the creator agent may include a preferred
    # name in the persona if the user requests something different.
    first_name = owner_name.split()[0] if owner_name and " " in owner_name else owner_name
    additional_system_prompt = (
        f"# Personal Agent Identity\n\n"
        f"Your name is **{display_name}**. You are the personal agent of **{owner_name}**.\n"
        f"Address them as **{first_name}** unless they ask you to call them something else.\n\n"
        f"## Persona\n\n"
        f"{persona}\n"
    )

    # Use the default personal agent config template (mirrors SRE agent config).
    cfg = PERSONAL_AGENT_DEFAULT_CONFIG
    sandbox_raw = cfg.get("sandbox", {})
    assert isinstance(sandbox_raw, dict)
    exec_raw = cfg.get("executor_config", {})
    assert isinstance(exec_raw, dict)
    tool_perms = cfg.get("tool_permissions", {"*": "allow"})
    assert isinstance(tool_perms, dict)
    allowed_sub = cfg.get("allowed_subagents", [])
    assert isinstance(allowed_sub, list)
    allowed_gw = cfg.get("allowed_gateways", ["*"])
    assert isinstance(allowed_gw, list)

    request = AgentCreateRequest(
        name=name,
        user_id=user_id,
        display_name=display_name,
        description=description or f"{owner_name}'s personal agent",
        executor_config=ExecutorConfigRequest(
            type=str(exec_raw.get("type", "harnessed")),
            model=str(exec_raw.get("model", "")) or None,
        ),
        tool_permissions={str(k): str(v) for k, v in tool_perms.items()},
        allowed_subagents=[str(s) for s in allowed_sub],
        default_repo=str(cfg.get("default_repo", "yupp-agent")),
        sandbox=SandboxConfigRequest(
            enabled=bool(sandbox_raw.get("enabled", True)),
            auto_allow_bash_if_sandboxed=bool(sandbox_raw.get("autoAllowBashIfSandboxed", True)),
            bwrap_enabled=bool(sandbox_raw.get("bwrapEnabled", True)),
        ),
        max_turns=int(cfg.get("max_turns", 50)),
        max_budget_usd=float(cfg.get("max_budget_usd", 3.0)),
        feedback_probability=float(cfg.get("feedback_probability", 0.2)),
        feedback_min_turns=int(cfg.get("feedback_min_turns", 5)),
        timeout_s=int(cfg.get("timeout_s", 300)),
        allowed_gateways=[str(g) for g in allowed_gw],
        additional_system_prompt=additional_system_prompt,
    )

    try:
        create_result = await service_create_agent(request)
        return {
            "success": True,
            "name": create_result.name,
            "status": create_result.status,
            "message": (
                f"Agent '{create_result.name}' created successfully. "
                f"Tell the user their personal agent name is '{create_result.name}' "
                f"and they should start a new thread mentioning @yuppclaw to talk to it."
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("Failed to create agent", name=name, error=str(e), exc_info=True)
        return {"error": f"Failed to create agent: {e}"}


# ============================================================================
# Personal Agent Self-Management Tools
# ============================================================================


async def _check_tool_explicitly_allowed(agent_name: str, tool_name: str) -> bool:
    """Check if *tool_name* is explicitly listed as "allow" in the agent's config.

    Tries on-disk config first (fast, lru_cache), then falls back to the DB
    config JSONB for DB-only agents (e.g. bizbot, personal agents without an
    on-disk directory).

    A wildcard ``"*": "allow"`` entry is intentionally NOT treated as explicit
    permission — the tool name must appear literally in ``tool_permissions``.
    """
    cfg = load_agent_config(agent_name)
    if cfg is not None:
        return cfg.tool_permissions.get(tool_name) == "allow"

    # DB-only agent — query raw config JSONB directly.
    async with get_async_session_read_replica() as db:
        result = await db.execute(
            text("SELECT config FROM agents WHERE name = :name"),
            {"name": agent_name},
        )
        row = result.first()
        if not row or not row[0]:
            return False
        from ypl.agent_harness_service.common.models import expand_tool_permissions

        raw_perms: dict[str, Any] = row[0].get("tool_permissions", {})
        try:
            expanded = expand_tool_permissions(raw_perms)
        except ValueError:
            # Unknown toolset in DB config — fall back to raw check.
            expanded = raw_perms
        return expanded.get(tool_name) == "allow"


async def _resolve_current_personal_agent(
    session_id: str | None = None,
    tool_name: str = "",
) -> tuple[str | None, str | None]:
    """Resolve the current agent name and verify it has access to self-prompt tools.

    Access is granted if:
    - The agent is a personal agent (yuppclaw-*), OR
    - The agent explicitly has *tool_name* set to ``"allow"`` in its config
      (on-disk or DB).

    Returns:
        (agent_name, error_message) — one will be None.
    """
    effective_session_id = mcp_session_id_var.get() or session_id or ""
    if not effective_session_id:
        return None, "No session context available"

    parent_info = await _resolve_parent_session(effective_session_id)
    agent_name = parent_info.get("agent_name")
    if not agent_name:
        return None, f"Could not resolve agent for session {effective_session_id}"

    agent_name = str(agent_name)

    # Auto-allow personal agents (yuppclaw-*).
    if is_personal_agent(agent_name):
        return agent_name, None

    # Also allow if the tool is explicitly listed as "allow" in the agent's config.
    # TODO: For shared-session agents (e.g. Slack multi-user bots), add caller-level
    # authorization to verify the requester is the session creator or has a privileged
    # role before allowing system prompt reads/writes. See PR #11253.
    if tool_name and await _check_tool_explicitly_allowed(agent_name, tool_name):
        return agent_name, None

    return (
        None,
        f"This tool is only available to personal agents or agents with explicit config access, not '{agent_name}'",
    )


@mcp.tool(
    name="read_self_system_prompt",
    description=(
        "Read your own additional system prompt (persona, identity, preferences). "
        "Use this to check what instructions define your personality and behavior."
    ),
)
async def read_self_system_prompt_tool(session_id: str = "") -> dict[str, Any]:
    """Read the current agent's additional_system_prompt from the database.

    Args:
        session_id: Optional session ID override (auto-resolved from context).

    Returns:
        Dict with the agent name and current additional_system_prompt content.
    """
    agent_name, err = await _resolve_current_personal_agent(session_id, tool_name="read_self_system_prompt")
    if err:
        return {"error": err}
    assert agent_name is not None

    async with get_async_session() as db_session:
        result = await db_session.execute(
            text("SELECT additional_system_prompt FROM agents WHERE name = :name"),
            {"name": agent_name},
        )
        row = result.first()
        if not row:
            return {"error": f"Agent '{agent_name}' not found"}

    return {
        "agent_name": agent_name,
        "additional_system_prompt": row[0] or "",
    }


@mcp.tool(
    name="update_self_system_prompt",
    description=(
        "Update your own additional system prompt (persona, identity, preferences). "
        "Use this when the user asks you to change fundamental aspects of your personality, "
        "how you address them, or core interaction style. The full prompt must be provided — "
        "this replaces the existing content entirely."
    ),
)
async def update_self_system_prompt_tool(
    new_prompt: str,
    session_id: str = "",
) -> dict[str, Any]:
    """Update the current agent's additional_system_prompt in the database.

    Args:
        new_prompt: The complete new additional system prompt content.
        session_id: Optional session ID override (auto-resolved from context).

    Returns:
        Dict with success status and the agent name.
    """
    agent_name, err = await _resolve_current_personal_agent(session_id, tool_name="update_self_system_prompt")
    if err:
        return {"error": err}
    assert agent_name is not None

    if not new_prompt.strip():
        return {"error": "new_prompt cannot be empty"}

    max_prompt_length = 10_000
    if len(new_prompt) > max_prompt_length:
        return {"error": f"new_prompt exceeds maximum length ({len(new_prompt)} > {max_prompt_length} chars)"}

    logger.info(
        "MCP tool: update_self_system_prompt",
        agent_name=agent_name,
        prompt_length=len(new_prompt),
    )

    async with get_async_session() as db_session:
        result = await db_session.execute(
            text("UPDATE agents SET additional_system_prompt = :prompt WHERE name = :name RETURNING name"),
            {"prompt": new_prompt, "name": agent_name},
        )
        if not result.first():
            return {"error": f"Agent '{agent_name}' not found"}
        await db_session.commit()

    return {
        "success": True,
        "agent_name": agent_name,
        "message": f"System prompt updated for '{agent_name}'. Changes take effect on the next session.",
    }


# ============================================================================
# Agent Schedule Tools
# ============================================================================


async def _get_session_and_agent(session_id: str) -> tuple[AgentSession | None, Agent | None, str | None]:
    """Look up session and its agent. Returns (session, agent, error_message)."""
    try:
        session_uuid = _uuid.UUID(session_id)
    except ValueError:
        return None, None, f"Invalid session_id format: {session_id}"

    async with get_async_session() as db_session:
        result = await db_session.execute(select(AgentSession).where(AgentSession.agent_session_id == session_uuid))
        agent_session = result.scalar_one_or_none()
        if not agent_session:
            return None, None, f"Session not found: {session_id}"

        agent = await db_session.get(Agent, agent_session.agent_id)
        if not agent:
            return agent_session, None, f"Agent not found for session: {session_id}"

        return agent_session, agent, None


async def _resolve_creator_info(
    caller_session: AgentSession | None, caller_agent: Agent | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve creator user_id from session context and verify they are a Yuppster.

    Returns (user_id, created_by_agent, error_message).
    If successful, error_message is None.
    """
    session_context = caller_session.context or {} if caller_session else {}
    created_by_agent = caller_agent.name if caller_agent else None

    # Resolve user identity to user_id and verify Yuppster status
    user_id, error = await resolve_yuppster_from_context(session_context)
    if error:
        return None, created_by_agent, error

    return user_id, created_by_agent, None


@mcp.tool(
    name="schedule_agent_call",
    description=(
        "Schedule a one-time agent call to execute at a specific future time. "
        "Use this to delay an agent invocation. The target agent will receive "
        "the message as a prompt when the scheduled time arrives. "
        "Requires your session_id (provided in the system prompt)."
    ),
)
async def schedule_agent_call(
    session_id: str,
    target_agent_name: str,
    message: str,
    execute_at: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Schedule a one-time agent call."""
    try:
        logger.info(
            "MCP tool: schedule_agent_call",
            session_id=session_id,
            target_agent_name=target_agent_name,
            execute_at=execute_at,
        )

        # Validate session and get caller agent info
        caller_session, caller_agent, error = await _get_session_and_agent(session_id)
        if error:
            return {"success": False, "error": error}

        # Validate timezone
        tz_error = validate_timezone(timezone)
        if tz_error:
            return {"success": False, "error": tz_error}

        # Parse execute_at
        execute_at_utc, exec_error = parse_execute_at(execute_at, timezone)
        if exec_error:
            return {"success": False, "error": exec_error}

        # Parse context
        context_dict, ctx_error = parse_schedule_context(context)
        if ctx_error:
            return {"success": False, "error": ctx_error}

        # Resolve user identity and verify Yuppster status
        created_by_user, created_by_agent, user_error = await _resolve_creator_info(caller_session, caller_agent)
        if user_error:
            return {"success": False, "error": user_error}

        return await create_agent_schedule(
            agent_name=target_agent_name,
            message=message,
            schedule_type=AgentScheduleType.SCHEDULED,
            cron_timezone=timezone,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            execute_at_utc=execute_at_utc,
        )
    except Exception as e:
        logger.error("schedule_agent_call failed", error=str(e), session_id=session_id, exc_info=True)
        return {"success": False, "error": f"Failed to create schedule: {e}"}


@mcp.tool(
    name="schedule_recurring_agent_call",
    description=(
        "Schedule a recurring agent call using a cron expression. "
        "Use this to set up periodic agent invocations (e.g., daily reports, weekly checks). "
        "The target agent will receive the message as a prompt at each scheduled time. "
        "Requires your session_id (provided in the system prompt)."
    ),
)
async def schedule_recurring_agent_call(
    session_id: str,
    target_agent_name: str,
    message: str,
    cron_expression: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
    max_runs: int | None = None,
) -> dict[str, Any]:
    """Schedule a recurring agent call."""
    try:
        logger.info(
            "MCP tool: schedule_recurring_agent_call",
            session_id=session_id,
            target_agent_name=target_agent_name,
            cron_expression=cron_expression,
        )

        # Validate session and get caller agent info
        caller_session, caller_agent, error = await _get_session_and_agent(session_id)
        if error:
            return {"success": False, "error": error}

        # Validate timezone
        tz_error = validate_timezone(timezone)
        if tz_error:
            return {"success": False, "error": tz_error}

        # Validate cron expression
        cron_error = validate_cron_expression(cron_expression, timezone)
        if cron_error:
            return {"success": False, "error": cron_error}

        if max_runs is not None and max_runs <= 0:
            return {"success": False, "error": "max_runs must be a positive integer"}

        # Parse context
        context_dict, ctx_error = parse_schedule_context(context)
        if ctx_error:
            return {"success": False, "error": ctx_error}

        # Resolve user identity and verify Yuppster status
        created_by_user, created_by_agent, user_error = await _resolve_creator_info(caller_session, caller_agent)
        if user_error:
            return {"success": False, "error": user_error}

        next_run_utc = compute_next_run_for_cron(cron_expression, timezone)

        return await create_agent_schedule(
            agent_name=target_agent_name,
            message=message,
            schedule_type=AgentScheduleType.RECURRING,
            cron_timezone=timezone,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            cron_expression=cron_expression,
            next_run_utc=next_run_utc,
            max_runs=max_runs,
        )
    except Exception as e:
        logger.error("schedule_recurring_agent_call failed", error=str(e), session_id=session_id, exc_info=True)
        return {"success": False, "error": f"Failed to create schedule: {e}"}


# ============================================================================
# Workspace Tools — filesystem and shell access for raw executors
# ============================================================================


# Normalize extensions to the MIME subtype that FastMCP expects.
# FastMCP derives MIME mechanically: Image → image/{fmt}, File → application/{fmt}.
# Without this mapping, e.g. "jpg" → "image/jpg" (wrong) or "docx" → "application/docx" (wrong).
_EXT_TO_MIME_SUFFIX: dict[str, str] = {
    # Images
    "jpg": "jpeg",
    # Documents — deterministic; mimetypes.guess_type() is environment-dependent
    "doc": "msword",
    "docx": "vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "vnd.ms-excel",
    "xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "vnd.ms-powerpoint",
    "pptx": "vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _wrap_binary_result(result: BinaryFileResult) -> Image | File:
    """Convert a BinaryFileResult into the appropriate FastMCP content type.

    Image → MCP ImageContent      (model sees natively via vision)
    File  → MCP EmbeddedResource  (client-dependent; works for PDFs on Claude)
    """
    fmt = _EXT_TO_MIME_SUFFIX.get(result.extension, result.extension)
    if result.category == "image":
        return Image(data=result.data, format=fmt)
    return File(data=result.data, format=fmt)


@mcp.tool(
    name="read",
    description=(
        "Read a file from the workspace. Returns file contents with line numbers "
        "(cat -n style) for text files. For binary files that AI models can process "
        "(images, PDFs, documents), returns the content in a native multimodal "
        "format. Supports offset and limit for large text files."
    ),
)
def mcp_read(session_id: str, path: str, offset: int = 0, limit: int = 2000) -> str | Image | File:
    """Read a file from the workspace.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        offset: Line number to start from (0-based). Default 0. Ignored for binary files.
        limit: Maximum number of lines to read. Default 2000. Ignored for binary files.
    """
    logger.info("MCP tool: read", session_id=session_id, path=path)
    _validate_session_id(session_id)
    result = read_file(session_id, path, offset, limit)
    if isinstance(result, BinaryFileResult):
        return _wrap_binary_result(result)
    return result


@mcp.tool(
    name="write",
    description=(
        "Write or create a file in the workspace. Creates parent directories "
        "as needed. Requires write access (call request_write_access first)."
    ),
)
def mcp_write(session_id: str, path: str, content: str) -> str:
    """Write or create a file.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        content: File content to write.
    """
    logger.info("MCP tool: write", session_id=session_id, path=path, content_length=len(content))
    _validate_session_id(session_id)
    return write_file(session_id, path, content)


@mcp.tool(
    name="edit",
    description=(
        "Edit a file using find-and-replace. Finds old_string in the file and "
        "replaces it with new_string. Errors if old_string is not found or not "
        "unique (unless replace_all is True). Requires write access."
    ),
)
def mcp_edit(session_id: str, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file via find-and-replace.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        path: File path relative to workspace root.
        old_string: Text to find.
        new_string: Replacement text.
        replace_all: Replace all occurrences (default False).
    """
    logger.info("MCP tool: edit", session_id=session_id, path=path, replace_all=replace_all)
    _validate_session_id(session_id)
    return edit_file(session_id, path, old_string, new_string, replace_all)


@mcp.tool(
    name="glob",
    description=(
        "List files matching a glob pattern in the workspace. "
        "Supports patterns like '**/*.py' or 'src/**/*.ts'. "
        "Returns newline-separated relative file paths."
    ),
)
def mcp_glob(session_id: str, pattern: str, path: str | None = None) -> str:
    """List files matching a glob pattern.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        pattern: Glob pattern to match (e.g., '**/*.py').
        path: Optional subdirectory to search in.
    """
    logger.info("MCP tool: glob", session_id=session_id, pattern=pattern, path=path)
    _validate_session_id(session_id)
    return list_files(session_id, pattern, path)


@mcp.tool(
    name="grep",
    description=(
        "Search file contents using a regex pattern (grep-like). "
        "Returns matching lines in file:line:content format. "
        "Use the glob parameter to filter which files to search."
    ),
)
def mcp_grep(session_id: str, pattern: str, path: str | None = None, glob: str | None = None) -> str:
    """Search file contents with regex.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        pattern: Regex pattern to search for.
        path: Optional subdirectory to search in.
        glob: Optional glob pattern to filter files (e.g., '*.py').
    """
    logger.info("MCP tool: grep", session_id=session_id, pattern=pattern, path=path)
    _validate_session_id(session_id)
    return search_files(session_id, pattern, path, glob)


@mcp.tool(
    name="bash",
    description=(
        "Run a shell command in the workspace. Returns combined stdout+stderr. "
        "Commands run with bash in the workspace directory. "
        "Requires write access (call request_write_access first). "
        "Output is truncated at 100KB. Timeout default is 120s (max 600s)."
    ),
)
async def mcp_bash(session_id: str, command: str, timeout: int = 120) -> str:
    """Run a shell command.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        command: Shell command to execute.
        timeout: Timeout in seconds (default 120, max 600).
    """
    logger.info("MCP tool: bash", session_id=session_id, command_length=len(command), timeout=timeout)
    _validate_session_id(session_id)

    # Use the BCH warm proxy when registered for this session — avoids a fresh
    # bwrap spawn per call (~150–400 ms) and reuses the warm process (~5 ms).
    manager = get_command_handler_manager(session_id)
    if manager is not None:
        return await manager.call_tool("Bash", {"command": command, "timeout": timeout})

    # Fallback: original per-call bwrap path.
    stack = _session_sandbox.get(session_id)
    bwrap = stack[-1] if stack else False
    return run_command(session_id, command, timeout, bwrap=bwrap)


@mcp.tool(
    name="webfetch",
    description=(
        "Fetch content from a URL. Returns the page content as plain text "
        "(HTML tags are stripped). Useful for reading documentation, APIs, etc."
    ),
)
async def mcp_webfetch(session_id: str, url: str, prompt: str | None = None) -> str:
    """Fetch URL content.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        url: The URL to fetch.
        prompt: Optional prompt (unused, for API compatibility).
    """
    logger.info("MCP tool: webfetch", session_id=session_id, url=url)
    _validate_session_id(session_id)
    return await fetch_url(url, prompt)


@mcp.tool(
    name="websearch",
    description=(
        "Search the web using Google. Returns organic search results with "
        "titles, URLs, and snippets. Use this to find current information, "
        "documentation, or answers to questions."
    ),
)
async def mcp_websearch(session_id: str, query: str, num_results: int = 10) -> str:
    """Search the web.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        query: Search query string.
        num_results: Maximum number of results to return (default 10).
    """
    logger.info("MCP tool: websearch", session_id=session_id, query=query)
    _validate_session_id(session_id)

    # dict.setdefault is atomic (single bytecode op, no await) so exactly one Lock
    # is created per session_id even under concurrent calls.
    lock = _session_websearch_locks.setdefault(session_id, asyncio.Lock())

    async with lock:
        turn_count = _turn_websearch_count.get(session_id, 0)
        if turn_count >= MAX_WEBSEARCH_CALLS_PER_TURN:
            raise ValueError(
                f"Web search limit reached ({MAX_WEBSEARCH_CALLS_PER_TURN} per turn). "
                "Please work with the results you already have."
            )

        session_count = _session_websearch_count.get(session_id, 0)
        if session_count >= MAX_WEBSEARCH_CALLS_PER_SESSION:
            raise ValueError(
                f"Web search limit reached ({MAX_WEBSEARCH_CALLS_PER_SESSION} per session). "
                "Please work with the results you already have."
            )

        _turn_websearch_count[session_id] = turn_count + 1
        _session_websearch_count[session_id] = session_count + 1

    success = False
    try:
        result = await search_web(query, num_results)
        success = True
        return result
    finally:
        if not success:
            # Roll back counters on any failure (including CancelledError).
            async with lock:
                _turn_websearch_count[session_id] = max(0, _turn_websearch_count.get(session_id, 1) - 1)
                _session_websearch_count[session_id] = max(0, _session_websearch_count.get(session_id, 1) - 1)


# ============================================================================
# Linear Integration Tools
# ============================================================================


def _get_linear_client() -> "Any":
    """Create and return a LinearClient. Raises on missing API key."""
    from ypl.backend.utils.linear import LinearClient

    return LinearClient()


@mcp.tool(
    name="linear_list_teams",
    description=(
        "List all Linear teams (projects) available in the workspace. "
        "Returns each team's ID, name, and key. Use team IDs when creating "
        "or filtering issues with other Linear tools."
    ),
)
def linear_list_teams() -> dict[str, Any]:
    """List all Linear teams.

    Returns:
        Dict with 'teams' list, each containing id, name, key, description.
        On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_list_teams")
    try:
        client = _get_linear_client()
        response = client.get_teams()
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("teams", {}).get("nodes", [])
        return {"teams": nodes}
    except Exception as e:
        logger.error("linear_list_teams failed", exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_get_workflow_states",
    description=(
        "Get the workflow states (statuses) for a Linear team. "
        "Returns state IDs, names, types (e.g., 'backlog', 'started', 'completed'), "
        "and colors. Use state IDs when creating or updating issues."
    ),
)
def linear_get_workflow_states(team_id: str) -> dict[str, Any]:
    """Get workflow states for a Linear team.

    Args:
        team_id: The Linear team ID (get from linear_list_teams).

    Returns:
        Dict with 'states' list, each containing id, name, type, color, position.
        On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_get_workflow_states", team_id=team_id)
    try:
        client = _get_linear_client()
        response = client.get_workflow_states(team_id)
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("workflowStates", {}).get("nodes", [])
        # Sort by position for predictable ordering
        nodes_sorted = sorted(nodes, key=lambda x: x.get("position", 0))
        return {"states": nodes_sorted}
    except Exception as e:
        logger.error("linear_get_workflow_states failed", team_id=team_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_get_issue",
    description=(
        "Fetch a single Linear issue by its ID or identifier (e.g., 'ENG-123'). "
        "Returns full issue details including title, description, state, assignee, "
        "priority, and URLs."
    ),
)
def linear_get_issue(issue_id: str) -> dict[str, Any]:
    """Fetch a single Linear issue.

    Args:
        issue_id: Issue ID (UUID) or identifier (e.g., 'ENG-123').

    Returns:
        Dict with issue fields on success. On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_get_issue", issue_id=issue_id)
    try:
        client = _get_linear_client()
        response = client.get_issue(issue_id)
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        issue = (response.get("data") or {}).get("issue")
        if issue is None:
            return {"error": "Issue not found"}
        return {"issue": issue}
    except Exception as e:
        logger.error("linear_get_issue failed", issue_id=issue_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_search_issues",
    description=(
        "Search Linear issues with optional filters. Can filter by team, state, "
        "assignee, and/or a text query (matches title and description). "
        "Returns matching issues sorted by most recently updated."
    ),
)
def linear_search_issues(
    query: str | None = None,
    team_id: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search Linear issues.

    Args:
        query: Optional text to search in issue titles and descriptions.
        team_id: Optional team ID to filter by.
        state_id: Optional workflow state ID to filter by.
        assignee_id: Optional user ID to filter by assignee.
        limit: Maximum number of results to return (default 20, max 50).

    Returns:
        Dict with 'issues' list on success. On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_search_issues",
        query=query,
        team_id=team_id,
        state_id=state_id,
        assignee_id=assignee_id,
        limit=limit,
    )
    try:
        client = _get_linear_client()
        response = client.search_issues(
            query_term=query,
            team_id=team_id,
            state_id=state_id,
            assignee_id=assignee_id,
            limit=min(limit, 50),
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("issues", {}).get("nodes", [])
        return {"issues": nodes, "count": len(nodes)}
    except Exception as e:
        logger.error("linear_search_issues failed", exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_create_issue",
    description=(
        "Create a new issue in Linear. Requires a title and team ID (get from "
        "linear_list_teams). Optionally specify a workflow state (get from "
        "linear_get_workflow_states), assignee, description, and priority. "
        "Priority values: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low."
    ),
)
def linear_create_issue(
    title: str,
    team_id: str,
    description: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    """Create a new Linear issue.

    Args:
        title: Issue title (required).
        team_id: Team ID to create the issue in (required). Use linear_list_teams.
        description: Optional issue description (supports markdown).
        state_id: Optional workflow state ID. Defaults to team's default state.
        assignee_id: Optional user ID to assign the issue to.
        priority: Optional priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).

    Returns:
        Dict with 'issue' (id, identifier, title, url, state, team) on success.
        On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_create_issue",
        title=title,
        team_id=team_id,
        has_description=description is not None,
        state_id=state_id,
        assignee_id=assignee_id,
        priority=priority,
    )
    try:
        client = _get_linear_client()
        response = client.create_issue(
            title=title,
            team_id=team_id,
            description=description,
            state_id=state_id,
            assignee_id=assignee_id,
            priority=priority,
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Create failed")}
        result = (response.get("data") or {}).get("issueCreate", {})
        if not result.get("success"):
            return {"error": "Create failed"}
        return {"issue": result.get("issue"), "success": True}
    except Exception as e:
        logger.error("linear_create_issue failed", title=title, team_id=team_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_update_issue",
    description=(
        "Update an existing Linear issue. Specify the issue ID or identifier "
        "(e.g., 'ENG-123') and any fields to change: title, description, state, "
        "assignee, or priority. Only provided fields are updated. "
        "Priority values: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low."
    ),
)
def linear_update_issue(
    issue_id: str,
    title: str | None = None,
    description: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    """Update an existing Linear issue.

    Args:
        issue_id: Issue ID (UUID) or identifier (e.g., 'ENG-123').
        title: New title for the issue.
        description: New description (supports markdown).
        state_id: New workflow state ID (use linear_get_workflow_states).
        assignee_id: New assignee user ID. Pass empty string to unassign.
        priority: New priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).

    Returns:
        Dict with 'issue' (updated fields) on success.
        On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_update_issue",
        issue_id=issue_id,
        title=title,
        state_id=state_id,
        assignee_id=assignee_id,
        priority=priority,
    )
    if not any(v is not None for v in (title, description, state_id, assignee_id, priority)):
        return {"error": "At least one field must be provided to update"}
    try:
        client = _get_linear_client()
        response = client.update_issue(
            issue_id=issue_id,
            title=title,
            description=description,
            state_id=state_id,
            assignee_id=assignee_id,
            priority=priority,
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Update failed")}
        result = (response.get("data") or {}).get("issueUpdate", {})
        if not result.get("success"):
            return {"error": "Update failed"}
        return {"issue": result.get("issue"), "success": True}
    except Exception as e:
        logger.error("linear_update_issue failed", issue_id=issue_id, exc_info=True)
        return {"error": str(e)}


# --- Skills ---


@mcp.tool(
    name="load_skill",
    description=(
        "Load a skill's full documentation by name. Skills provide domain-specific "
        "guidance for common tasks (e.g. workspace workflows, memory management, "
        "debugging patterns). The system prompt lists available skills with short "
        "descriptions — use this tool to load the full content when needed."
    ),
)
def load_skill(skill_name: str) -> str:
    """Load and return a skill's SKILL.md content.

    Args:
        skill_name: Name of the skill to load (e.g., 'workspace-guide',
            'memory-guide', 'fetch-from-db'). Must match a directory name
            under the skills directory.

    Returns:
        The full skill content with YAML frontmatter stripped.
        On error, returns a descriptive error message.
    """
    logger.info("MCP tool: load_skill", skill_name=skill_name)

    from ypl.agent_harness_service.common.constants import AHS_SKILLS_DIR

    # Validate skill_name to prevent path traversal
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        return f"[ERROR] Invalid skill name: {skill_name!r}"

    skill_path = os.path.join(AHS_SKILLS_DIR, skill_name, "SKILL.md")
    if not os.path.isfile(skill_path):
        # List available skills to help the agent
        available: list[str] = []
        if os.path.isdir(AHS_SKILLS_DIR):
            available = sorted(
                d for d in os.listdir(AHS_SKILLS_DIR) if os.path.isfile(os.path.join(AHS_SKILLS_DIR, d, "SKILL.md"))
            )
        return f"[ERROR] Skill '{skill_name}' not found. Available skills: {', '.join(available)}"

    try:
        with open(skill_path) as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] Failed to read skill '{skill_name}': {e}"

    # Strip YAML frontmatter (--- ... ---)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3 :].lstrip("\n")

    return content


if __name__ == "__main__":
    mcp.run(transport="stdio")
