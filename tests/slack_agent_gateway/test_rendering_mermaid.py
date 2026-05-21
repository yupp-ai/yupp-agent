"""Tests for the Mermaid renderer (calls public mermaid.ink HTTP service).

HTTP calls are mocked so tests are hermetic. The renderer's contract:
base64-url-encode the source, hit ``GET mermaid.ink/img/<encoded>``,
expect PNG bytes back on 200 and ``RenderError`` on anything else.
"""

from __future__ import annotations
import base64
from unittest.mock import MagicMock, patch

import httpx
import pytest
from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError


def _make_response(status_code: int, content: bytes = b"", text: str = "") -> MagicMock:
    """Construct a minimal httpx.Response-shaped mock."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = content
    resp.text = text or content.decode("utf-8", errors="ignore")
    return resp


class TestMermaidRenderer:
    def test_success_returns_png_bytes(self) -> None:
        renderer = registry.get("mermaid")
        assert renderer is not None
        png = b"\x89PNG\r\n\x1a\nfake-mermaid-image"
        with patch(
            "ypl.slack_agent_gateway.rendering.renderers.mermaid.httpx.get",
            return_value=_make_response(200, content=png),
        ) as mock_get:
            result = renderer.render("graph TD\n  A --> B")
        assert result == png
        # Verify the URL encodes the source via base64-url and hits mermaid.ink.
        called_url = mock_get.call_args[0][0]
        assert called_url.startswith("https://mermaid.ink/img/")
        # Extract the path component and decode it back; should match input.
        encoded = called_url.split("/")[-1].split("?", 1)[0]
        decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
        assert decoded == "graph TD\n  A --> B"

    def test_non_200_raises_render_error(self) -> None:
        renderer = registry.get("mermaid")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.mermaid.httpx.get",
                return_value=_make_response(400, text="parse error: unexpected token"),
            ),
            pytest.raises(RenderError, match="400"),
        ):
            renderer.render("not valid mermaid")

    def test_empty_body_raises_render_error(self) -> None:
        renderer = registry.get("mermaid")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.mermaid.httpx.get",
                return_value=_make_response(200, content=b""),
            ),
            pytest.raises(RenderError, match="empty"),
        ):
            renderer.render("graph TD; A --> B")

    def test_network_error_raises_render_error(self) -> None:
        renderer = registry.get("mermaid")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.mermaid.httpx.get",
                side_effect=httpx.ConnectError("name resolution failed"),
            ),
            pytest.raises(RenderError, match="request failed"),
        ):
            renderer.render("graph TD; A --> B")
