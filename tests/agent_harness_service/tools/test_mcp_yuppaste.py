"""Unit tests for ypl/mcp_server/tools/yuppaste.py."""

from __future__ import annotations
import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.mcp_server.tools.yuppaste import mcp_create_yuppaste, mcp_read_yuppaste

_FAKE_SIGNED_URL = "/p/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee?sig=abc123&exp=9999999999"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paste_result(
    paste_uuid: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    name: str | None = "My Paste",
    data: str | None = "hello content",
    named_slug: str | None = None,
    version: int = 1,
    redirect_url: str | None = None,
    file_size: int | None = None,
    attachments: list | None = None,
) -> MagicMock:
    r = MagicMock()
    r.uuid = paste_uuid
    r.name = name
    r.data = data
    r.content_url = f"pastes/{paste_uuid[:2]}/{paste_uuid}/{paste_uuid}.txt"
    r.created_by = "user@example.com"
    r.created_at = datetime(2024, 1, 15, tzinfo=UTC)
    r.named_slug = named_slug
    r.version = version
    r.redirect_url = redirect_url
    r.file_size = file_size
    r.attachments = attachments or []
    return r


VALID_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_USER_ID = "user-123"


# ---------------------------------------------------------------------------
# mcp_create_yuppaste
# ---------------------------------------------------------------------------


class TestMcpCreateYuppaste:
    async def test_success_simple(self) -> None:
        paste = _make_paste_result()

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
            patch("ypl.mcp_server.tools.yuppaste._try_sign_url", return_value=_FAKE_SIGNED_URL),
        ):
            result = await mcp_create_yuppaste.fn(content="hello")

        assert result["success"] is True
        assert result["go_link"] == "http://go/p/abc"
        assert result["uuid"] == VALID_UUID

    async def test_signed_url_included_when_signing_configured(self) -> None:
        paste = _make_paste_result()

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
            patch("ypl.mcp_server.tools.yuppaste._try_sign_url", return_value=_FAKE_SIGNED_URL),
        ):
            result = await mcp_create_yuppaste.fn(content="hello")

        assert result["success"] is True
        assert "signed_url" in result
        assert result["signed_url"] == _FAKE_SIGNED_URL

    async def test_signed_url_omitted_when_signing_not_configured(self) -> None:
        paste = _make_paste_result()

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
            patch("ypl.mcp_server.tools.yuppaste._try_sign_url", return_value=None),
        ):
            result = await mcp_create_yuppaste.fn(content="hello")

        assert result["success"] is True
        assert "signed_url" not in result

    async def test_signed_url_included_for_attachment_path(self) -> None:
        paste = _make_paste_result()

        valid_b64 = base64.b64encode(b"image data").decode()
        attachments_json = __import__("json").dumps([{"filename": "img.png", "content_base64": valid_b64}])

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste_with_attachments",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
            patch("ypl.mcp_server.tools.yuppaste._try_sign_url", return_value=_FAKE_SIGNED_URL),
        ):
            result = await mcp_create_yuppaste.fn(content="test", attachments=attachments_json)

        assert result["success"] is True
        assert result["signed_url"] == _FAKE_SIGNED_URL

    async def test_content_too_large(self) -> None:
        big_content = "x" * (10 * 1024 * 1024 + 1)
        result = await mcp_create_yuppaste.fn(content=big_content)
        assert result["success"] is False
        assert "exceeds maximum" in result["error"]

    async def test_no_auth_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=None),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value=None),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="unknown"),
        ):
            result = await mcp_create_yuppaste.fn(content="hello")

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_create_new_slug_without_named_slug_fails(self) -> None:
        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
        ):
            result = await mcp_create_yuppaste.fn(content="test", create_new_slug=True, named_slug=None)

        assert result["success"] is False
        assert "create_new_slug requires named_slug" in result["error"]

    async def test_invalid_attachments_json(self) -> None:
        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
        ):
            result = await mcp_create_yuppaste.fn(content="test", attachments="{not valid json}")

        assert result["success"] is False
        assert "Invalid attachments JSON" in result["error"]

    async def test_attachments_must_be_array(self) -> None:
        import json

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
        ):
            result = await mcp_create_yuppaste.fn(
                content="test",
                attachments=json.dumps({"filename": "f.png", "content_base64": "abc"}),
            )

        assert result["success"] is False
        assert "must be a JSON array" in result["error"]

    async def test_attachment_missing_fields(self) -> None:
        import json

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
        ):
            result = await mcp_create_yuppaste.fn(
                content="test",
                attachments=json.dumps([{"filename": "f.png"}]),  # missing content_base64
            )

        assert result["success"] is False
        assert "filename" in result["error"] or "content_base64" in result["error"]

    async def test_invalid_base64_attachment(self) -> None:
        import json

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
        ):
            result = await mcp_create_yuppaste.fn(
                content="test",
                attachments=json.dumps([{"filename": "f.png", "content_base64": "!!!not_base64!!!"}]),
            )

        assert result["success"] is False
        assert "Invalid base64" in result["error"]

    async def test_with_attachments_success(self) -> None:
        import json

        paste = _make_paste_result()

        valid_b64 = base64.b64encode(b"image data").decode()
        attachments_json = json.dumps([{"filename": "img.png", "content_base64": valid_b64}])

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste_with_attachments",
                AsyncMock(return_value=paste),
            ) as mock_create_attachments,
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
        ):
            result = await mcp_create_yuppaste.fn(content="test", attachments=attachments_json)

        assert result["success"] is True
        # Verify the decoded attachment was passed correctly
        mock_create_attachments.assert_called_once()
        attachments_arg = mock_create_attachments.call_args[1]["attachments"]
        assert len(attachments_arg) == 1
        filename, file_bytes, content_type = attachments_arg[0]
        assert filename == "img.png"
        assert file_bytes == b"image data"

    async def test_with_attachments_exception(self) -> None:
        import json

        valid_b64 = base64.b64encode(b"image data").decode()
        attachments_json = json.dumps([{"filename": "img.png", "content_base64": valid_b64}])

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste_with_attachments",
                AsyncMock(side_effect=RuntimeError("storage error")),
            ),
        ):
            result = await mcp_create_yuppaste.fn(content="test", attachments=attachments_json)

        assert result["success"] is False
        assert "error" in result

    async def test_named_slug_included_in_response(self) -> None:
        paste = _make_paste_result(named_slug="my-report", version=3)

        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste",
                AsyncMock(return_value=paste),
            ),
            patch(
                "ypl.mcp_server.tools.yuppaste.generate_yuppaste_slug_link",
                return_value="http://go/p/my-report/3",
            ),
        ):
            result = await mcp_create_yuppaste.fn(content="test", named_slug="my-report")

        assert result["success"] is True
        assert result["named_slug"] == "my-report"
        assert result["version"] == 3

    async def test_backend_value_error_returned(self) -> None:
        with (
            patch("ypl.mcp_server.tools.yuppaste.get_requesting_user_id", return_value=FAKE_USER_ID),
            patch(
                "ypl.mcp_server.tools.yuppaste.resolve_email_from_user_id",
                AsyncMock(return_value="user@example.com"),
            ),
            patch("ypl.mcp_server.tools.yuppaste.get_authenticated_user_email", return_value="user@example.com"),
            patch(
                "ypl.mcp_server.tools.yuppaste.create_yuppaste",
                AsyncMock(side_effect=ValueError("invalid slug")),
            ),
        ):
            result = await mcp_create_yuppaste.fn(content="test")

        assert result["success"] is False
        assert "invalid slug" in result["error"]


