"""Reply-text dispatcher — split into text and renderable segments.

The dispatcher does one thing: scan an agent's reply for fenced blocks
whose language tag matches a registered renderer (or one of its aliases),
and produce an ordered list of segments. It does NOT call renderers —
that's the integration layer's job.

Fence syntax recognized::

    ```<tag>
    <source>
    ```

The opening triple-backticks must start a line. The closing
triple-backticks must also start a line. ``<tag>`` is a lowercase
identifier; aliases are resolved to canonical renderer names.
"""

from __future__ import annotations
import re

from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderableSegment, TextSegment

# Maps fence-tag aliases to canonical renderer names. ``graphviz`` is the
# common alternative spelling for ``dot``; ``math`` and ``latex`` are what
# LLMs spontaneously emit for formula expressions even though the
# underlying engine is matplotlib mathtext (a LaTeX-math subset).
_FENCE_ALIASES: dict[str, str] = {
    "mermaid": "mermaid",
    "dot": "dot",
    "graphviz": "dot",
    "formula": "formula",
    "math": "formula",
    "latex": "formula",
}

# Fence regex. Matches at line start (^ with MULTILINE), captures the tag
# and body, requires the closing fence at line start. Non-greedy body so
# multiple fences in one reply don't merge.
_FENCE_RE = re.compile(
    r"^```([a-zA-Z][a-zA-Z0-9_+-]*)[ \t]*\n(.*?)\n```[ \t]*(?=\n|$)",
    re.MULTILINE | re.DOTALL,
)


def split(text: str) -> list[TextSegment | RenderableSegment]:
    """Split ``text`` into ordered text and renderable segments.

    Unknown fence tags (e.g. ``​```python``​``) pass through as part of the
    surrounding text segment — Slack will render them as code blocks.

    Args:
        text: The full reply text from the agent.

    Returns:
        Ordered list where each element is either ``TextSegment`` (for
        plain text or unrenderable fences) or ``RenderableSegment`` (for
        recognized fences). Empty text segments are dropped.
    """
    segments: list[TextSegment | RenderableSegment] = []
    cursor = 0

    for match in _FENCE_RE.finditer(text):
        tag = match.group(1).lower()
        renderer_name = _FENCE_ALIASES.get(tag)
        if renderer_name is None or not registry.is_registered(renderer_name):
            # Unknown / unregistered — leave fence inside the surrounding
            # text segment. The cursor doesn't advance past it; we'll keep
            # scanning for the next renderable fence.
            continue

        # Emit the text run that precedes this fence (if any).
        preceding = text[cursor : match.start()]
        if preceding.strip():
            segments.append(TextSegment(text=preceding.strip("\n")))

        segments.append(
            RenderableSegment(
                renderer_name=renderer_name,
                source=match.group(2),
                fence_tag=tag,
            )
        )
        cursor = match.end()

    # Trailing text after the last renderable fence.
    if cursor < len(text):
        tail = text[cursor:]
        if tail.strip():
            segments.append(TextSegment(text=tail.strip("\n")))

    # If no renderables were found, emit the whole text as one segment so
    # callers can rely on a non-empty list for non-empty input.
    if not segments and text:
        segments.append(TextSegment(text=text))

    return segments


def has_renderable(segments: list[TextSegment | RenderableSegment]) -> bool:
    """Return True if any segment is a RenderableSegment."""
    return any(isinstance(s, RenderableSegment) for s in segments)


def text_only(segments: list[TextSegment | RenderableSegment]) -> str:
    """Join all text segments into a single string (renderables omitted)."""
    return "\n\n".join(s.text for s in segments if isinstance(s, TextSegment))
