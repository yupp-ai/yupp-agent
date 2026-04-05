"""Extended tests for ypl.agent_harness_service.common.types.

Covers:
- StreamEvent properties (text, session_id, cost_usd, duration_ms, num_turns, subtype)
- SessionPermissions (full_access, restricted, from_context, has_full_tool_access)
- AHSValidationError inheritance
- SessionCreateRequest, SessionMessageRequest, SessionStopRequest validation
- AttachmentInfo model
"""

from __future__ import annotations
from typing import Any

import pytest
from ypl.agent_harness_service.common.types import (
    AHSValidationError,
    AttachmentInfo,
    SessionCreateRequest,
    SessionMessageRequest,
    SessionPermissions,
    SessionStopRequest,
    StreamEvent,
)

# ===========================================================================
# StreamEvent
# ===========================================================================


class TestStreamEventText:
    def test_assistant_event_extracts_text(self) -> None:
        event = StreamEvent(
            type="assistant",
            raw={
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": " world"},
                    ]
                }
            },
        )
        assert event.text == "Hello world"

    def test_assistant_event_ignores_non_text_blocks(self) -> None:
        event = StreamEvent(
            type="assistant",
            raw={
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Bash"},
                        {"type": "text", "text": "done"},
                    ]
                }
            },
        )
        assert event.text == "done"

    def test_non_assistant_event_text_is_empty(self) -> None:
        event = StreamEvent(type="result", raw={"text": "some text"})
        assert event.text == ""

    def test_assistant_empty_content_returns_empty(self) -> None:
        event = StreamEvent(type="assistant", raw={"message": {"content": []}})
        assert event.text == ""

    def test_assistant_missing_content_returns_empty(self) -> None:
        event = StreamEvent(type="assistant", raw={"message": {}})
        assert event.text == ""


class TestStreamEventSessionId:
    def test_system_event_returns_session_id(self) -> None:
        event = StreamEvent(type="system", raw={"session_id": "sess-123"})
        assert event.session_id == "sess-123"

    def test_result_event_returns_session_id(self) -> None:
        event = StreamEvent(type="result", raw={"session_id": "sess-456"})
        assert event.session_id == "sess-456"

    def test_assistant_event_session_id_is_none(self) -> None:
        event = StreamEvent(type="assistant", raw={})
        assert event.session_id is None

    def test_system_event_missing_session_id_returns_none(self) -> None:
        event = StreamEvent(type="system", raw={})
        assert event.session_id is None


class TestStreamEventCostUsd:
    def test_result_event_returns_cost_usd(self) -> None:
        event = StreamEvent(type="result", raw={"estimated_cost_usd": 0.05})
        assert event.cost_usd == 0.05

    def test_result_event_cost_usd_fallback(self) -> None:
        event = StreamEvent(type="result", raw={"cost_usd": 0.03})
        assert event.cost_usd == 0.03

    def test_non_result_event_cost_is_none(self) -> None:
        event = StreamEvent(type="assistant", raw={})
        assert event.cost_usd is None

    def test_result_event_zero_estimated_cost_falls_through(self) -> None:
        # Known source-level issue: 0 is falsy, so `estimated_cost_usd or cost_usd`
        # skips the zero value and returns cost_usd instead. This test documents the
        # current (arguably incorrect) behavior rather than blessing it.
        event = StreamEvent(type="result", raw={"estimated_cost_usd": 0, "cost_usd": 0.01})
        assert event.cost_usd == 0.01


class TestStreamEventDurationMs:
    def test_result_event_returns_duration(self) -> None:
        event = StreamEvent(type="result", raw={"duration_ms": 5000})
        assert event.duration_ms == 5000

    def test_non_result_event_duration_is_none(self) -> None:
        event = StreamEvent(type="system", raw={"duration_ms": 999})
        assert event.duration_ms is None


class TestStreamEventNumTurns:
    def test_result_event_returns_num_turns(self) -> None:
        event = StreamEvent(type="result", raw={"num_turns": 3})
        assert event.num_turns == 3

    def test_non_result_event_num_turns_is_none(self) -> None:
        event = StreamEvent(type="assistant", raw={})
        assert event.num_turns is None


