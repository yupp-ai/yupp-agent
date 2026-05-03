"""Unit tests for ``ypl.mcp_server.tools.artifact_notifier``.

Covers:

* The pure ``build_notification_payload`` formatter — title escaping,
  per-type visuals, slug+version field, MEMORY scope redaction, attribution
  line, link rendering for pointer artifacts.
* The ``notify_artifact_event`` entry point — channel resolution
  (env-disabled, empty config, configured), background-task scheduling,
  Slack post mocking, and failure swallowing.
"""

from __future__ import annotations
import asyncio
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError
from ypl.db.agent_harness import AgentArtifactType
from ypl.mcp_common.auth_context import RequestContext
from ypl.mcp_server.tools import artifact_notifier
from ypl.mcp_server.tools.artifact_notifier import (
    _build_attribution,
    _format_memory_scope,
    _mrkdwn_escape,
    _slack_link,
    _truncate,
    build_notification_payload,
    notify_artifact_event,
)

FAKE_ARTIFACT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FAKE_SESSION_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _agent_artifacts_ctx(
    *,
    session_id: str | None = str(FAKE_SESSION_ID),
    agent_name: str | None = "eng-raccoon",
    user_id: str | None = "user-1",
) -> RequestContext:
    """Build a RequestContext suitable for patching ``current_request_context``."""
    return RequestContext(
        auth_kind="agent_secret",
        requesting_user_id=user_id,
        ahs_session_id=session_id,
        ahs_agent_name=agent_name,
    )


def _make_artifact(
    *,
    artifact_type: AgentArtifactType = AgentArtifactType.TEXT,
    title: str = "Investigation report",
    description: str | None = None,
    url: str | None = "https://artifacts.example.com/artifacts/abc",
    named_slug: str | None = None,
    version: int | None = None,
    memory_scope: str | None = None,
    memory_scope_subject: str | None = None,
) -> MagicMock:
    a = MagicMock()
    a.agent_artifact_id = FAKE_ARTIFACT_ID
    a.artifact_type = artifact_type
    a.title = title
    a.description = description
    a.url = url
    a.named_slug = named_slug
    a.version = version
    a.memory_scope = memory_scope
    a.memory_scope_subject = memory_scope_subject
    return a


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_value_unchanged(self) -> None:
        assert _truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self) -> None:
        assert _truncate("hello", 5) == "hello"

    def test_too_long_is_truncated_with_ellipsis(self) -> None:
        result = _truncate("hello world", 6)
        assert result.endswith("…")
        assert len(result) <= 6

    def test_empty_string(self) -> None:
        assert _truncate("", 10) == ""


class TestMrkdwnEscape:
    def test_escapes_angle_brackets(self) -> None:
        assert _mrkdwn_escape("a<b>c") == "a&lt;b&gt;c"

    def test_escapes_ampersand(self) -> None:
        assert _mrkdwn_escape("a&b") == "a&amp;b"

    def test_idempotent_for_safe_text(self) -> None:
        assert _mrkdwn_escape("hello world") == "hello world"


class TestSlackLink:
    def test_with_url(self) -> None:
        assert _slack_link("https://example.com", "Click") == "<https://example.com|Click>"

    def test_without_url_falls_back_to_bold(self) -> None:
        assert _slack_link(None, "Click") == "*Click*"

    def test_label_is_escaped(self) -> None:
        assert _slack_link("https://example.com", "<weird>") == "<https://example.com|&lt;weird&gt;>"


class TestFormatMemoryScope:
    def test_topic_scope(self) -> None:
        assert _format_memory_scope("topic", None) == "`topic` (shared)"

    def test_agent_scope_with_subject(self) -> None:
        assert _format_memory_scope("agent", "eng-raccoon") == "`agent` (`eng-raccoon`)"

    def test_agent_scope_without_subject(self) -> None:
        assert _format_memory_scope("agent", None) == "`agent`"

    def test_user_scope_redacts_subject(self) -> None:
        # The user_id must NEVER appear in the rendered output (privacy).
        result = _format_memory_scope("user", "user-123")
        assert "user-123" not in (result or "")
        assert result == "`user` (private)"

    def test_unknown_scope_falls_back(self) -> None:
        assert _format_memory_scope("custom", None) == "`custom`"

    def test_empty_scope_returns_none(self) -> None:
        assert _format_memory_scope(None, None) is None


