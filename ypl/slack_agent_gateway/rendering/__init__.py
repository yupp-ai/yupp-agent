"""Rich-content rendering for SAG replies.

When an AHS agent emits a fenced block like ``​```mermaid ... ``​`` in
its reply, the dispatcher detects it, hands the source to the matching
renderer, and the resulting PNG is uploaded to Slack via
``files_upload_v2``. Renderers are lazy-loaded — nothing runs until a
matching fence appears.

Public entry points:

- :func:`split` — text → ordered list of ``TextSegment | RenderableSegment``.
- :func:`get_renderer` — fetch a registered renderer by name.

See ``ypl/slack_agent_gateway/rendering/DESIGN.md`` (or the design artifact)
for the full picture.
"""

from __future__ import annotations

# Import renderer modules for registration side effects. The order matches
# the public fence priority (mermaid, dot, formula) but is not semantically
# meaningful — each module only registers its own name(s).
from ypl.slack_agent_gateway.rendering import registry as _registry
from ypl.slack_agent_gateway.rendering.dispatcher import has_renderable, split, text_only
from ypl.slack_agent_gateway.rendering.renderers import dot as _dot_mod  # noqa: F401
from ypl.slack_agent_gateway.rendering.renderers import formula as _formula_mod  # noqa: F401
from ypl.slack_agent_gateway.rendering.renderers import mermaid as _mermaid_mod  # noqa: F401
from ypl.slack_agent_gateway.rendering.types import RenderableSegment, RenderError, TextSegment

get_renderer = _registry.get

__all__ = [
    "RenderError",
    "RenderableSegment",
    "TextSegment",
    "get_renderer",
    "has_renderable",
    "split",
    "text_only",
]