# ---------------------------------------------------------------------------
# mcp_read_yuppaste
# ---------------------------------------------------------------------------


class TestMcpReadYuppaste:
    async def test_success_by_uuid(self) -> None:
        paste = _make_paste_result()

        with (
            patch(
                "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_uuid",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
        ):
            result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID)

        assert result["success"] is True
        assert result["uuid"] == VALID_UUID
        assert result["content"] == "hello content"

    async def test_success_by_slug(self) -> None:
        paste = _make_paste_result(named_slug="my-report", version=2)

        with (
            patch(
                "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_slug",
                AsyncMock(return_value=paste),
            ),
            patch(
                "ypl.mcp_server.tools.yuppaste.generate_yuppaste_slug_link",
                return_value="http://go/p/my-report/2",
            ),
        ):
            result = await mcp_read_yuppaste.fn(named_slug="my-report")

        assert result["success"] is True
        assert result["named_slug"] == "my-report"
        assert result["version"] == 2

    async def test_both_uuid_and_slug_returns_error(self) -> None:
        result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID, named_slug="my-report")
        assert result["success"] is False
        assert "not both" in result["error"]

    async def test_neither_uuid_nor_slug_returns_error(self) -> None:
        result = await mcp_read_yuppaste.fn()
        assert result["success"] is False
        assert "Must provide either" in result["error"]

    async def test_not_found_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_uuid",
            AsyncMock(side_effect=ValueError("Paste not found")),
        ):
            result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID)

        assert result["success"] is False
        assert "Paste not found" in result["error"]

    async def test_redirect_url_included(self) -> None:
        paste = _make_paste_result(data=None, redirect_url="https://storage.example.com/big.txt")

        with (
            patch(
                "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_uuid",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
        ):
            result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID)

        assert result["success"] is True
        assert result["content"] is None
        assert "redirect_url" in result

    async def test_file_size_included_when_present(self) -> None:
        paste = _make_paste_result(file_size=12345)

        with (
            patch(
                "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_uuid",
                AsyncMock(return_value=paste),
            ),
            patch("ypl.mcp_server.tools.yuppaste.generate_yuppaste_link", return_value="http://go/p/abc"),
        ):
            result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID)

        assert result["file_size_bytes"] == 12345

    async def test_generic_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.yuppaste.get_yuppaste_by_uuid",
            AsyncMock(side_effect=RuntimeError("storage error")),
        ):
            result = await mcp_read_yuppaste.fn(paste_uuid=VALID_UUID)

        assert result["success"] is False
        assert "unexpected error" in result["error"].lower()
