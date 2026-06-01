"""Formula renderer — renders math / physics-unit / chemistry expressions
to PNG using matplotlib's mathtext engine.

In-process, no subprocess. Covers what LLMs emit in chat: math symbols,
sub/super-scripts, fractions, integrals, matrices, Greek letters, and the
expressions you'd use for physics units (``\\frac{m}{s^2}``) or simple
chemistry stoichiometry (``H_2O + CO_2``). Not full LaTeX — multi-page
documents are out of scope.

The fence is ``​```formula`` (canonical); the dispatcher also routes
``​```math`` and ``​```latex`` here.
"""

from __future__ import annotations
import io

from ypl.slack_agent_gateway.rendering import registry
from ypl.slack_agent_gateway.rendering.types import RenderError

_FONT_SIZE = 20
_DPI = 200
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB sanity cap


class FormulaRenderer:
    """Render a math / formula expression to PNG via matplotlib mathtext."""

    name = "formula"
    output_filename = "formula.png"

    def render(self, source: str) -> bytes:
        # Lazy import — matplotlib is a heavy module we don't want to pay
        # for at SAG startup. First call costs ~400ms; subsequent calls
        # are cheap.
        import matplotlib

        matplotlib.use("Agg")  # headless backend
        import matplotlib.pyplot as plt

        expression = source.strip()
        # Strip the wrapping ``$...$`` if the agent included them — mathtext
        # adds them itself.
        if expression.startswith("$") and expression.endswith("$"):
            expression = expression[1:-1].strip()
        if not expression:
            raise RenderError("formula source is empty")

        fig = plt.figure(figsize=(0.01, 0.01))
        try:
            fig.text(0, 0, f"${expression}$", fontsize=_FONT_SIZE)
            buf = io.BytesIO()
            try:
                fig.savefig(
                    buf,
                    format="png",
                    dpi=_DPI,
                    bbox_inches="tight",
                    pad_inches=0.15,
                )
            except (ValueError, RuntimeError) as e:
                # mathtext raises ValueError for unparseable expressions
                # (mismatched braces, unknown symbols).
                raise RenderError(f"matplotlib mathtext could not parse formula: {e}") from e
        finally:
            plt.close(fig)

        data = buf.getvalue()
        if not data:
            raise RenderError("matplotlib produced empty output")
        if len(data) > _MAX_BYTES:
            raise RenderError(f"formula output too large: {len(data)} bytes")
        return data


def _factory() -> FormulaRenderer:
    return FormulaRenderer()


registry.register("formula", _factory)