class TestStreamEventSubtype:
    def test_result_event_returns_subtype(self) -> None:
        event = StreamEvent(type="result", raw={"subtype": "success"})
        assert event.subtype == "success"

    def test_result_event_stopped_subtype(self) -> None:
        event = StreamEvent(type="result", raw={"subtype": "stopped"})
        assert event.subtype == "stopped"

    def test_non_result_event_subtype_is_none(self) -> None:
        event = StreamEvent(type="assistant", raw={})
        assert event.subtype is None


class TestStreamEventDefaults:
    def test_raw_defaults_to_empty_dict(self) -> None:
        event = StreamEvent(type="system")
        assert event.raw == {}

    def test_type_preserved(self) -> None:
        event = StreamEvent(type="tool_use", raw={"name": "Bash"})
        assert event.type == "tool_use"


# ===========================================================================
# SessionPermissions
# ===========================================================================


class TestSessionPermissionsFullAccess:
    def test_full_access_has_wildcard_tools(self) -> None:
        perms = SessionPermissions.full_access()
        assert "*" in perms.allowed_harness_tools

    def test_full_access_has_full_tool_access(self) -> None:
        perms = SessionPermissions.full_access()
        assert perms.has_full_tool_access is True

    def test_full_access_includes_all_servers(self) -> None:
        from ypl.agent_harness_service.common.constants import ALL_MCP_SERVERS

        perms = SessionPermissions.full_access()
        for server in ALL_MCP_SERVERS:
            assert server in perms.allowed_servers


class TestSessionPermissionsRestricted:
    def test_restricted_only_has_harness_server(self) -> None:
        perms = SessionPermissions.restricted()
        assert perms.allowed_servers == ["harness"]

    def test_restricted_does_not_have_wildcard(self) -> None:
        perms = SessionPermissions.restricted()
        assert "*" not in perms.allowed_harness_tools

    def test_restricted_no_full_tool_access(self) -> None:
        perms = SessionPermissions.restricted()
        assert perms.has_full_tool_access is False

    def test_restricted_has_basic_tools(self) -> None:
        from ypl.agent_harness_service.common.constants import RESTRICTED_HARNESS_TOOLS

        perms = SessionPermissions.restricted()
        for tool in RESTRICTED_HARNESS_TOOLS:
            assert tool in perms.allowed_harness_tools


class TestSessionPermissionsFromContext:
    def test_from_context_with_permissions_key(self) -> None:
        ctx: dict[str, Any] = {
            "permissions": {
                "allowed_servers": ["harness"],
                "allowed_harness_tools": ["send_slack_message"],
            }
        }
        perms = SessionPermissions.from_context(ctx)
        assert perms.allowed_servers == ["harness"]
        assert perms.allowed_harness_tools == ["send_slack_message"]

    def test_from_context_empty_returns_default(self) -> None:
        perms = SessionPermissions.from_context({})
        assert perms.has_full_tool_access is True

    def test_from_context_old_format_denied_tools(self) -> None:
        """Old format with denied_* fields: any denies → restricted."""
        ctx = {"permissions": {"denied_tools": ["bash"], "denied_servers": []}}
        perms = SessionPermissions.from_context(ctx)
        assert perms.has_full_tool_access is False

    def test_from_context_old_format_no_denies(self) -> None:
        """Old format with empty denied_* fields: → full access."""
        ctx: dict = {"permissions": {"denied_tools": [], "denied_servers": []}}
        perms = SessionPermissions.from_context(ctx)
        assert perms.has_full_tool_access is True

    def test_from_context_with_denied_servers(self) -> None:
        """Old format with non-empty denied_servers: → restricted."""
        ctx = {"permissions": {"denied_tools": [], "denied_servers": ["yuppster-mcp-server"]}}
        perms = SessionPermissions.from_context(ctx)
        assert perms.has_full_tool_access is False


