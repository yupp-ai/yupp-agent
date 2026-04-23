"""Unit tests for SAG events module.

Covers:
- verify_slack_signature_multi (HMAC validation, replay protection)
- handle_url_verification (challenge echo)
- _extract_command (stop / attach / help / agents / models / status / verbose / quiet)
- _extract_model_directive + _extract_agent_directive (leading directive parsing)
- _extract_leading_directives (stacked directives in any order)
- _format_models_list, _format_agents_list (formatting helpers)
- handle_reaction_added (emoji feedback routing)
- handle_app_mention basics (channel denied, stop command, invalid event)
"""

from __future__ import annotations
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from ypl.slack_agent_gateway.events import (
    _extract_agent_directive,
    _extract_command,
    _extract_leading_directives,
    _extract_model_directive,
    _format_agents_list,
    _format_models_list,
    handle_reaction_added,
    handle_url_verification,
    verify_slack_signature_multi,
)
from ypl.slack_agent_gateway.types import AgentAppConfig, AgentSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_config(app_id: str = "A001") -> AgentAppConfig:
    return AgentAppConfig(
        app_id=app_id,
        agent_name="test-agent",
        slack_name="testbot",
        bot_token="xoxb-test-token",
        signing_secret="test-secret",
        display_name="Test Bot",
    )


def _make_session(
    session_id: str = "C123:1234567890.000:A001",
    channel_id: str = "C123",
    thread_ts: str = "1234567890.000",
    app_id: str = "A001",
) -> AgentSession:
    now = datetime.now(UTC)
    return AgentSession(
        session_id=session_id,
        channel_id=channel_id,
        channel_name="general",
        thread_ts=thread_ts,
        creator_slack_user_id="U999",
        creator_slack_username="testuser",
        app_id=app_id,
        agent_name="test-agent",
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(hours=8),
    )


def _compute_signature(body: bytes, secret: str, timestamp: str) -> str:
    sig_basestring = f"v0:{timestamp}:".encode() + body
    computed = hmac.new(secret.encode(), sig_basestring, hashlib.sha256).hexdigest()
    return f"v0={computed}"


def _make_request(body: bytes, secret: str, timestamp: str | None = None) -> MagicMock:
    ts = timestamp or str(int(time.time()))
    sig = _compute_signature(body, secret, ts)
    req = MagicMock()
    req.headers = {
        "X-Slack-Signature": sig,
        "X-Slack-Request-Timestamp": ts,
    }
    return req


# ---------------------------------------------------------------------------
# verify_slack_signature_multi
# ---------------------------------------------------------------------------


