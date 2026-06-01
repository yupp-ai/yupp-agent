"""Tests for the DOT (Graphviz) renderer.

Subprocess calls are mocked so the tests pass on machines without
``graphviz`` installed. The renderer's contract is: stdin gets the DOT
source, ``-Tpng`` produces PNG on stdout, non-zero exit → ``RenderError``.
"""

from __future__ import annotations
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError


def _make_completed(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Construct a subprocess.CompletedProcess-shaped mock."""
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestDotRenderer:
    def test_success_returns_png_bytes(self) -> None:
        renderer = registry.get("dot")
        assert renderer is not None
        png_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"
        with patch(
            "ypl.slack_agent_gateway.rendering.renderers.dot.subprocess.run",
            return_value=_make_completed(0, stdout=png_bytes),
        ) as mock_run:
            result = renderer.render("digraph G { A -> B }")
        assert result == png_bytes
        # Confirm we called `dot -Tpng` with stdin = source bytes.
        args, kwargs = mock_run.call_args
        assert args[0] == ["dot", "-Tpng"]
        assert kwargs["input"] == b"digraph G { A -> B }"

    def test_nonzero_exit_raises_render_error(self) -> None:
        renderer = registry.get("dot")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.dot.subprocess.run",
                return_value=_make_completed(1, stderr=b"syntax error near 'foo'"),
            ),
            pytest.raises(RenderError, match="syntax error"),
        ):
            renderer.render("not actually dot")

    def test_empty_output_raises_render_error(self) -> None:
        renderer = registry.get("dot")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.dot.subprocess.run",
                return_value=_make_completed(0, stdout=b""),
            ),
            pytest.raises(RenderError, match="empty"),
        ):
            renderer.render("digraph G { }")

    def test_missing_binary_raises_render_error(self) -> None:
        renderer = registry.get("dot")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.dot.subprocess.run",
                side_effect=FileNotFoundError("no such file"),
            ),
            pytest.raises(RenderError, match="not found"),
        ):
            renderer.render("digraph G { A -> B }")

    def test_timeout_raises_render_error(self) -> None:
        renderer = registry.get("dot")
        assert renderer is not None
        with (
            patch(
                "ypl.slack_agent_gateway.rendering.renderers.dot.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="dot", timeout=5.0),
            ),
            pytest.raises(RenderError, match="timed out"),
        ):
            renderer.render("digraph G { A -> B }")
