"""Mermaid renderer — uses the public mermaid.ink HTTP service.

We POST the diagram source via the GET URL form (base64-url encoded) and
get back a PNG. This avoids bundling Node + Chromium into the SAG image
(~300MB+ install) at the cost of an outbound HTTP call.

**Privacy caveat:** Mermaid source leaves our cluster. For sensitive
contexts, the documented migration path is a self-hosted Kroki sidecar
(same protocol shape; just change the base URL). Tracked in the design
doc.
"""

from __future__ import annotations
import base64

import httpx

from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError

_MERMAID_INK_BASE = "https://mermaid.ink/img"
_TIMEOUT_SECONDS = 10.0
_MAX_BYTES = 4 * 1024 * 1024  # 4 MB — sanity cap on returned image size


class MermaidRenderer:
    """Render Mermaid diagram source to PNG via mermaid.ink."""

    name = "mermaid"
    output_filename = "diagram.png"

    def render(self, source: str) -> bytes:
        encoded = base64.urlsafe_b64encode(source.strip().encode("utf-8")).decode("ascii")
        url = f"{_MERMAID_INK_BASE}/{encoded}?type=png&bgColor=FFFFFF"
        try:
            response = httpx.get(url, timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as e:
            raise RenderError(f"mermaid.ink request failed: {e}") from e

        if response.status_code != 200:
            # mermaid.ink returns 400 with a small body on parse errors;
            # surface the first 200 chars so logs are useful.
            body_snippet = response.text[:200] if response.text else "<empty>"
            raise RenderError(f"mermaid.ink returned {response.status_code}: {body_snippet}")

        data = response.content
        if not data:
            raise RenderError("mermaid.ink returned empty body")
        if len(data) > _MAX_BYTES:
            raise RenderError(f"mermaid.ink response too large: {len(data)} bytes")
        return data


def _factory() -> MermaidRenderer:
    return MermaidRenderer()


registry.register("mermaid", _factory)
