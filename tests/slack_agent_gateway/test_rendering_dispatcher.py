"""Unit tests for ypl/slack_agent_gateway/rendering/dispatcher.py.

The dispatcher splits an agent's reply text into ordered text and
renderable segments. It must:

- Recognize fenced blocks with registered tags (and aliases).
- Leave unknown / unregistered fences as part of the surrounding text.
- Preserve segment ordering so the caption rebuild matches the original.
- Return a single text segment for input with no fences (so callers can
  rely on a non-empty list for non-empty input).
"""

from __future__ import annotations

from ypl.slack_agent_gateway.rendering import dispatcher
from ypl.slack_agent_gateway.rendering.types import RenderableSegment, TextSegment


class TestSplit:
    def test_plain_text_returns_single_text_segment(self) -> None:
        out = dispatcher.split("just some words")
        assert out == [TextSegment(text="just some words")]

    def test_empty_input_returns_empty_list(self) -> None:
        assert dispatcher.split("") == []

    def test_single_mermaid_fence(self) -> None:
        text = "Here is a diagram:\n\n```mermaid\ngraph TD\n  A --> B\n```\n\nDone."
        out = dispatcher.split(text)
        assert len(out) == 3
        assert isinstance(out[0], TextSegment)
        assert out[0].text == "Here is a diagram:"
        assert isinstance(out[1], RenderableSegment)
        assert out[1].renderer_name == "mermaid"
        assert out[1].source == "graph TD\n  A --> B"
        assert out[1].fence_tag == "mermaid"
        assert isinstance(out[2], TextSegment)
        assert out[2].text == "Done."

    def test_graphviz_alias_resolves_to_dot(self) -> None:
        text = "```graphviz\ndigraph G { A -> B }\n```"
        out = dispatcher.split(text)
        renderables = [s for s in out if isinstance(s, RenderableSegment)]
        assert len(renderables) == 1
        assert renderables[0].renderer_name == "dot"
        assert renderables[0].fence_tag == "graphviz"

    def test_math_and_latex_aliases_resolve_to_formula(self) -> None:
        for alias in ("math", "latex", "formula"):
            text = f"```{alias}\nE = mc^2\n```"
            renderables = [s for s in dispatcher.split(text) if isinstance(s, RenderableSegment)]
            assert len(renderables) == 1
            assert renderables[0].renderer_name == "formula"
            assert renderables[0].fence_tag == alias

    def test_unknown_fence_tag_stays_as_text(self) -> None:
        text = "```python\nprint('hello')\n```"
        out = dispatcher.split(text)
        # Unknown fence — whole text is one segment, fence preserved.
        assert len(out) == 1
        assert isinstance(out[0], TextSegment)
        assert "```python" in out[0].text

    def test_multiple_fences_preserved_in_order(self) -> None:
        text = (
            "First a flow:\n\n```mermaid\nA --> B\n```\n\n"
            "Then a graph:\n\n```dot\ndigraph X { A -> B }\n```\n\n"
            "And a formula:\n\n```formula\nE = mc^2\n```\n"
        )
        out = dispatcher.split(text)
        kinds = [type(s).__name__ for s in out]
        assert kinds == [
            "TextSegment",
            "RenderableSegment",
            "TextSegment",
            "RenderableSegment",
            "TextSegment",
            "RenderableSegment",
        ]
        renderable_names = [s.renderer_name for s in out if isinstance(s, RenderableSegment)]
        assert renderable_names == ["mermaid", "dot", "formula"]

    def test_fence_is_case_insensitive(self) -> None:
        text = "```Mermaid\ngraph LR; A-->B\n```"
        renderables = [s for s in dispatcher.split(text) if isinstance(s, RenderableSegment)]
        assert len(renderables) == 1
        assert renderables[0].renderer_name == "mermaid"

    def test_unknown_fence_does_not_break_subsequent_known_fence(self) -> None:
        text = "First, code:\n```python\nx = 1\n```\nThen a diagram:\n```mermaid\nA --> B\n```\n"
        out = dispatcher.split(text)
        renderables = [s for s in out if isinstance(s, RenderableSegment)]
        assert len(renderables) == 1
        assert renderables[0].renderer_name == "mermaid"
        # The python fence should still appear as part of a text segment.
        joined_text = " ".join(s.text for s in out if isinstance(s, TextSegment))
        assert "```python" in joined_text


class TestHelpers:
    def test_has_renderable_true(self) -> None:
        segs: list[TextSegment | RenderableSegment] = [
            TextSegment(text="hi"),
            RenderableSegment(renderer_name="mermaid", source="A-->B", fence_tag="mermaid"),
        ]
        assert dispatcher.has_renderable(segs) is True

    def test_has_renderable_false(self) -> None:
        segs: list[TextSegment | RenderableSegment] = [TextSegment(text="just text")]
        assert dispatcher.has_renderable(segs) is False

    def test_text_only_joins_text_segments(self) -> None:
        segs: list[TextSegment | RenderableSegment] = [
            TextSegment(text="hello"),
            RenderableSegment(renderer_name="mermaid", source="A-->B", fence_tag="mermaid"),
            TextSegment(text="world"),
        ]
        assert dispatcher.text_only(segs) == "hello\n\nworld"
