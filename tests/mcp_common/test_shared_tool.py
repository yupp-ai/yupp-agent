"""Tests for the ``shared_tool`` dual-registration decorator.

These tests pin the contract that lets phase-3 (mono mcp flag) and
phase-4 (dev-token retirement) drop the AHS → agcouch detour without
breaking agents:

* The decorator forwards every positional and keyword argument to each
  ``FastMCP.tool(...)`` call.
* When ``requires_settings`` is empty the tool registers on every
  instance in ``_INSTANCES`` (currently both harness and agcouch).
* When any required setting is missing the tool is *skipped* on
  credential-gated instances (currently just the harness mount) and a
  ``logger.warning`` is emitted naming the tool and the missing
  settings; the non-gated instances still register so engineer-IDE
  traffic on ``/mcp/agcouch`` fails loudly at call time rather than
  going 404 in the listing.

The tests construct fresh ``FastMCP`` instances and monkey-patch
``_INSTANCES`` / ``_GATED_INSTANCES`` so they don't pollute the real
harness or agcouch registries.
"""

from __future__ import annotations
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from ypl.mcp_common import shared_tool as shared_tool_mod
from ypl.mcp_common.shared_tool import shared_tool


@pytest.fixture
def fresh_instances() -> Iterator[tuple[FastMCP, FastMCP]]:
    """Yield a (harness, agcouch) pair with the harness gated, agcouch not.

    Replaces the module-level ``_INSTANCES`` / ``_GATED_INSTANCES`` for
    the duration of the test so registrations don't bleed into the real
    application instances.
    """
    harness = FastMCP("test-harness")
    agcouch = FastMCP("test-agcouch")
    with (
        patch.object(shared_tool_mod, "_INSTANCES", [harness, agcouch]),
        patch.object(shared_tool_mod, "_GATED_INSTANCES", frozenset({harness})),
    ):
        yield harness, agcouch


async def _registered_tool_names(instance: FastMCP) -> set[str]:
    """Return the set of tool names visible on ``instance`` via the public API."""
    tools = await instance.get_tools()
    return set(tools)


async def test_registers_on_every_instance(fresh_instances: tuple[FastMCP, FastMCP]) -> None:
    """With no creds gate, the tool lands on harness and agcouch alike."""
    harness, agcouch = fresh_instances

    @shared_tool(name="hello")
    async def hello() -> dict[str, Any]:
        return {"ok": True}

    assert "hello" in await _registered_tool_names(harness)
    assert "hello" in await _registered_tool_names(agcouch)


