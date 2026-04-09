"""Unit tests for ypl/agent_harness_service/tools/sandboxed_ops.py.

Covers:
- _wrap_binary_result: extension mapping (jpg→jpeg, known docs, unknown ext pass-through)
- mcp_read: delegates to read_file, wraps binary result, validates session_id
- mcp_write: delegates to write_file, validates session_id
- mcp_edit: delegates to edit_file, validates session_id
- mcp_glob: delegates to list_files, validates session_id
- mcp_grep: delegates to search_files, validates session_id
- mcp_bash: validates session_id, BCH manager path, fallback bwrap path
- mcp_webfetch: validates session_id, delegates to fetch_url
- mcp_websearch: validates session_id, per-turn/per-session rate limiting,
  counter rollback on failure, counter increments
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.utilities.types import File, Image

# Import the FunctionTool objects and unwrap via .fn per project convention
# (see test_workspace_mcp.py for this pattern)
from ypl.agent_harness_service.tools.sandboxed_ops import (
    _wrap_binary_result,
    mcp_bash,
    mcp_edit,
    mcp_glob,
    mcp_grep,
    mcp_read,
    mcp_webfetch,
    mcp_websearch,
    mcp_write,
)
from ypl.agent_harness_service.tools.workspace_tools import BinaryFileResult

# Unwrap FunctionTool wrappers to get the raw callables (see test_workspace_mcp.py)
_mcp_read = mcp_read.fn
_mcp_write = mcp_write.fn
_mcp_edit = mcp_edit.fn
_mcp_glob = mcp_glob.fn
_mcp_grep = mcp_grep.fn
_mcp_bash = mcp_bash.fn
_mcp_webfetch = mcp_webfetch.fn
_mcp_websearch = mcp_websearch.fn

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INVALID_UUID = "not-a-valid-uuid"

_OPS_MODULE = "ypl.agent_harness_service.tools.sandboxed_ops"


# ---------------------------------------------------------------------------
# _wrap_binary_result
# ---------------------------------------------------------------------------


class TestWrapBinaryResult:
    def _make_result(self, extension: str, category: str, data: bytes = b"data") -> BinaryFileResult:
        result = MagicMock(spec=BinaryFileResult)
        result.extension = extension
        result.category = category
        result.data = data
        return result

    def test_jpg_maps_to_jpeg_image(self) -> None:
        result = self._make_result("jpg", "image")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, Image)

    def test_png_maps_to_png_image(self) -> None:
        result = self._make_result("png", "image")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, Image)

    def test_pdf_maps_to_pdf_file(self) -> None:
        result = self._make_result("pdf", "document")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, File)

    def test_docx_maps_to_file(self) -> None:
        result = self._make_result("docx", "document")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, File)

    def test_doc_maps_to_msword_file(self) -> None:
        result = self._make_result("doc", "document")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, File)

    def test_unknown_extension_passed_through_as_file(self) -> None:
        result = self._make_result("xyz", "document")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, File)

    def test_unknown_extension_image_category(self) -> None:
        result = self._make_result("bmp", "image")
        wrapped = _wrap_binary_result(result)
        assert isinstance(wrapped, Image)


# ---------------------------------------------------------------------------
# mcp_read
# ---------------------------------------------------------------------------


class TestMcpRead:
    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _mcp_read(INVALID_UUID, "file.txt")

    def test_returns_string_for_text_file(self) -> None:
        with (
            patch(
                f"{_OPS_MODULE}.read_file",
                return_value="line 1\nline 2",
            ),
        ):
            result = _mcp_read(VALID_UUID, "file.txt")
        assert result == "line 1\nline 2"

    def test_returns_image_for_binary_image(self) -> None:
        binary_result = MagicMock(spec=BinaryFileResult)
        binary_result.extension = "png"
        binary_result.category = "image"
        binary_result.data = b"\x89PNG"

        with patch(f"{_OPS_MODULE}.read_file", return_value=binary_result):
            result = _mcp_read(VALID_UUID, "image.png")
        assert isinstance(result, Image)

    def test_passes_offset_and_limit(self) -> None:
        with patch(f"{_OPS_MODULE}.read_file", return_value="content") as mock_read:
            _mcp_read(VALID_UUID, "file.txt", offset=10, limit=50)
        mock_read.assert_called_once_with(VALID_UUID, "file.txt", 10, 50)


# ---------------------------------------------------------------------------
# mcp_write
# ---------------------------------------------------------------------------


class TestMcpWrite:
    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _mcp_write(INVALID_UUID, "file.txt", "content")

    def test_delegates_to_write_file(self) -> None:
        with patch(f"{_OPS_MODULE}.write_file", return_value="Written 7 bytes") as mock_write:
            result = _mcp_write(VALID_UUID, "output.txt", "content")
        mock_write.assert_called_once_with(VALID_UUID, "output.txt", "content")
        assert result == "Written 7 bytes"


# ---------------------------------------------------------------------------
# mcp_edit
# ---------------------------------------------------------------------------


class TestMcpEdit:
    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _mcp_edit(INVALID_UUID, "file.txt", "old", "new")

    def test_delegates_to_edit_file(self) -> None:
        with patch(f"{_OPS_MODULE}.edit_file", return_value="Edited 1 occurrence") as mock_edit:
            result = _mcp_edit(VALID_UUID, "file.py", "old_name", "new_name", replace_all=True)
        mock_edit.assert_called_once_with(VALID_UUID, "file.py", "old_name", "new_name", True)
        assert result == "Edited 1 occurrence"


# ---------------------------------------------------------------------------
# mcp_glob
# ---------------------------------------------------------------------------


class TestMcpGlob:
    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _mcp_glob(INVALID_UUID, "**/*.py")

    def test_delegates_to_list_files(self) -> None:
        with patch(f"{_OPS_MODULE}.list_files", return_value="a.py\nb.py") as mock_list:
            result = _mcp_glob(VALID_UUID, "**/*.py", path="src")
        mock_list.assert_called_once_with(VALID_UUID, "**/*.py", "src")
        assert "a.py" in result

    def test_passes_none_path_by_default(self) -> None:
        with patch(f"{_OPS_MODULE}.list_files", return_value="") as mock_list:
            _mcp_glob(VALID_UUID, "*.txt")
        mock_list.assert_called_once_with(VALID_UUID, "*.txt", None)


# ---------------------------------------------------------------------------
# mcp_grep
# ---------------------------------------------------------------------------


class TestMcpGrep:
    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _mcp_grep(INVALID_UUID, "pattern")

    def test_delegates_to_search_files(self) -> None:
        with patch(f"{_OPS_MODULE}.search_files", return_value="file.py:1:match") as mock_search:
            result = _mcp_grep(VALID_UUID, "TODO", path="src", glob="*.py")
        mock_search.assert_called_once_with(VALID_UUID, "TODO", "src", "*.py")
        assert "match" in result


# ---------------------------------------------------------------------------
# mcp_bash
# ---------------------------------------------------------------------------


class TestMcpBash:
    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            await _mcp_bash(INVALID_UUID, "ls")

    async def test_uses_bch_manager_when_registered(self) -> None:
        mock_manager = AsyncMock()
        mock_manager.call_tool.return_value = "output from BCH"

        with patch(f"{_OPS_MODULE}.get_command_handler_manager", return_value=mock_manager):
            result = await _mcp_bash(VALID_UUID, "ls -la", timeout=60)

        mock_manager.call_tool.assert_awaited_once_with("Bash", {"command": "ls -la", "timeout": 60})
        assert result == "output from BCH"

    async def test_falls_back_to_run_command_when_no_manager(self) -> None:
        with (
            patch(f"{_OPS_MODULE}.get_command_handler_manager", return_value=None),
            patch(f"{_OPS_MODULE}._session_sandbox", {}),
            patch(f"{_OPS_MODULE}.run_command", return_value="fallback output") as mock_run,
        ):
            result = await _mcp_bash(VALID_UUID, "echo hello")

        mock_run.assert_called_once_with(VALID_UUID, "echo hello", 120, bwrap=False)
        assert result == "fallback output"

    async def test_uses_bwrap_when_sandbox_stack_has_true(self) -> None:
        session_sandbox = {VALID_UUID: [True]}
        with (
            patch(f"{_OPS_MODULE}.get_command_handler_manager", return_value=None),
            patch(f"{_OPS_MODULE}._session_sandbox", session_sandbox),
            patch(f"{_OPS_MODULE}.run_command", return_value="sandboxed") as mock_run,
        ):
            await _mcp_bash(VALID_UUID, "ls")

        mock_run.assert_called_once_with(VALID_UUID, "ls", 120, bwrap=True)

    async def test_bwrap_false_when_stack_top_is_false(self) -> None:
        session_sandbox = {VALID_UUID: [False]}
        with (
            patch(f"{_OPS_MODULE}.get_command_handler_manager", return_value=None),
            patch(f"{_OPS_MODULE}._session_sandbox", session_sandbox),
            patch(f"{_OPS_MODULE}.run_command", return_value="result") as mock_run,
        ):
            await _mcp_bash(VALID_UUID, "ls")

        mock_run.assert_called_once_with(VALID_UUID, "ls", 120, bwrap=False)


# ---------------------------------------------------------------------------
# mcp_webfetch
# ---------------------------------------------------------------------------


class TestMcpWebfetch:
    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            await _mcp_webfetch(INVALID_UUID, "https://example.com")

    async def test_delegates_to_fetch_url(self) -> None:
        with patch(f"{_OPS_MODULE}.fetch_url", new_callable=AsyncMock, return_value="page content") as mock_fetch:
            result = await _mcp_webfetch(VALID_UUID, "https://example.com", prompt="summarize")
        mock_fetch.assert_awaited_once_with("https://example.com", "summarize")
        assert result == "page content"

    async def test_passes_none_prompt_by_default(self) -> None:
        with patch(f"{_OPS_MODULE}.fetch_url", new_callable=AsyncMock, return_value="") as mock_fetch:
            await _mcp_webfetch(VALID_UUID, "https://example.com")
        mock_fetch.assert_awaited_once_with("https://example.com", None)


# ---------------------------------------------------------------------------
# mcp_websearch — rate limiting
# ---------------------------------------------------------------------------


class TestMcpWebsearch:
    def _clean_session(self) -> None:
        """Remove test session state from module-level dicts."""
        from ypl.agent_harness_service.tools.mcp_instance import (
            _session_websearch_count,
            _session_websearch_locks,
            _turn_websearch_count,
        )

        _turn_websearch_count.pop(VALID_UUID, None)
        _session_websearch_count.pop(VALID_UUID, None)
        _session_websearch_locks.pop(VALID_UUID, None)

    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            await _mcp_websearch(INVALID_UUID, "query")

    async def test_successful_search_increments_counters(self) -> None:
        self._clean_session()
        from ypl.agent_harness_service.tools.mcp_instance import (
            _session_websearch_count,
            _turn_websearch_count,
        )

        with patch(f"{_OPS_MODULE}.search_web", new_callable=AsyncMock, return_value="results"):
            result = await _mcp_websearch(VALID_UUID, "test query")

        assert result == "results"
        assert _turn_websearch_count.get(VALID_UUID) == 1
        assert _session_websearch_count.get(VALID_UUID) == 1
        self._clean_session()

    async def test_turn_limit_raises_value_error(self) -> None:
        self._clean_session()
        from ypl.agent_harness_service.common.constants import MAX_WEBSEARCH_CALLS_PER_TURN
        from ypl.agent_harness_service.tools.mcp_instance import _turn_websearch_count

        _turn_websearch_count[VALID_UUID] = MAX_WEBSEARCH_CALLS_PER_TURN

        with pytest.raises(ValueError, match="Web search limit reached"):
            await _mcp_websearch(VALID_UUID, "query")
        self._clean_session()

    async def test_session_limit_raises_value_error(self) -> None:
        self._clean_session()
        from ypl.agent_harness_service.common.constants import MAX_WEBSEARCH_CALLS_PER_SESSION
        from ypl.agent_harness_service.tools.mcp_instance import _session_websearch_count

        _session_websearch_count[VALID_UUID] = MAX_WEBSEARCH_CALLS_PER_SESSION

        with pytest.raises(ValueError, match="Web search limit reached"):
            await _mcp_websearch(VALID_UUID, "query")
        self._clean_session()

    async def test_counter_rolled_back_on_failure(self) -> None:
        self._clean_session()
        from ypl.agent_harness_service.tools.mcp_instance import (
            _session_websearch_count,
            _turn_websearch_count,
        )

        with (
            patch(f"{_OPS_MODULE}.search_web", new_callable=AsyncMock, side_effect=RuntimeError("search failed")),
            pytest.raises(RuntimeError),
        ):
            await _mcp_websearch(VALID_UUID, "query")

        # Counters should be rolled back to 0 after failure
        assert _turn_websearch_count.get(VALID_UUID, 0) == 0
        assert _session_websearch_count.get(VALID_UUID, 0) == 0
        self._clean_session()

    async def test_multiple_searches_accumulate_counters(self) -> None:
        self._clean_session()
        from ypl.agent_harness_service.tools.mcp_instance import (
            _session_websearch_count,
            _turn_websearch_count,
        )

        with patch(f"{_OPS_MODULE}.search_web", new_callable=AsyncMock, return_value="results"):
            await _mcp_websearch(VALID_UUID, "query 1")
            await _mcp_websearch(VALID_UUID, "query 2")

        assert _turn_websearch_count.get(VALID_UUID) == 2
        assert _session_websearch_count.get(VALID_UUID) == 2
        self._clean_session()
