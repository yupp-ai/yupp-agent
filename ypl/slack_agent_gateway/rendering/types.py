"""Types for the SAG content-rendering pipeline.

These are intentionally plain dataclasses (not Pydantic) because they are
internal to SAG and never crossed the wire. The dispatcher produces a list
of these segments; ``callbacks.add_reply`` consumes them.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class TextSegment:
    """A run of plain text between (or outside) renderable fences."""

    text: str


@dataclass(frozen=True)
class RenderableSegment:
    """A fenced block whose source should be rendered to an image.

    Attributes:
        renderer_name: Canonical renderer key (``mermaid``, ``dot``,
            ``formula``). Aliases (``graphviz``, ``math``, ``latex``) are
            resolved to canonical names by the dispatcher.
        source: Raw fence body — what the agent wrote between the
            opening and closing triple-backticks.
        fence_tag: The literal tag the agent used (``mermaid``,
            ``graphviz``, ``latex``, ...). Preserved verbatim so the
            failure path can reproduce the original code block if
            rendering blows up.
    """

    renderer_name: str
    source: str
    fence_tag: str


class RenderError(RuntimeError):
    """Raised by a renderer when it cannot turn its source into an image.

    The dispatcher / callback layer catches this, logs it, and falls back
    to posting the original fenced source as plain text with a small
    note — content is never silently dropped.
    """
