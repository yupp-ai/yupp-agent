"""Tests for ypl.agent_harness_service.common.constants.

Covers:
- is_personal_agent
- _validate_session_id
- get_session_dir
- TOOLSETS and HARNESS_TO_CLI_TOOL_MAP completeness
- SESSION_INFRA_DIRS contents
- PERSONAL_AGENT_DEFAULT_CONFIG structure
"""

from __future__ import annotations
import uuid

import pytest
from ypl.agent_harness_service.common.constants import (
    HARNESS_TO_CLI_TOOL_MAP,
    PERSONAL_AGENT_DEFAULT_CONFIG,
    SESSION_INFRA_DIRS,
    TOOLSETS,
    _validate_session_id,
    get_session_dir,
    is_personal_agent,
)

# ===========================================================================
# is_personal_agent
# ===========================================================================


class TestIsPersonalAgent:
    def test_yuppclaw_prefix_is_personal(self) -> None:
        assert is_personal_agent("yuppclaw-alice") is True

    def test_yuppclaw_prefix_with_username(self) -> None:
        assert is_personal_agent("yuppclaw-john-doe") is True

    def test_non_personal_agent(self) -> None:
        assert is_personal_agent("sre") is False

    def test_code_reviewer_is_not_personal(self) -> None:
        assert is_personal_agent("code-reviewer") is False

    def test_yuppclaw_without_suffix_not_personal(self) -> None:
        # "yuppclaw" alone doesn't have the "-" suffix → not a personal agent
        assert is_personal_agent("yuppclaw") is False

    def test_partial_prefix_match_not_personal(self) -> None:
        assert is_personal_agent("yuppclawx-alice") is False

    def test_empty_string_not_personal(self) -> None:
        assert is_personal_agent("") is False


# ===========================================================================
# _validate_session_id
# ===========================================================================


class TestValidateSessionId:
    def test_valid_uuid_does_not_raise(self) -> None:
        valid_id = str(uuid.uuid4())
        _validate_session_id(valid_id)  # should not raise

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="required"):
            _validate_session_id("")

    def test_non_uuid_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            _validate_session_id("not-a-uuid")

    def test_path_traversal_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            _validate_session_id("../etc/passwd")

    def test_numeric_string_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            _validate_session_id("12345")

    def test_uuid_with_braces_accepted(self) -> None:
        """Python's uuid.UUID accepts braces — {uuid} is valid."""
        uid = "{" + str(uuid.uuid4()) + "}"
        _validate_session_id(uid)  # should not raise


# ===========================================================================
# get_session_dir
# ===========================================================================


class TestGetSessionDir:
    def test_returns_path_with_session_id(self) -> None:
        session_id = str(uuid.uuid4())
        path = get_session_dir(session_id)
        assert session_id in path

    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            get_session_dir("invalid-id")

    def test_path_is_string(self) -> None:
        session_id = str(uuid.uuid4())
        path = get_session_dir(session_id)
        assert isinstance(path, str)

    def test_path_contains_sessions_dir(self) -> None:
        from ypl.agent_harness_service.common.constants import AHS_SESSIONS_DIR

        session_id = str(uuid.uuid4())
        path = get_session_dir(session_id)
        assert path.startswith(AHS_SESSIONS_DIR)

    def test_deterministic_for_same_id(self) -> None:
        session_id = str(uuid.uuid4())
        assert get_session_dir(session_id) == get_session_dir(session_id)


# ===========================================================================
# TOOLSETS completeness
# ===========================================================================


class TestToolsets:
    def test_readonly_contains_expected_tools(self) -> None:
        readonly = TOOLSETS["@readonly"]
        for tool in ("read", "glob", "grep", "webfetch", "websearch"):
            assert tool in readonly, f"Expected {tool!r} in @readonly toolset"

    def test_readwrite_contains_readonly_tools_plus_write(self) -> None:
        readwrite = TOOLSETS["@readwrite"]
        for tool in ("bash", "write", "edit"):
            assert tool in readwrite, f"Expected {tool!r} in @readwrite toolset"

    def test_readwrite_is_superset_of_readonly(self) -> None:
        readonly = set(TOOLSETS["@readonly"])
        readwrite = set(TOOLSETS["@readwrite"])
        assert readonly.issubset(readwrite)

    def test_toolset_names_start_with_at(self) -> None:
        for key in TOOLSETS:
            assert key.startswith("@"), f"Toolset key {key!r} should start with '@'"


# ===========================================================================
# HARNESS_TO_CLI_TOOL_MAP
# ===========================================================================


class TestHarnessToCliToolMap:
    def test_bash_maps_to_Bash(self) -> None:
        assert HARNESS_TO_CLI_TOOL_MAP["bash"] == "Bash"

    def test_read_maps_to_Read(self) -> None:
        assert HARNESS_TO_CLI_TOOL_MAP["read"] == "Read"

    def test_all_values_are_pascal_case(self) -> None:
        for generic, cli in HARNESS_TO_CLI_TOOL_MAP.items():
            assert cli[0].isupper(), f"CLI name {cli!r} for {generic!r} should be PascalCase"

    def test_write_maps_to_Write(self) -> None:
        assert HARNESS_TO_CLI_TOOL_MAP["write"] == "Write"

    def test_edit_maps_to_Edit(self) -> None:
        assert HARNESS_TO_CLI_TOOL_MAP["edit"] == "Edit"


# ===========================================================================
# SESSION_INFRA_DIRS
# ===========================================================================


class TestSessionInfraDirs:
    def test_contains_history(self) -> None:
        assert "history" in SESSION_INFRA_DIRS

    def test_contains_attachments(self) -> None:
        assert "attachments" in SESSION_INFRA_DIRS

    def test_is_frozenset(self) -> None:
        assert isinstance(SESSION_INFRA_DIRS, frozenset)

    def test_contains_agent_memories(self) -> None:
        assert "agent_memories" in SESSION_INFRA_DIRS


# ===========================================================================
# PERSONAL_AGENT_DEFAULT_CONFIG
# ===========================================================================


class TestPersonalAgentDefaultConfig:
    def test_has_executor_config(self) -> None:
        assert "executor_config" in PERSONAL_AGENT_DEFAULT_CONFIG

    def test_executor_type_is_harnessed(self) -> None:
        assert PERSONAL_AGENT_DEFAULT_CONFIG["executor_config"]["type"] == "harnessed"

    def test_has_mcp_is_true(self) -> None:
        assert PERSONAL_AGENT_DEFAULT_CONFIG["has_mcp"] is True

    def test_has_sandbox_enabled(self) -> None:
        assert PERSONAL_AGENT_DEFAULT_CONFIG["sandbox"]["enabled"] is True

    def test_max_turns_reasonable(self) -> None:
        assert PERSONAL_AGENT_DEFAULT_CONFIG["max_turns"] >= 20

    def test_tool_permissions_allow_all(self) -> None:
        assert PERSONAL_AGENT_DEFAULT_CONFIG["tool_permissions"] == {"*": "allow"}
