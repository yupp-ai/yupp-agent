"""Structural integrity tests for cross-component AHS interactions.

These tests verify that the reorganized module graph is internally consistent:
types from different subpackages are compatible, config loading works, and
the main package init is importable.
"""

from __future__ import annotations
import importlib


class TestCoreConfig:
    """Verify common config module loads and exposes expected symbols."""

    def test_config_module_loads(self) -> None:
        mod = importlib.import_module("ypl.agent_harness_service.common.config")
        assert hasattr(mod, "AgentConfig")
        assert hasattr(mod, "SandboxConfig")
        assert hasattr(mod, "load_agent_config")

    def test_config_classes_are_pydantic(self) -> None:
        from pydantic import BaseModel
        from ypl.agent_harness_service.common.config import AgentConfig, SandboxConfig

        assert issubclass(AgentConfig, BaseModel)
        assert issubclass(SandboxConfig, BaseModel)


class TestCommonModelCompat:
    """Verify common models are consistent and usable."""

    def test_executor_config_importable_from_common_models(self) -> None:
        from ypl.agent_harness_service.common.models import ExecutorConfig

        spec = ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6")
        assert spec.type == "raw"
        assert spec.model == "anthropic/claude-sonnet-4-6"

    def test_agent_spec_uses_executor_config(self) -> None:
        from ypl.agent_harness_service.common.models import AgentSpec, ExecutorConfig

        executor = ExecutorConfig(type="harnessed", model="codex-cli")
        agent = AgentSpec(name="test", executor=executor, max_steps=5)
        assert agent.executor.type == "harnessed"
        assert agent.name == "test"

    def test_tool_permissions_expand(self) -> None:
        from ypl.agent_harness_service.common.models import expand_tool_permissions

        perms: dict[str, str] = {"*": "deny", "read": "allow"}
        result = expand_tool_permissions(perms)  # type: ignore[arg-type]
        assert result["*"] == "deny"
        assert result["read"] == "allow"

    def test_tool_permissions_to_cli_flags(self) -> None:
        from ypl.agent_harness_service.common.models import tool_permissions_to_cli_flags

        allowed, disallowed = tool_permissions_to_cli_flags({"*": "allow"})
        assert allowed is None
        assert disallowed is None


class TestProjectCoreTypeCompat:
    """Verify project types are importable alongside common types."""

    def test_project_types_importable(self) -> None:
        from ypl.agent_harness_service.projects.project_types import TaskSummary

        summary = TaskSummary(PENDING=2, total=2)
        assert summary.PENDING == 2
        assert summary.total == 2

    def test_project_types_are_pydantic(self) -> None:
        from pydantic import BaseModel
        from ypl.agent_harness_service.projects.project_types import (
            ProjectResponse,
            TaskSummary,
        )

        assert issubclass(ProjectResponse, BaseModel)
        assert issubclass(TaskSummary, BaseModel)

    def test_common_and_project_types_coexist(self) -> None:
        """Common types and project types can be imported in the same scope."""
        from ypl.agent_harness_service.common.models import AgentSpec
        from ypl.agent_harness_service.common.types import SessionPermissions
        from ypl.agent_harness_service.projects.project_types import TaskSummary

        assert AgentSpec is not None
        assert SessionPermissions is not None
        assert TaskSummary is not None


class TestMainPackageInit:
    """Verify the top-level __init__.py is importable."""

    def test_top_level_package_importable(self) -> None:
        mod = importlib.import_module("ypl.agent_harness_service")
        assert mod is not None

    def test_common_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.common as common

        assert common is not None

    def test_core_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.core as core

        assert core is not None

    def test_gateway_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.gateway as gateway

        assert gateway is not None

    def test_executors_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.executors as executors

        assert executors is not None

    def test_tools_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.tools as tools

        assert tools is not None

    def test_projects_subpackage_importable_from_top(self) -> None:
        import ypl.agent_harness_service.projects as projects

        assert projects is not None


class TestCommonTypesStructure:
    """Verify common types module exposes expected response models."""

    def test_session_history_response_fields(self) -> None:
        from ypl.agent_harness_service.common.types import SessionHistoryResponse

        field_names = set(SessionHistoryResponse.model_fields.keys())
        assert "session_id" in field_names
        assert "messages" in field_names

    def test_session_permissions_full_access(self) -> None:
        from ypl.agent_harness_service.common.types import SessionPermissions

        perms = SessionPermissions.full_access()
        assert perms.has_full_tool_access is True

    def test_session_permissions_restricted(self) -> None:
        from ypl.agent_harness_service.common.types import SessionPermissions

        perms = SessionPermissions.restricted()
        assert perms.has_full_tool_access is False


class TestCrossComponentReExports:
    """Verify re-exported symbols are accessible from both locations."""

    def test_stream_event_from_common_types(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent

        assert StreamEvent is not None

    def test_stream_event_from_executors_runner(self) -> None:
        from ypl.agent_harness_service.executors.runner import StreamEvent

        assert StreamEvent is not None

    def test_stream_event_is_same_class(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent as CommonStreamEvent
        from ypl.agent_harness_service.executors.runner import StreamEvent as RunnerStreamEvent

        assert CommonStreamEvent is RunnerStreamEvent

    def test_get_session_dir_from_common_constants(self) -> None:
        from ypl.agent_harness_service.common.constants import get_session_dir

        assert callable(get_session_dir)

    def test_get_session_dir_from_workspace_tools(self) -> None:
        from ypl.agent_harness_service.tools.workspace_tools import get_session_dir

        assert callable(get_session_dir)

    def test_get_session_dir_is_same_function(self) -> None:
        from ypl.agent_harness_service.common.constants import get_session_dir as const_fn
        from ypl.agent_harness_service.tools.workspace_tools import get_session_dir as tools_fn

        assert const_fn is tools_fn