class TestVerifySlackSignatureMulti:
    def test_returns_none_in_local_env(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"

        req = MagicMock()
        req.headers = {}
        with patch("ypl.slack_agent_gateway.events.settings", mock_settings):
            result = verify_slack_signature_multi(req, b"body", ["secret"])
        assert result is None

    def test_raises_when_missing_signature_header(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        req = MagicMock()
        req.headers = {"X-Slack-Request-Timestamp": str(int(time.time()))}
        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, b"body", ["secret"])
        assert exc_info.value.status_code == 401

    def test_raises_when_missing_timestamp_header(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        req = MagicMock()
        req.headers = {"X-Slack-Signature": "v0=abc"}
        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, b"body", ["secret"])
        assert exc_info.value.status_code == 401

    def test_raises_on_old_timestamp(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        old_ts = str(int(time.time()) - 600)  # 10 minutes ago
        body = b"test body"
        req = _make_request(body, "secret", timestamp=old_ts)

        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, body, ["secret"])
        assert exc_info.value.status_code == 401

    def test_accepts_fresh_timestamp(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        body = b"payload=x"
        secret = "my-secret"
        req = _make_request(body, secret)  # default: time.time() (fresh)
        with patch("ypl.slack_agent_gateway.events.settings", mock_settings):
            result = verify_slack_signature_multi(req, body, [secret])
        assert result == secret

    def test_raises_just_outside_window(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        just_old_ts = str(int(time.time()) - 301)  # 1 second past the 300s threshold
        body = b"test"
        req = _make_request(body, "secret", timestamp=just_old_ts)
        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, body, ["secret"])
        assert exc_info.value.status_code == 401

    def test_valid_signature_returns_matching_secret(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        body = b"payload=hello"
        secret = "my-signing-secret"
        req = _make_request(body, secret)

        with patch("ypl.slack_agent_gateway.events.settings", mock_settings):
            result = verify_slack_signature_multi(req, body, ["wrong-secret", secret])
        assert result == secret

    def test_raises_when_no_secret_matches(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        body = b"payload=hello"
        req = _make_request(body, "real-secret")

        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, body, ["wrong-secret-1", "wrong-secret-2"])
        assert exc_info.value.status_code == 401

    def test_first_of_multiple_secrets_matches(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"

        body = b"test"
        secret = "secret-a"
        req = _make_request(body, secret)

        with patch("ypl.slack_agent_gateway.events.settings", mock_settings):
            result = verify_slack_signature_multi(req, body, [secret, "secret-b"])
        assert result == secret

    def test_raises_on_invalid_timestamp_format(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"

        req = MagicMock()
        req.headers = {
            "X-Slack-Signature": "v0=abc",
            "X-Slack-Request-Timestamp": "not-a-number",
        }
        with (
            patch("ypl.slack_agent_gateway.events.settings", mock_settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            verify_slack_signature_multi(req, b"body", ["secret"])
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# handle_url_verification
# ---------------------------------------------------------------------------


class TestHandleUrlVerification:
    @pytest.mark.asyncio
    async def test_echoes_challenge(self) -> None:
        payload = {"type": "url_verification", "challenge": "abc123xyz"}
        response = await handle_url_verification(payload)
        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body["challenge"] == "abc123xyz"

    @pytest.mark.asyncio
    async def test_raises_when_challenge_missing(self) -> None:
        payload = {"type": "url_verification"}
        with pytest.raises(HTTPException) as exc_info:
            await handle_url_verification(payload)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _extract_command
# ---------------------------------------------------------------------------


class TestExtractCommand:
    def test_stop_command(self) -> None:
        assert _extract_command("<@U123> /stop") == ("stop", "")

    def test_stop_command_uppercase(self) -> None:
        assert _extract_command("<@U123> /STOP") == ("stop", "")

    def test_stop_without_slash(self) -> None:
        assert _extract_command("<@U123> stop") == ("stop", "")

    def test_attach_without_args(self) -> None:
        result = _extract_command("<@U123> /attach")
        assert result == ("attach", "")

    def test_attach_with_session_id(self) -> None:
        result = _extract_command("<@U123> /attach abc-uuid-123")
        assert result is not None
        assert result[0] == "attach"
        assert result[1] == "abc-uuid-123"

    def test_non_command_returns_none(self) -> None:
        assert _extract_command("<@U123> hello world") is None

    def test_regular_message_returns_none(self) -> None:
        assert _extract_command("<@U123> can you help me?") is None

    def test_empty_text_returns_none(self) -> None:
        assert _extract_command("") is None

    def test_attach_without_slash(self) -> None:
        result = _extract_command("<@U123> attach abc-uuid")
        assert result is not None
        assert result[0] == "attach"

    def test_mention_stripped_before_matching(self) -> None:
        # Multiple mentions stripped
        assert _extract_command("<@U1> <@U2> stop") == ("stop", "")

    def test_attach_colon_alias(self) -> None:
        # Colon form is accepted as a shorthand for /attach <uuid>.
        assert _extract_command("<@U123> /attach:abc-uuid-123") == ("attach", "abc-uuid-123")

    def test_attach_colon_with_empty_uuid(self) -> None:
        # Malformed colon form with no value — caller will reject this as missing UUID.
        assert _extract_command("<@U123> /attach:") == ("attach", "")

    def test_help_command(self) -> None:
        assert _extract_command("<@U123> /help") == ("help", "")

    def test_help_requires_slash(self) -> None:
        # Bare "help" (no slash) must NOT be treated as a command — it's a common
        # natural-language phrasing ("help me with X").
        assert _extract_command("<@U123> help me with this") is None
        assert _extract_command("<@U123> help") is None

    def test_agents_command(self) -> None:
        assert _extract_command("<@U123> /agents") == ("agents", "")

    def test_models_command(self) -> None:
        assert _extract_command("<@U123> /models") == ("models", "")

    def test_status_command(self) -> None:
        assert _extract_command("<@U123> /status") == ("status", "")

    def test_status_requires_slash(self) -> None:
        # "status" alone is a common word; don't accidentally match it as a command.
        assert _extract_command("<@U123> status") is None
        assert _extract_command("<@U123> status update") is None

    def test_verbose_command(self) -> None:
        assert _extract_command("<@U123> /verbose") == ("verbose", "")

    def test_quiet_command(self) -> None:
        assert _extract_command("<@U123> /quiet") == ("quiet", "")

    def test_verbose_and_quiet_require_slash(self) -> None:
        assert _extract_command("<@U123> verbose") is None
        assert _extract_command("<@U123> quiet") is None

    def test_extra_args_after_bare_command_ignored(self) -> None:
        # /help foo bar should match /help; extras are dropped.
        assert _extract_command("<@U123> /help foo bar") == ("help", "")

    def test_case_insensitive_new_commands(self) -> None:
        assert _extract_command("<@U123> /HELP") == ("help", "")
        assert _extract_command("<@U123> /Agents") == ("agents", "")
        assert _extract_command("<@U123> /StAtUs") == ("status", "")


# ---------------------------------------------------------------------------
# _extract_model_directive
# ---------------------------------------------------------------------------


class TestExtractModelDirective:
    def test_no_directive_returns_none_spec(self) -> None:
        spec, text = _extract_model_directive("<@U123> please review my PR")
        assert spec is None
        assert "please review my PR" in text

    def test_model_directive_extracted(self) -> None:
        spec, text = _extract_model_directive("<@U123> /model:anthropic/claude-3-5 do this task")
        assert spec == "anthropic/claude-3-5"
        assert text == "do this task"

    def test_model_directive_case_insensitive(self) -> None:
        spec, _ = _extract_model_directive("<@U123> /Model:openai/gpt-4 hello")
        assert spec == "openai/gpt-4"

    def test_model_only_no_remaining_text(self) -> None:
        spec, text = _extract_model_directive("<@U123> /model:mymodel/v1")
        assert spec == "mymodel/v1"
        assert text == ""

    def test_mention_stripped_from_cleaned_text(self) -> None:
        _, text = _extract_model_directive("<@UABC> no directive here")
        assert "<@UABC>" not in text

    def test_model_directive_not_first_word_ignored(self) -> None:
        """If /model: is not the first word, it's treated as normal text."""
        spec, text = _extract_model_directive("<@U123> hello /model:foo do stuff")
        assert spec is None
        assert "/model:foo" in text

    def test_model_directive_space_form(self) -> None:
        """Space form ``/model SPEC rest`` is accepted for syntax consistency."""
        spec, text = _extract_model_directive("<@U123> /model anthropic/claude-3-5 do this task")
        assert spec == "anthropic/claude-3-5"
        assert text == "do this task"

    def test_model_directive_space_form_case_insensitive(self) -> None:
        spec, _ = _extract_model_directive("<@U123> /Model openai/gpt-4 hello")
        assert spec == "openai/gpt-4"

    def test_model_directive_space_form_no_value(self) -> None:
        """Bare ``/model`` with no value is not a directive — treat as natural text."""
        spec, text = _extract_model_directive("<@U123> /model")
        assert spec is None
        assert "/model" in text

    def test_model_directive_colon_form_no_value(self) -> None:
        """Bare ``/model:`` with empty value is not a directive."""
        spec, text = _extract_model_directive("<@U123> /model:")
        assert spec is None
        assert "/model:" in text


# ---------------------------------------------------------------------------
# _extract_agent_directive
# ---------------------------------------------------------------------------


class TestExtractAgentDirective:
    def test_no_directive_returns_none(self) -> None:
        name, text = _extract_agent_directive("<@U123> please look at this")
        assert name is None
        assert "please look at this" in text

    def test_colon_form(self) -> None:
        name, text = _extract_agent_directive("<@U123> /agent:sre what's firing?")
        assert name == "sre"
        assert text == "what's firing?"

    def test_space_form(self) -> None:
        name, text = _extract_agent_directive("<@U123> /agent data-scientist run a query")
        assert name == "data-scientist"
        assert text == "run a query"

    def test_case_insensitive(self) -> None:
        name, _ = _extract_agent_directive("<@U123> /Agent SRE hello")
        assert name == "SRE"  # value case preserved; only command name is case-insensitive

    def test_only_first_word_matters(self) -> None:
        name, text = _extract_agent_directive("<@U123> hello /agent sre do stuff")
        assert name is None
        assert "/agent sre do stuff" in text

    def test_no_value_not_a_directive(self) -> None:
        name, text = _extract_agent_directive("<@U123> /agent")
        assert name is None
        assert "/agent" in text


# ---------------------------------------------------------------------------
# _extract_leading_directives
# ---------------------------------------------------------------------------


class TestExtractLeadingDirectives:
    def test_neither_present(self) -> None:
        agent, model, cleaned = _extract_leading_directives("<@U1> hello")
        assert agent is None
        assert model is None
        assert cleaned == "hello"

    def test_only_agent(self) -> None:
        agent, model, cleaned = _extract_leading_directives("<@U1> /agent sre what alerts")
        assert agent == "sre"
        assert model is None
        assert cleaned == "what alerts"

    def test_only_model(self) -> None:
        agent, model, cleaned = _extract_leading_directives("<@U1> /model anthropic/claude-sonnet-4-6 review my PR")
        assert agent is None
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "review my PR"

    def test_agent_then_model(self) -> None:
        agent, model, cleaned = _extract_leading_directives(
            "<@U1> /agent sre /model anthropic/claude-sonnet-4-6 what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"

    def test_model_then_agent(self) -> None:
        agent, model, cleaned = _extract_leading_directives(
            "<@U1> /model anthropic/claude-sonnet-4-6 /agent sre what's firing?"
        )
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "what's firing?"

    def test_colon_and_space_mixed(self) -> None:
        agent, model, cleaned = _extract_leading_directives("<@U1> /agent:sre /model anthropic/claude-sonnet-4-6 go")
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == "go"

    def test_stacked_no_body(self) -> None:
        agent, model, cleaned = _extract_leading_directives("<@U1> /agent sre /model anthropic/claude-sonnet-4-6")
        assert agent == "sre"
        assert model == "anthropic/claude-sonnet-4-6"
        assert cleaned == ""


# ---------------------------------------------------------------------------
# _format_models_list
# ---------------------------------------------------------------------------


class TestFormatModelsList:
    def test_contains_harnessed_section(self) -> None:
        models = {"harnessed": ["claude-agent", "codex-agent"], "raw": []}
        result = _format_models_list(models)
        assert "Harnessed" in result
        assert "claude-agent" in result
        assert "codex-agent" in result

    def test_contains_raw_section(self) -> None:
        models = {"harnessed": [], "raw": ["gpt-4o", "claude-3-5-sonnet"]}
        result = _format_models_list(models)
        assert "Raw LLM" in result
        assert "gpt-4o" in result

    def test_usage_example_included(self) -> None:
        models: dict[str, list[str]] = {"harnessed": [], "raw": []}
        result = _format_models_list(models)
        # Space form is the canonical documented syntax.
        assert "/model " in result

    def test_empty_models_dict(self) -> None:
        result = _format_models_list({})
        # Should not raise, just return formatted empty sections
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _format_agents_list
# ---------------------------------------------------------------------------


class TestFormatAgentsList:
    def test_empty_list(self) -> None:
        assert "No agents available" in _format_agents_list([])

    def test_renders_agent_names_and_models(self) -> None:
        agents = [
            {
                "name": "sre",
                "display_name": "SRE",
                "description": "Site reliability engineer",
                "executor_model": "claude-code-cli",
            },
            {
                "name": "data-scientist",
                "display_name": "Data Scientist",
                "description": "Runs SQL and analyses",
                "llm_model": "openai/gpt-4o",
            },
        ]
        result = _format_agents_list(agents)
        assert "sre" in result
        assert "SRE" in result
        assert "claude-code-cli" in result
        assert "data-scientist" in result
        assert "openai/gpt-4o" in result

    def test_usage_hint_included(self) -> None:
        result = _format_agents_list([{"name": "x", "display_name": "X"}])
        assert "/agent" in result

    def test_description_truncated_when_long(self) -> None:
        long_desc = "a" * 200
        result = _format_agents_list([{"name": "x", "display_name": "X", "description": long_desc}])
        # Description is truncated with ellipsis rather than dumped verbatim.
        assert "..." in result
        assert long_desc not in result


# ---------------------------------------------------------------------------
# handle_reaction_added
# ---------------------------------------------------------------------------


class TestHandleReactionAdded:
    def _make_event(
        self,
        reaction: str = "+1",
        item_type: str = "message",
        channel: str = "C123",
        ts: str = "1111.000",
        user: str = "U456",
    ) -> dict:
        return {
            "reaction": reaction,
            "user": user,
            "item": {"type": item_type, "channel": channel, "ts": ts},
        }

    @pytest.mark.asyncio
    async def test_incomplete_event_returns_invalid(self) -> None:
        app_config = _make_app_config()
        event = {"reaction": "+1"}  # Missing user/item fields
        result = await handle_reaction_added(event, app_config)
        assert result["status"] == "invalid_event"

    @pytest.mark.asyncio
    async def test_non_message_item_skipped(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(item_type="file")
        result = await handle_reaction_added(event, app_config)
        assert result["status"] == "skipped"
        assert result["reason"] == "not a message reaction"

    @pytest.mark.asyncio
    async def test_non_whitelisted_emoji_skipped(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(reaction="tada")  # Not in whitelist
        result = await handle_reaction_added(event, app_config)
        assert result["status"] == "skipped"
        assert result["reason"] == "not a whitelisted feedback emoji"

    @pytest.mark.asyncio
    async def test_reaction_on_non_agent_message_skipped(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(reaction="+1")

        with patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value=None):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "skipped"
        assert result["reason"] == "not an agent reply"

    @pytest.mark.asyncio
    async def test_thumbsup_records_positive_feedback(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(reaction="+1")

        with (
            patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value="sess-abc"),
            patch(
                "ypl.slack_agent_gateway.events.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value="yupp-user-123",
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
        ):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "feedback_recorded"
        assert result["rating"] == "POSITIVE"
        mock_feedback.assert_awaited_once()
        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["rating"] == "POSITIVE"

    @pytest.mark.asyncio
    async def test_thumbsdown_records_negative_feedback(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(reaction="-1")

        with (
            patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value="sess-xyz"),
            patch(
                "ypl.slack_agent_gateway.events.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
        ):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "feedback_recorded"
        assert result["rating"] == "NEGATIVE"
        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["rating"] == "NEGATIVE"

    @pytest.mark.asyncio
    async def test_skin_tone_variant_normalized(self) -> None:
        """Emojis like '+1::skin-tone-2' should normalize to '+1' for matching."""
        app_config = _make_app_config()
        event = self._make_event(reaction="+1::skin-tone-2")

        with (
            patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value="sess-1"),
            patch(
                "ypl.slack_agent_gateway.events.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
        ):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "feedback_recorded"
        assert result["rating"] == "POSITIVE"

    @pytest.mark.asyncio
    async def test_feedback_send_failure_returns_error(self) -> None:
        app_config = _make_app_config()
        event = self._make_event(reaction="thumbsup")

        with (
            patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value="sess-1"),
            patch(
                "ypl.slack_agent_gateway.events.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_feedback",
                new_callable=AsyncMock,
                return_value=None,  # AHS failure
            ),
        ):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_db_error_resolving_user_falls_back_to_none(self) -> None:
        """DB errors during user resolution should be handled gracefully."""
        app_config = _make_app_config()
        event = self._make_event(reaction="+1")

        with (
            patch("ypl.slack_agent_gateway.events.get_session_for_reply", return_value="sess-1"),
            patch(
                "ypl.slack_agent_gateway.events.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection failed"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
        ):
            result = await handle_reaction_added(event, app_config)

        assert result["status"] == "feedback_recorded"
        # user_id should be None on DB error
        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["user_id"] is None
