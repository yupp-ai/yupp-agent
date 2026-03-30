"""Core Agent Harness Service — session management, message handling.

This package is the main orchestrator. It:
1. Creates/resumes sessions
2. Stores user messages
3. Kicks off the agent runner as a background task
4. Persists agent responses with metadata

Re-exports all public names for backward compatibility with callers
that import from ``ypl.agent_harness_service.service``.
"""

# ---------------------------------------------------------------------------
# State & constants
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# DB resolvers
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service._resolvers import (
    _download_attachments_to_workspace,
    _has_inflight_turn,
    _load_agent_config_with_db_fallback,
    _mark_session_completed,
    _next_turn_number,
    _prepend_attachment_paths,
    _resolve_agent,
    _resolve_personal_agent_for_user,
    _resolve_session,
    _resolve_user_name_from_db,
)
from ypl.agent_harness_service.service._state import (
    PERSONAL_AGENT_PREFIXES,
    PendingMessage,
    _active_tasks,
    _command_handlers,  # test-only export (test_service_bch_lifecycle.py)
    _pending_messages,
    _pre_spawn_tasks,
    get_active_turn_count,
    has_execution_capacity,
)

# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service.agent_crud import (
    create_agent,
    edit_agent,
)

# ---------------------------------------------------------------------------
# Message / data helpers
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service.message_helpers import (
    VisibleContent,
    _extract_tool_uses,  # test-only export (test_service.py)
    _extract_visible_content,
    _scrub_null_bytes,
    _trim_value,
    strip_thinking_tags,
)

# ---------------------------------------------------------------------------
# Queries & feedback
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service.queries import (
    get_agent_detail,
    get_session_detail,
    get_session_history,
    list_agents,
    list_sessions,
    send_feedback,
)

# ---------------------------------------------------------------------------
# Agent turn runner
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service.run_task import (
    EagerPersistState,
    _eager_persist_agent_msg,
    _persist_system_msg,
    _run_agent_task,
)

# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.service.session_lifecycle import (
    _drain_pending_messages,
    _inject_internal_message,
    _maybe_update_task_completion,
    attach_slack_to_session,
    create_session,
    deliver_subagent_result_to_parent,
    send_message,
    send_slack_restart_courtesy,
    send_slack_shutdown_courtesy,
    stop_all_command_handler_managers,
    stop_session,
)

__all__ = [
    # State
    "PERSONAL_AGENT_PREFIXES",
    "PendingMessage",
    "_active_tasks",
    "_command_handlers",
    "_pending_messages",
    "_pre_spawn_tasks",
    "get_active_turn_count",
    "has_execution_capacity",
    # Message helpers
    "VisibleContent",
    "_extract_tool_uses",
    "_extract_visible_content",
    "_scrub_null_bytes",
    "_trim_value",
    "strip_thinking_tags",
    # Resolvers
    "_download_attachments_to_workspace",
    "_has_inflight_turn",
    "_load_agent_config_with_db_fallback",
    "_mark_session_completed",
    "_next_turn_number",
    "_prepend_attachment_paths",
    "_resolve_agent",
    "_resolve_personal_agent_for_user",
    "_resolve_session",
    "_resolve_user_name_from_db",
    # Run task
    "EagerPersistState",
    "_eager_persist_agent_msg",
    "_persist_system_msg",
    "_run_agent_task",
    # Session lifecycle
    "_drain_pending_messages",
    "_inject_internal_message",
    "_maybe_update_task_completion",
    "attach_slack_to_session",
    "create_session",
    "deliver_subagent_result_to_parent",
    "send_message",
    "send_slack_restart_courtesy",
    "send_slack_shutdown_courtesy",
    "stop_all_command_handler_managers",
    "stop_session",
    # Queries
    "get_agent_detail",
    "get_session_detail",
    "get_session_history",
    "list_agents",
    "list_sessions",
    "send_feedback",
    # Agent CRUD
    "create_agent",
    "edit_agent",
]
