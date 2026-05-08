"""Test-time defaults for mono_server tests.

Both master flags (``AHS_MONO_ENABLE_GATEWAY_SERVICE`` and
``AHS_MONO_ENABLE_MCP``) default to *False* in production (the
pure-AHS deployment shape — see ``DEPLOYMENT.md``).  The bulk of the
mono-server test suite was written assuming the legacy "AHS + SAG +
agcouch MCP" shape, so we flip both flags to ``True`` here at conftest
import time — *before* any test module imports ``ypl.mono_server.server``
and triggers the module-level ``app = create_app()`` call.

Tests that specifically exercise the off-default behaviour (the new
"pure AHS" shape) must explicitly override one or both flags via
``patch.dict(os.environ, {...})`` and call ``create_app()`` to get a
fresh app with the desired config — see
``test_master_flags.py::TestPureAhsDefaults`` for examples.

Use ``setdefault`` rather than direct assignment so a developer running
``AHS_MONO_ENABLE_GATEWAY_SERVICE=false pytest tests/mono_server/`` to
debug the new-default shape can override from the shell without this
conftest stomping on them.
"""

from __future__ import annotations
import os

# Set BEFORE any mono-server test imports `ypl.mono_server.server`.
# Without this, the module-level `app` would be the new "pure AHS" shape
# (no /mcp/agcouch, no /gw/slack), and the legacy tests that assert
# those mounts exist would fail across the board.
os.environ.setdefault("AHS_MONO_ENABLE_GATEWAY_SERVICE", "true")
os.environ.setdefault("AHS_MONO_ENABLE_MCP", "true")
