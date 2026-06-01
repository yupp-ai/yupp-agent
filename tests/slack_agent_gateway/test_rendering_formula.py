"""Tests for the formula renderer (matplotlib.mathtext).

These tests run a real matplotlib render so we exercise the actual
mathtext parser. They're fast (~50ms each after the first import) and
deterministic enough for CI.
"""

from __future__ import annotations

import pytest
from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError


class TestFormulaRenderer:
    def test_renders_simple_expression(self) -> None:
        renderer = registry.get("formula")
        assert renderer is not None
        png = renderer.render("E = mc^2")
        # PNG magic bytes
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        # Some non-trivial size — anything under 1 KB likely means we got
        # a blank image, which would indicate broken rendering.
        assert len(png) > 1024

    def test_strips_wrapping_dollar_signs(self) -> None:
        renderer = registry.get("formula")
        assert renderer is not None
        wrapped = renderer.render("$\\sum_{i=0}^n x_i$")
        unwrapped = renderer.render("\\sum_{i=0}^n x_i")
        # Both should produce a valid PNG.
        assert wrapped.startswith(b"\x89PNG")
        assert unwrapped.startswith(b"\x89PNG")

    def test_empty_source_raises_render_error(self) -> None:
        renderer = registry.get("formula")
        assert renderer is not None
        with pytest.raises(RenderError, match="empty"):
            renderer.render("   ")

    def test_invalid_expression_raises_render_error(self) -> None:
        renderer = registry.get("formula")
        assert renderer is not None
        # Mismatched braces — mathtext should reject this.
        with pytest.raises(RenderError):
            renderer.render("\\frac{1}{")

    def test_output_filename_metadata(self) -> None:
        renderer = registry.get("formula")
        assert renderer is not None
        assert renderer.output_filename == "formula.png"
        assert renderer.name == "formula"
