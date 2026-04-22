"""Unit tests for ypl/slack_agent_gateway/commands.py.

Covers:
- _build_create_agent_modal (pure data construction)
- process_slack_command (async, signature verification + form parsing mocked)
- _handle_create_agent_command (async, Slack SDK mocked)
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from ypl.slack_agent_gateway.commands import (
    CREATE_AGENT_MODAL_CALLBACK_ID,
    _build_create_agent_modal,
    _handle_create_agent_command,
    process_slack_command,
)

MODULE = "ypl.slack_agent_gateway.commands"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fastapi_request(body: bytes, headers: dict[str, str] | None = None) -> MagicMock:
    """Build a mock FastAPI Request with a known body."""
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    request.headers = headers or {}
    return request


def _encode_form(**kwargs: str) -> bytes:
    return urlencode(kwargs).encode("utf-8")


# ---------------------------------------------------------------------------
# _build_create_agent_modal — pure
# ---------------------------------------------------------------------------


class TestBuildCreateAgentModal:
    def test_returns_dict_with_view_key(self) -> None:
        result = _build_create_agent_modal("trigger-123")
        assert "trigger_id" in result
        assert "view" in result

    def test_trigger_id_is_set(self) -> None:
        result = _build_create_agent_modal("my-trigger-id")
        assert result["trigger_id"] == "my-trigger-id"

    def test_modal_callback_id(self) -> None:
        result = _build_create_agent_modal("tid")
        assert result["view"]["callback_id"] == CREATE_AGENT_MODAL_CALLBACK_ID

    def test_modal_type_is_modal(self) -> None:
        result = _build_create_agent_modal("tid")
        assert result["view"]["type"] == "modal"

    def test_has_required_blocks(self) -> None:
        result = _build_create_agent_modal("tid")
        blocks = result["view"]["blocks"]
        block_ids = [b["block_id"] for b in blocks]
        assert "agent_name_block" in block_ids
        assert "slack_name_block" in block_ids
        assert "display_name_block" in block_ids

    def test_has_optional_blocks(self) -> None:
        result = _build_create_agent_modal("tid")
        blocks = result["view"]["blocks"]
        block_ids = [b["block_id"] for b in blocks]
        assert "description_block" in block_ids
        assert "system_prompt_block" in block_ids

    def test_optional_blocks_are_marked_optional(self) -> None:
        result = _build_create_agent_modal("tid")
        blocks = result["view"]["blocks"]
        optional_blocks = [b for b in blocks if b.get("optional")]
        assert len(optional_blocks) >= 2  # description + system_prompt

    def test_submit_and_close_buttons_exist(self) -> None:
        result = _build_create_agent_modal("tid")
        view = result["view"]
        assert "submit" in view
        assert "close" in view
        assert view["submit"]["text"] == "Submit"
        assert view["close"]["text"] == "Cancel"

    def test_agent_name_block_has_correct_action_id(self) -> None:
        result = _build_create_agent_modal("tid")
        blocks = result["view"]["blocks"]
        agent_name_block = next(b for b in blocks if b["block_id"] == "agent_name_block")
        assert agent_name_block["element"]["action_id"] == "agent_name_input"

    def test_system_prompt_is_multiline(self) -> None:
        result = _build_create_agent_modal("tid")
        blocks = result["view"]["blocks"]
        prompt_block = next(b for b in blocks if b["block_id"] == "system_prompt_block")
        assert prompt_block["element"].get("multiline") is True


# ---------------------------------------------------------------------------
# process_slack_command — async
# ---------------------------------------------------------------------------


class TestProcessSlackCommand:
    async def test_raises_500_when_signing_secret_not_configured(self) -> None:
        body = _encode_form(command="/create-agent", trigger_id="tid", user_id="U001", channel_id="C001")
        request = _make_fastapi_request(body)

        mock_config = {"signing_secret": "", "bot_token": ""}
        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            pytest.raises(HTTPException) as exc_info,
        ):
            await process_slack_command(request)

        assert exc_info.value.status_code == 500

    async def test_raises_400_on_invalid_utf8_body(self) -> None:
        body = b"\xff\xfe"  # Invalid UTF-8
        request = _make_fastapi_request(body)

        mock_config = {"signing_secret": "test-secret", "bot_token": ""}
        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.verify_slack_signature_multi"),
            pytest.raises(HTTPException) as exc_info,
        ):
            await process_slack_command(request)

        assert exc_info.value.status_code == 400

    async def test_handles_create_agent_command(self) -> None:
        body = _encode_form(command="/create-agent", trigger_id="tid-123", user_id="U001", channel_id="C001")
        request = _make_fastapi_request(body)

        mock_config = {"signing_secret": "test-secret", "bot_token": "xoxb-test"}
        mock_client = AsyncMock()
        mock_client.views_open = AsyncMock(return_value={"ok": True})

        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.verify_slack_signature_multi"),
            patch(f"{MODULE}.build_slack_client", return_value=mock_client),
        ):
            response = await process_slack_command(request)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 200

    async def test_returns_unknown_command_response(self) -> None:
        body = _encode_form(command="/unknown-cmd", trigger_id="tid", user_id="U001", channel_id="C001")
        request = _make_fastapi_request(body)

        mock_config = {"signing_secret": "test-secret", "bot_token": ""}
        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.verify_slack_signature_multi"),
        ):
            response = await process_slack_command(request)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 200
        import json

        content = json.loads(response.body)
        assert "Unknown command" in content["text"]

    async def test_verifies_signature_before_processing(self) -> None:
        body = _encode_form(command="/create-agent", trigger_id="tid", user_id="U001", channel_id="C001")
        request = _make_fastapi_request(body)

        mock_config = {"signing_secret": "test-secret", "bot_token": "xoxb-test"}
        mock_verify = MagicMock(side_effect=HTTPException(status_code=403, detail="Bad signature"))

        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.verify_slack_signature_multi", mock_verify),
            pytest.raises(HTTPException) as exc_info,
        ):
            await process_slack_command(request)

        assert exc_info.value.status_code == 403
        mock_verify.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_create_agent_command — async
# ---------------------------------------------------------------------------


class TestHandleCreateAgentCommand:
    async def test_returns_ephemeral_when_no_bot_token(self) -> None:
        mock_config = {"bot_token": "", "signing_secret": "sec"}
        with patch(f"{MODULE}.get_bot_father_config", return_value=mock_config):
            response = await _handle_create_agent_command("tid", "U001")

        assert isinstance(response, JSONResponse)
        import json

        content = json.loads(response.body)
        assert "not configured" in content["text"].lower()

    async def test_opens_modal_successfully(self) -> None:
        mock_config = {"bot_token": "xoxb-test-token", "signing_secret": "sec"}
        mock_client = AsyncMock()
        mock_client.views_open = AsyncMock(return_value={"ok": True})

        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.build_slack_client", return_value=mock_client),
        ):
            response = await _handle_create_agent_command("trigger-abc", "U123")

        assert isinstance(response, JSONResponse)
        assert response.status_code == 200
        mock_client.views_open.assert_awaited_once()
        call_kwargs = mock_client.views_open.call_args[1]
        assert call_kwargs["trigger_id"] == "trigger-abc"

    async def test_returns_error_message_when_modal_open_fails(self) -> None:
        mock_config = {"bot_token": "xoxb-test-token", "signing_secret": "sec"}
        mock_client = AsyncMock()
        mock_client.views_open = AsyncMock(side_effect=Exception("Slack API error"))

        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.build_slack_client", return_value=mock_client),
        ):
            response = await _handle_create_agent_command("tid", "U001")

        assert isinstance(response, JSONResponse)
        import json

        content = json.loads(response.body)
        assert "Failed" in content["text"] or "failed" in content["text"]

    async def test_passes_correct_view_to_views_open(self) -> None:
        mock_config = {"bot_token": "xoxb-token", "signing_secret": "sec"}
        mock_client = AsyncMock()
        mock_client.views_open = AsyncMock(return_value={"ok": True})

        with (
            patch(f"{MODULE}.get_bot_father_config", return_value=mock_config),
            patch(f"{MODULE}.build_slack_client", return_value=mock_client),
        ):
            await _handle_create_agent_command("my-trigger", "U555")

        call_kwargs = mock_client.views_open.call_args[1]
        assert "view" in call_kwargs
        assert call_kwargs["view"]["type"] == "modal"
        assert call_kwargs["view"]["callback_id"] == CREATE_AGENT_MODAL_CALLBACK_ID