class TestBuildAttribution:
    def test_with_agent_and_session(self) -> None:
        parts = _build_attribution(
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id=None,
        )
        assert any("eng-raccoon" in p for p in parts)
        assert any(str(FAKE_SESSION_ID) in p for p in parts)

    def test_user_id_not_echoed(self) -> None:
        parts = _build_attribution(
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id="user-private-123",
        )
        joined = " | ".join(parts)
        assert "user-private-123" not in joined

    def test_empty_inputs_yield_empty_list(self) -> None:
        assert _build_attribution(agent_name=None, session_id=None, user_id=None) == []


# ---------------------------------------------------------------------------
# build_notification_payload — full message shape
# ---------------------------------------------------------------------------


class TestBuildNotificationPayload:
    def test_text_artifact_minimal(self) -> None:
        artifact = _make_artifact(
            artifact_type=AgentArtifactType.TEXT,
            title="My report",
            url="https://artifacts.example.com/artifacts/abc",
        )
        text, blocks, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id="user-1",
        )
        # Fallback text always populated for accessibility / push notifications.
        assert "TEXT" in text
        assert "created" in text
        assert "My report" in text
        # Blocks are inside an attachment so we get the coloured side-bar.
        assert blocks == []
        assert len(attachments) == 1
        assert attachments[0]["color"] == "#1d9bf0"  # TEXT colour
        attachment_blocks = attachments[0]["blocks"]
        # First block is the header.
        assert attachment_blocks[0]["type"] == "section"
        assert "TEXT" in attachment_blocks[0]["text"]["text"]
        # The viewer URL gets linked under the title.
        assert "https://artifacts.example.com/artifacts/abc" in attachment_blocks[0]["text"]["text"]

    def test_includes_id_field(self) -> None:
        artifact = _make_artifact()
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        rendered = str(attachments)
        assert str(FAKE_ARTIFACT_ID) in rendered

    def test_includes_slug_and_version(self) -> None:
        artifact = _make_artifact(named_slug="my-report", version=3)
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="new_version",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        rendered = str(attachments)
        assert "my-report" in rendered
        assert "v3" in rendered

    def test_pointer_artifact_includes_link_field(self) -> None:
        artifact = _make_artifact(
            artifact_type=AgentArtifactType.CODE_REVIEW,
            title="Fix auth bug",
            url="https://github.com/yupp-ai/repo/pull/42",
        )
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id=None,
        )
        rendered = str(attachments)
        # The pointer URL should appear at least once (linked in the title)
        # and as its own *Link* field.
        assert "github.com/yupp-ai/repo/pull/42" in rendered
        assert "Link" in rendered

    def test_description_renders_as_italic(self) -> None:
        artifact = _make_artifact(description="A beautiful description.")
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        rendered = str(attachments)
        # Italic = wrapped in underscores per Slack mrkdwn.
        assert "_A beautiful description._" in rendered

    def test_long_title_is_truncated(self) -> None:
        long_title = "x" * 1000
        artifact = _make_artifact(title=long_title)
        text, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        assert "…" in str(attachments)
        assert long_title not in text  # truncated below 1000 chars

    def test_memory_user_scope_redacts_subject(self) -> None:
        artifact = _make_artifact(
            artifact_type=AgentArtifactType.MEMORY,
            url=None,  # MEMORY artifacts have inline content, no url
            named_slug="user_preferences",
            version=1,
            memory_scope="user",
            memory_scope_subject="user-private-id-999",
        )
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id="user-private-id-999",
        )
        rendered = str(attachments)
        # The user_id must NOT appear anywhere in the rendered Slack payload.
        assert "user-private-id-999" not in rendered
        # But the scope label IS rendered, with a redaction marker.
        assert "private" in rendered

    def test_memory_agent_scope_includes_subject(self) -> None:
        artifact = _make_artifact(
            artifact_type=AgentArtifactType.MEMORY,
            url=None,
            named_slug="feedback_style",
            version=1,
            memory_scope="agent",
            memory_scope_subject="eng-raccoon",
        )
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name="eng-raccoon",
            session_id=FAKE_SESSION_ID,
            user_id=None,
        )
        rendered = str(attachments)
        # Agent name is public — safe to echo.
        assert "eng-raccoon" in rendered

    def test_memory_uses_memory_color(self) -> None:
        artifact = _make_artifact(
            artifact_type=AgentArtifactType.MEMORY,
            url=None,
            named_slug="x",
            version=1,
            memory_scope="topic",
        )
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        assert attachments[0]["color"] == "#a371f7"

    def test_html_in_title_is_escaped(self) -> None:
        artifact = _make_artifact(title="<script>alert(1)</script>")
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        rendered = str(attachments)
        # Raw < / > should be escaped to mrkdwn entity form so Slack won't
        # treat them as link-syntax delimiters.
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered

    def test_event_verb_per_event(self) -> None:
        artifact = _make_artifact()
        for event, expected_verb in [
            ("created", "created"),
            ("updated", "updated"),
            ("new_version", "new version saved"),
        ]:
            text, _, attachments = build_notification_payload(
                artifact=artifact,
                event=event,  # type: ignore[arg-type]
                agent_name=None,
                session_id=None,
                user_id=None,
            )
            rendered = str(attachments) + text
            assert expected_verb in rendered