async def test_skips_gated_instances_when_setting_missing(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """Missing ``requires_settings`` skips harness but still registers on agcouch."""
    harness, agcouch = fresh_instances

    with patch.object(shared_tool_mod, "settings") as mock_settings:
        mock_settings.YUPPDB_URL = ""  # missing — gates trip

        @shared_tool(name="needs_yuppdb", requires_settings=("YUPPDB_URL",))
        async def needs_yuppdb() -> dict[str, Any]:
            return {"ok": True}

    harness_tools = await _registered_tool_names(harness)
    agcouch_tools = await _registered_tool_names(agcouch)
    assert "needs_yuppdb" not in harness_tools
    assert "needs_yuppdb" in agcouch_tools


async def test_registers_everywhere_when_setting_present(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """A populated setting clears the gate and the tool registers on harness too."""
    harness, agcouch = fresh_instances

    with patch.object(shared_tool_mod, "settings") as mock_settings:
        mock_settings.YUPPDB_URL = "postgres://yuppdb"  # populated

        @shared_tool(name="needs_yuppdb_ok", requires_settings=("YUPPDB_URL",))
        async def needs_yuppdb_ok() -> dict[str, Any]:
            return {"ok": True}

    harness_tools = await _registered_tool_names(harness)
    agcouch_tools = await _registered_tool_names(agcouch)
    assert "needs_yuppdb_ok" in harness_tools
    assert "needs_yuppdb_ok" in agcouch_tools


async def test_skip_warning_names_tool_and_missing_settings(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """Missing creds emits a structured warning naming the tool + missing keys.

    The codebase uses a structured logger that doesn't route through the
    stdlib ``logging`` module's standard handlers, so we patch
    ``shared_tool_mod.logger`` directly to assert on the kwargs.
    """
    _harness, _agcouch = fresh_instances

    with (
        patch.object(shared_tool_mod, "settings") as mock_settings,
        patch.object(shared_tool_mod, "logger") as mock_logger,
    ):
        mock_settings.YUPPDB_URL = None
        mock_settings.OTHER_THING = ""

        @shared_tool(
            name="needs_two",
            requires_settings=("YUPPDB_URL", "OTHER_THING"),
        )
        async def needs_two() -> dict[str, Any]:
            return {"ok": True}

    mock_logger.warning.assert_called_once()
    args, kwargs = mock_logger.warning.call_args
    assert "credential-gated" in args[0]
    assert kwargs["tool"] == "needs_two"
    assert set(kwargs["missing_settings"]) == {"YUPPDB_URL", "OTHER_THING"}
    # ``test-harness`` is the gated instance from ``fresh_instances``; the
    # decorator names every gated instance it skipped.
    assert "test-harness" in kwargs["skipped_mounts"]


async def test_returns_function_tool_with_fn_attr(fresh_instances: tuple[FastMCP, FastMCP]) -> None:
    """The decorator returns a FunctionTool whose ``.fn`` is the raw coroutine.

    Existing test code accesses the unwrapped coroutine via
    ``module.tool_name.fn`` — the same shape FastMCP's stock decorator
    produces. ``shared_tool`` preserves that contract by returning the
    ``FunctionTool`` from the first registration; every other instance
    has its own ``FunctionTool`` pointing at the same ``fn``.
    """
    _harness, _agcouch = fresh_instances

    @shared_tool(name="passthrough")
    async def passthrough(x: int) -> int:
        return x + 1

    # FunctionTool exposes ``.fn`` for direct test invocation.
    assert hasattr(passthrough, "fn")
    assert await passthrough.fn(41) == 42


async def test_skip_path_still_returns_function_tool(fresh_instances: tuple[FastMCP, FastMCP]) -> None:
    """When harness is gated but agcouch registers, return the agcouch FunctionTool."""
    _harness, _agcouch = fresh_instances

    with patch.object(shared_tool_mod, "settings") as mock_settings:
        mock_settings.YUPPDB_URL = ""

        @shared_tool(name="gated", requires_settings=("YUPPDB_URL",))
        async def gated(x: int) -> int:
            return x * 2

    # Even though harness was skipped, agcouch registered and we return
    # its FunctionTool so call sites can still pull ``.fn``.
    assert hasattr(gated, "fn")
    assert await gated.fn(3) == 6


async def test_env_var_clears_gate(
    fresh_instances: tuple[FastMCP, FastMCP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required cred set in ``os.environ`` (not on ``settings``) clears the gate.

    Pins the post-review extension that lets callers gate on env-only
    creds (e.g. ``SLACK_MCP_SERVER_APP_USER_TOKEN``,
    ``LINEAR_API_KEY``) without first promoting them onto ``Settings``.
    """
    harness, agcouch = fresh_instances

    monkeypatch.setenv("SOME_ENV_TOKEN", "value-from-env")
    with patch.object(shared_tool_mod, "settings") as mock_settings:
        # Settings doesn't carry the env-only token.
        mock_settings.SOME_ENV_TOKEN = None

        @shared_tool(name="env_gated", requires_settings=("SOME_ENV_TOKEN",))
        async def env_gated() -> dict[str, Any]:
            return {"ok": True}

    harness_tools = await _registered_tool_names(harness)
    agcouch_tools = await _registered_tool_names(agcouch)
    assert "env_gated" in harness_tools
    assert "env_gated" in agcouch_tools


async def test_all_skipped_raises_runtime_error(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """When every instance is gated and creds are missing, decoration crashes.

    Pins the loud-failure contract introduced in the round-1 review fix:
    a tool that would land on no MCP server raises ``RuntimeError`` so a
    future ``_INSTANCES`` shrink (phase-3 / phase-4) cannot silently
    de-tooth a tool.
    """
    harness, _agcouch = fresh_instances

    # Make EVERY instance gated.
    with (
        patch.object(shared_tool_mod, "_GATED_INSTANCES", frozenset({harness, _agcouch})),
        patch.object(shared_tool_mod, "settings") as mock_settings,
    ):
        mock_settings.YUPPDB_URL = ""

        with pytest.raises(RuntimeError, match="register on no MCP server"):

            @shared_tool(name="all_gated", requires_settings=("YUPPDB_URL",))
            async def all_gated() -> dict[str, Any]:
                return {"ok": True}


async def test_empty_instances_raises_runtime_error(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """Empty ``_INSTANCES`` (config error) crashes loudly at decoration time."""
    with patch.object(shared_tool_mod, "_INSTANCES", []), pytest.raises(RuntimeError, match="no MCP instance"):

        @shared_tool(name="orphan")
        async def orphan() -> dict[str, Any]:
            return {"ok": True}


async def test_requires_gcp_adc_skips_when_probe_fails(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """``requires_gcp_adc=True`` adds a synthetic missing-cred when ADC probe fails."""
    harness, agcouch = fresh_instances

    with (
        patch.object(shared_tool_mod, "_gcp_adc_available", return_value=False),
        patch.object(shared_tool_mod, "logger") as mock_logger,
    ):

        @shared_tool(name="gcp_dependent", requires_gcp_adc=True)
        async def gcp_dependent() -> dict[str, Any]:
            return {"ok": True}

    harness_tools = await _registered_tool_names(harness)
    agcouch_tools = await _registered_tool_names(agcouch)
    assert "gcp_dependent" not in harness_tools
    assert "gcp_dependent" in agcouch_tools
    # Warning surfaces the synthetic identifier so operators can tell ADC
    # is the failing dependency, not a missing env var.
    mock_logger.warning.assert_called_once()
    _args, kwargs = mock_logger.warning.call_args
    assert "GCP_APPLICATION_DEFAULT_CREDENTIALS" in kwargs["missing_settings"]


async def test_requires_gcp_adc_clears_when_probe_succeeds(
    fresh_instances: tuple[FastMCP, FastMCP],
) -> None:
    """When ADC is available, ``requires_gcp_adc=True`` is a no-op."""
    harness, agcouch = fresh_instances

    with patch.object(shared_tool_mod, "_gcp_adc_available", return_value=True):

        @shared_tool(name="gcp_ok", requires_gcp_adc=True)
        async def gcp_ok() -> dict[str, Any]:
            return {"ok": True}

    assert "gcp_ok" in await _registered_tool_names(harness)
    assert "gcp_ok" in await _registered_tool_names(agcouch)
