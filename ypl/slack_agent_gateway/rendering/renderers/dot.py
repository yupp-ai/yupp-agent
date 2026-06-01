"""Graphviz renderer — subprocesses the system ``dot`` binary.

Requires ``graphviz`` to be installed in the container image
(``apt-get install graphviz``). See the project Dockerfile.
"""

from __future__ import annotations
import subprocess

from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError

_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 4 * 1024 * 1024  # 4 MB sanity cap


class DotRenderer:
    """Render Graphviz DOT source to PNG."""

    name = "dot"
    output_filename = "graph.png"

    def render(self, source: str) -> bytes:
        try:
            result = subprocess.run(
                ["dot", "-Tpng"],
                input=source.encode("utf-8"),
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as e:
            raise RenderError("`dot` binary not found — install `graphviz` in the image") from e
        except subprocess.TimeoutExpired as e:
            raise RenderError(f"dot rendering timed out after {_TIMEOUT_SECONDS}s") from e

        if result.returncode != 0:
            stderr_snippet = result.stderr.decode("utf-8", errors="replace")[:200]
            raise RenderError(f"dot exited {result.returncode}: {stderr_snippet}")
        if not result.stdout:
            raise RenderError("dot produced empty output")
        if len(result.stdout) > _MAX_BYTES:
            raise RenderError(f"dot output too large: {len(result.stdout)} bytes")
        return result.stdout


def _factory() -> DotRenderer:
    return DotRenderer()


registry.register("dot", _factory)