# ---------------------------------------------------------------------------
# notify_artifact_event — channel gating + Slack post
# ---------------------------------------------------------------------------


class TestNotifyArtifactEvent:
    async def test_disabled_environment_skips(self) -> None:
        """In local / test environments we must never call Slack."""
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "local"}, clear=False),
            patch("ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client") as mock_client,
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
        assert task is None
        mock_client.assert_not_called()

    async def test_empty_channel_skips(self) -> None:
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=False),
            patch.object(artifact_notifier.settings, "ARTIFACT_NOTIFICATIONS_CHANNEL", ""),
            patch("ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client") as mock_client,
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
        assert task is None
        mock_client.assert_not_called()

    async def test_posts_to_configured_channel(self) -> None:
        post_mock = AsyncMock(return_value={"ok": True, "ts": "123"})
        client_mock = MagicMock()
        client_mock.chat_postMessage = post_mock
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=False),
            patch.object(artifact_notifier.settings, "ARTIFACT_NOTIFICATIONS_CHANNEL", "C0FAKE0001"),
            patch(
                "ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client",
                return_value=client_mock,
            ),
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
            assert task is not None
            await task
        post_mock.assert_awaited_once()
        assert post_mock.await_args is not None
        kwargs = post_mock.await_args.kwargs
        assert kwargs["channel"] == "C0FAKE0001"
        # Defensive: link previews are off so the channel doesn't get noisy.
        assert kwargs["unfurl_links"] is False
        assert kwargs["unfurl_media"] is False
        # The attachment carries the per-type colour.
        assert kwargs["attachments"][0]["color"]

    async def test_slack_api_error_swallowed(self) -> None:
        post_mock = AsyncMock(side_effect=SlackApiError(message="rate limited", response={"error": "rate_limited"}))  # type: ignore[no-untyped-call]
        client_mock = MagicMock()
        client_mock.chat_postMessage = post_mock
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=False),
            patch.object(artifact_notifier.settings, "ARTIFACT_NOTIFICATIONS_CHANNEL", "C0FAKE0001"),
            patch(
                "ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client",
                return_value=client_mock,
            ),
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
            assert task is not None
            # Awaiting the task must NOT raise — the notifier swallows errors.
            await task

    async def test_missing_ops_bot_client_swallowed(self) -> None:
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=False),
            patch.object(artifact_notifier.settings, "ARTIFACT_NOTIFICATIONS_CHANNEL", "C0FAKE0001"),
            patch(
                "ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client",
                side_effect=ValueError("SLACK_MCP_SERVER_APP_BOT_TOKEN is not set"),
            ),
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
            assert task is not None
            # Must not raise — the artifact already saved fine.
            await task

    async def test_returns_task_so_callers_can_await(self) -> None:
        post_mock = AsyncMock(return_value={"ok": True, "ts": "123"})
        client_mock = MagicMock()
        client_mock.chat_postMessage = post_mock
        with (
            patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=False),
            patch.object(artifact_notifier.settings, "ARTIFACT_NOTIFICATIONS_CHANNEL", "C0FAKE0001"),
            patch(
                "ypl.mcp_server.tools.artifact_notifier.get_ops_bot_write_client",
                return_value=client_mock,
            ),
        ):
            task = await notify_artifact_event(
                artifact=_make_artifact(),
                event="created",
                agent_name="eng-raccoon",
                session_id=FAKE_SESSION_ID,
                user_id=None,
            )
            assert isinstance(task, asyncio.Task)
            await task

    @pytest.mark.parametrize(
        ("artifact_type", "expected_color"),
        [
            (AgentArtifactType.TEXT, "#1d9bf0"),
            (AgentArtifactType.CODE_REVIEW, "#2eb886"),
            (AgentArtifactType.OTHER, "#9aa0a6"),
            (AgentArtifactType.MEMORY, "#a371f7"),
        ],
    )
    async def test_colour_per_type(self, artifact_type: AgentArtifactType, expected_color: str) -> None:
        artifact = _make_artifact(
            artifact_type=artifact_type,
            url=None if artifact_type == AgentArtifactType.MEMORY else "https://example.com/x",
            memory_scope="topic" if artifact_type == AgentArtifactType.MEMORY else None,
        )
        _, _, attachments = build_notification_payload(
            artifact=artifact,
            event="created",
            agent_name=None,
            session_id=None,
            user_id=None,
        )
        assert attachments[0]["color"] == expected_color