class TestHasFullToolAccess:
    def test_wildcard_in_tools_is_full_access(self) -> None:
        perms = SessionPermissions(allowed_harness_tools=["*"])
        assert perms.has_full_tool_access is True

    def test_specific_tools_only_not_full_access(self) -> None:
        perms = SessionPermissions(allowed_harness_tools=["send_slack_message", "new_task"])
        assert perms.has_full_tool_access is False

    def test_empty_list_not_full_access(self) -> None:
        perms = SessionPermissions(allowed_harness_tools=[])
        assert perms.has_full_tool_access is False


# ===========================================================================
# AHSValidationError
# ===========================================================================


class TestAHSValidationError:
    def test_is_value_error(self) -> None:
        err = AHSValidationError("test error")
        assert isinstance(err, ValueError)

    def test_message_preserved(self) -> None:
        err = AHSValidationError("missing user_id")
        assert str(err) == "missing user_id"

    def test_can_be_raised_and_caught_as_value_error(self) -> None:
        with pytest.raises(ValueError):
            raise AHSValidationError("bad request")

    def test_can_be_caught_as_ahs_error(self) -> None:
        with pytest.raises(AHSValidationError):
            raise AHSValidationError("bad request")


# ===========================================================================
# Request model validation
# ===========================================================================


class TestSessionCreateRequest:
    def test_minimal_required_fields(self) -> None:
        req = SessionCreateRequest(agent_id="sre", trigger="slack")
        assert req.agent_id == "sre"
        assert req.trigger == "slack"
        assert req.message is None

    def test_optional_fields_default(self) -> None:
        req = SessionCreateRequest(agent_id="sre", trigger="api")
        assert req.user_id is None
        assert req.context is None
        assert req.session_id is None
        assert req.attachments is None
        assert req.source == "api"
        assert req.force_model is None

    def test_with_message(self) -> None:
        req = SessionCreateRequest(agent_id="sre", trigger="slack", message="Hello agent")
        assert req.message == "Hello agent"

    def test_with_context(self) -> None:
        ctx = {"repo": "yupp-agent", "pr_url": "https://github.com/pr/123"}
        req = SessionCreateRequest(agent_id="sre", trigger="slack", context=ctx)
        assert req.context == ctx

    def test_with_attachments(self) -> None:
        attachment = AttachmentInfo(
            filename="report.pdf",
            content_type="application/pdf",
            size=1024,
            gcs_url="gs://yupp-agents/attachments/sess/report.pdf",
        )
        req = SessionCreateRequest(agent_id="sre", trigger="api", attachments=[attachment])
        assert req.attachments is not None
        assert len(req.attachments) == 1


class TestSessionMessageRequest:
    def test_required_fields(self) -> None:
        req = SessionMessageRequest(session_id="sess-123", message="What's the status?")
        assert req.session_id == "sess-123"
        assert req.message == "What's the status?"

    def test_optional_fields_default(self) -> None:
        req = SessionMessageRequest(session_id="sess-123", message="hi")
        assert req.slack_ts is None
        assert req.slack_user_id is None
        assert req.user_id is None
        assert req.source == "api"

    def test_with_slack_fields(self) -> None:
        req = SessionMessageRequest(
            session_id="sess-123",
            message="hi",
            slack_ts="1234567890.123456",
            slack_user_id="U12345",
        )
        assert req.slack_ts == "1234567890.123456"
        assert req.slack_user_id == "U12345"


class TestSessionStopRequest:
    def test_requires_session_id(self) -> None:
        req = SessionStopRequest(session_id="sess-123")
        assert req.session_id == "sess-123"


class TestAttachmentInfo:
    def test_all_fields(self) -> None:
        att = AttachmentInfo(
            filename="image.png",
            content_type="image/png",
            size=2048,
            gcs_url="gs://yupp-agents/attachments/sess/image.png",
        )
        assert att.filename == "image.png"
        assert att.content_type == "image/png"
        assert att.size == 2048
        assert att.gcs_url == "gs://yupp-agents/attachments/sess/image.png"