# ---------------------------------------------------------------------------
# Integration smoke: ExitStack-style hookup is wired into the MCP tools
# ---------------------------------------------------------------------------


class TestArtifactToolsHookNotifier:
    """Sanity-check that the MCP artifact tools call ``notify_artifact_event``.

    We don't re-test the full MCP tool surface here — that's covered by
    ``test_mcp_artifacts.py``. We only verify the hook fires. The patch
    target is the symbol *inside* each module (``agent_artifacts`` /
    ``memory_artifacts``), since both import via ``from … import …``.
    """

    async def test_add_artifact_pointer_calls_notifier(self) -> None:
        from ypl.mcp_server.tools.agent_artifacts import add_artifact

        artifact = _make_artifact(artifact_type=AgentArtifactType.CODE_REVIEW, url="https://example.com/pr/1")
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.current_request_context",
                    return_value=_agent_artifacts_ctx(),
                )
            )
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None))
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts._insert_pointer_artifact",
                    AsyncMock(return_value=artifact),
                )
            )
            notify_mock = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.notify_artifact_event",
                    AsyncMock(return_value=None),
                )
            )
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="My PR",
                url="https://example.com/pr/1",
            )
        assert result["success"] is True
        notify_mock.assert_awaited_once()
        assert notify_mock.await_args is not None
        kwargs = notify_mock.await_args.kwargs
        assert kwargs["event"] == "created"
        assert kwargs["agent_name"] == "eng-raccoon"

    async def test_update_artifact_calls_notifier(self) -> None:
        from ypl.mcp_server.tools.agent_artifacts import update_artifact

        artifact = _make_artifact(title="Updated title")
        with ExitStack() as stack:
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts._caller_agent_name", return_value="eng-raccoon")
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts._caller_session_id",
                    return_value=FAKE_SESSION_ID,
                )
            )
            stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts._caller_user_id", return_value="user-1"))
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts._update_artifact",
                    AsyncMock(return_value=artifact),
                )
            )
            notify_mock = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.notify_artifact_event",
                    AsyncMock(return_value=None),
                )
            )
            result = await update_artifact.fn(artifact_id=str(FAKE_ARTIFACT_ID), title="Updated title")
        assert result["success"] is True
        notify_mock.assert_awaited_once()
        assert notify_mock.await_args is not None
        assert notify_mock.await_args.kwargs["event"] == "updated"

    async def test_update_artifact_content_calls_notifier(self) -> None:
        from ypl.mcp_server.tools.agent_artifacts import update_artifact_content

        artifact = _make_artifact(named_slug="my-report", version=2, url="https://artifacts.example.com/x")
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.current_request_context",
                    return_value=_agent_artifacts_ctx(),
                )
            )
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None))
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.create_artifact",
                    AsyncMock(return_value=artifact),
                )
            )
            notify_mock = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.notify_artifact_event",
                    AsyncMock(return_value=None),
                )
            )
            result = await update_artifact_content.fn(slug="my-report", content="# v2")
        assert result["success"] is True
        notify_mock.assert_awaited_once()
        assert notify_mock.await_args is not None
        assert notify_mock.await_args.kwargs["event"] == "new_version"
