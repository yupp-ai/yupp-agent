"""Tests for context management — pruning, compaction, and session history."""

import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from ypl.agent_harness_service.common.models import CompactionConfig
from ypl.agent_harness_service.executors.context import (
    cleanup_session_history,
    compact_messages,
    get_session_history_path,
    load_session_history,
    prune_tool_results,
    save_pre_compaction_snapshot,
    save_session_history,
)


class TestPruneToolResults:
    def test_no_pruning_under_budget(self) -> None:
        """No pruning when already under target."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result, pruned = prune_tool_results(
            messages, "system", context_limit=200_000, target_ratio=0.7, protect_steps=2
        )
        assert pruned == 0
        assert result[0]["content"] == "Hello"

    def test_prune_openai_tool_results(self) -> None:
        """Prune old OpenAI-format tool results while protecting recent ones."""
        big_result = "x" * 50_000
        messages: list[dict] = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "thinking..."},
            {"role": "tool", "tool_call_id": "old1", "content": big_result},
            {"role": "assistant", "content": "more thinking..."},
            {"role": "tool", "tool_call_id": "old2", "content": big_result},
            {"role": "assistant", "content": "recent thinking..."},
            {"role": "tool", "tool_call_id": "recent1", "content": big_result},
        ]
        # Set a tight limit so pruning is needed
        # With 3 big results of 50k chars each, that's ~45k tokens estimated
        _, pruned = prune_tool_results(messages, "system", context_limit=30_000, target_ratio=0.7, protect_steps=2)
        # Should prune old1 (oldest), protect old2 and recent1 (last 2)
        assert pruned >= 1
        assert "[pruned:" in messages[2]["content"]
        # Recent ones should be intact
        assert messages[6]["content"] == big_result

    def test_prune_anthropic_tool_results(self) -> None:
        """Prune Anthropic-format tool_result blocks."""
        big_result = "x" * 50_000
        messages: list[dict] = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": [{"type": "text", "text": "let me check"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "old1", "content": big_result},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "got it"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "recent1", "content": big_result},
                ],
            },
        ]
        _, pruned = prune_tool_results(messages, "system", context_limit=20_000, target_ratio=0.7, protect_steps=1)
        assert pruned >= 1
        # Old tool result should be pruned
        assert "[pruned:" in messages[2]["content"][0]["content"]
        # Recent should be intact
        assert messages[4]["content"][0]["content"] == big_result

    def test_protect_steps_respected(self) -> None:
        """All tool results protected when protect_steps >= total tool results."""
        big_result = "x" * 50_000
        messages: list[dict] = [
            {"role": "user", "content": "task"},
            {"role": "tool", "tool_call_id": "t1", "content": big_result},
            {"role": "tool", "tool_call_id": "t2", "content": big_result},
        ]
        _, pruned = prune_tool_results(messages, "system", context_limit=10_000, target_ratio=0.7, protect_steps=5)
        # All protected — nothing to prune
        assert pruned == 0

    def test_never_prunes_user_prompt(self) -> None:
        """The original user prompt is never touched."""
        original_prompt = "Do something important"
        messages: list[dict] = [
            {"role": "user", "content": original_prompt},
            {"role": "tool", "tool_call_id": "t1", "content": "x" * 50_000},
        ]
        prune_tool_results(messages, "system", context_limit=10_000, target_ratio=0.7, protect_steps=0)
        assert messages[0]["content"] == original_prompt


class TestCompactMessages:
    @pytest.mark.asyncio
    async def test_compact_messages_success(self) -> None:
        """Compaction produces a new single-message conversation."""
        # Use large messages so the compacted version is actually smaller
        messages: list[dict] = [
            {"role": "user", "content": "Build a web app with authentication"},
            {"role": "assistant", "content": "I'll start by setting up the project. " + "x" * 2000},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "y" * 2000}]},
            {"role": "assistant", "content": "Now adding auth module. " + "z" * 2000},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "w" * 2000}]},
        ]
        config = CompactionConfig(model="anthropic/claude-haiku-4-5")

        # Mock the Anthropic API call
        mock_response = AsyncMock()
        mock_response.content = [AsyncMock(text="## Goal\nBuild a web app\n## Progress\nBackend started")]
        mock_response.usage = AsyncMock(input_tokens=500, output_tokens=100)

        with patch("ypl.agent_harness_service.executors.context.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create.return_value = mock_response
            mock_client_cls.return_value = mock_client

            new_messages, stats = await compact_messages(messages, "Build a web app", "system", config)

        assert len(new_messages) == 1
        assert new_messages[0]["role"] == "user"
        assert "<conversation_summary>" in new_messages[0]["content"]
        assert "Build a web app" in new_messages[0]["content"]
        assert stats["tokens_before"] > 0
        assert stats["tokens_after"] > 0
        assert stats["tokens_after"] < stats["tokens_before"]

    @pytest.mark.asyncio
    async def test_compact_messages_llm_failure_returns_original(self) -> None:
        """Compaction failure returns original messages unchanged (no context loss)."""
        messages: list[dict] = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "working on it"},
        ]
        config = CompactionConfig(model="anthropic/claude-haiku-4-5")

        with patch("ypl.agent_harness_service.executors.context.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create.side_effect = Exception("API error")
            mock_client_cls.return_value = mock_client

            new_messages, stats = await compact_messages(messages, "task", "system", config)

        # Should return original messages unchanged, not a tiny replacement
        assert new_messages is messages
        assert len(new_messages) == 2
        assert new_messages[0]["content"] == "task"
        assert new_messages[1]["content"] == "working on it"
        assert stats["compaction_failed"] is True
        assert stats["compaction_input_tokens"] == 0
        assert stats["tokens_before"] == stats["tokens_after"]


class TestGetSessionHistoryPath:
    def test_path_structure(self) -> None:
        """Path includes session ID, history subdir, and expected filename."""
        path = get_session_history_path("abc-123")
        assert "abc-123" in path
        assert "/history/" in path
        assert path.endswith("raw_history.json")


class TestSaveAndLoadSessionHistory:
    def test_save_and_load_roundtrip(self, tmp_path: str) -> None:
        """Messages survive a save/load roundtrip."""
        path = os.path.join(str(tmp_path), "session", "raw_history.json")
        messages: list[dict] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/tmp/x"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file contents here"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Done!"}]},
        ]

        save_session_history(path, messages, "anthropic")
        loaded = load_session_history(path)

        assert loaded is not None
        loaded_messages, loaded_provider = loaded
        assert loaded_provider == "anthropic"
        assert len(loaded_messages) == len(messages)
        assert loaded_messages == messages

    def test_load_nonexistent_file(self) -> None:
        """Loading from a nonexistent path returns None."""
        result = load_session_history("/nonexistent/path/raw_history.json")
        assert result is None

    def test_load_invalid_json(self, tmp_path: str) -> None:
        """Loading invalid JSON returns None."""
        path = os.path.join(str(tmp_path), "bad.json")
        with open(path, "w") as f:
            f.write("not valid json{{{")

        result = load_session_history(path)
        assert result is None

    def test_load_empty_messages(self, tmp_path: str) -> None:
        """Loading a file with empty messages returns None."""
        path = os.path.join(str(tmp_path), "empty.json")
        with open(path, "w") as f:
            json.dump({"provider": "anthropic", "messages": [], "message_count": 0}, f)

        result = load_session_history(path)
        assert result is None

    def test_load_non_dict_json(self, tmp_path: str) -> None:
        """Loading a file with non-dict JSON (e.g., a list) returns None."""
        path = os.path.join(str(tmp_path), "list.json")
        with open(path, "w") as f:
            json.dump([1, 2, 3], f)

        result = load_session_history(path)
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path: str) -> None:
        """Save creates parent directories if they don't exist."""
        path = os.path.join(str(tmp_path), "deep", "nested", "dir", "raw_history.json")
        messages = [{"role": "user", "content": "test"}]

        save_session_history(path, messages, "openai")

        assert os.path.isfile(path)
        loaded = load_session_history(path)
        assert loaded is not None
        loaded_messages, provider = loaded
        assert provider == "openai"
        assert loaded_messages == messages

    def test_openai_format_messages(self, tmp_path: str) -> None:
        """OpenAI-format messages (with role=tool) survive roundtrip."""
        path = os.path.join(str(tmp_path), "openai_history.json")
        messages: list[dict] = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
            {"role": "assistant", "content": "The file says: file contents"},
        ]

        save_session_history(path, messages, "openai")
        loaded = load_session_history(path)

        assert loaded is not None
        loaded_messages, loaded_provider = loaded
        assert loaded_provider == "openai"
        assert loaded_messages == messages


class TestSavePreCompactionSnapshot:
    def test_snapshot_created_with_timestamp(self, tmp_path: str) -> None:
        """Snapshot file is created in the same directory with a timestamp name."""
        history_path = os.path.join(str(tmp_path), "session-1", "raw_history.json")
        messages: list[dict] = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 10_000}]},
        ]

        save_pre_compaction_snapshot(history_path, messages, "anthropic")

        session_dir = os.path.dirname(history_path)
        snapshot_files = [f for f in os.listdir(session_dir) if f.startswith("pre_compaction_")]
        assert len(snapshot_files) == 1
        assert snapshot_files[0].endswith(".json")

        # Verify contents
        with open(os.path.join(session_dir, snapshot_files[0])) as f:
            payload = json.load(f)
        assert payload["provider"] == "anthropic"
        assert payload["message_count"] == 3
        assert payload["messages"] == messages

    def test_multiple_snapshots_coexist(self, tmp_path: str) -> None:
        """Multiple compaction snapshots get unique filenames."""
        history_path = os.path.join(str(tmp_path), "session-1", "raw_history.json")
        messages = [{"role": "user", "content": "test"}]

        save_pre_compaction_snapshot(history_path, messages, "anthropic")
        time.sleep(0.001)  # Ensure distinct microsecond timestamps
        save_pre_compaction_snapshot(history_path, messages, "anthropic")

        session_dir = os.path.dirname(history_path)
        snapshot_files = [f for f in os.listdir(session_dir) if f.startswith("pre_compaction_")]
        assert len(snapshot_files) == 2


class TestCleanupSessionHistory:
    def _create_session(self, base_dir: str, session_id: str, age_days: float) -> str:
        """Helper: create a session with history/ subdir and a specific mtime."""
        session_dir = os.path.join(base_dir, session_id)
        history_dir = os.path.join(session_dir, "history")
        os.makedirs(history_dir)
        path = os.path.join(history_dir, "raw_history.json")
        with open(path, "w") as f:
            json.dump({"provider": "anthropic", "messages": [{"role": "user", "content": "test"}]}, f)
        # Backdate the mtime
        old_time = time.time() - (age_days * 86_400)
        os.utime(path, (old_time, old_time))
        return session_dir

    def test_deletes_old_sessions(self, tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sessions older than retention are deleted."""
        base = str(tmp_path)
        monkeypatch.setattr("ypl.agent_harness_service.executors.context.AHS_SESSIONS_DIR", base)

        old_dir = self._create_session(base, "old-session", age_days=45)
        recent_dir = self._create_session(base, "recent-session", age_days=5)

        stats = cleanup_session_history(retention_days=30)

        assert stats["deleted_count"] == 1
        assert stats["skipped_count"] == 1
        assert not os.path.exists(old_dir)
        assert os.path.exists(recent_dir)

    def test_dry_run_does_not_delete(self, tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dry run reports what would be deleted without deleting."""
        base = str(tmp_path)
        monkeypatch.setattr("ypl.agent_harness_service.executors.context.AHS_SESSIONS_DIR", base)

        old_dir = self._create_session(base, "old-session", age_days=45)

        stats = cleanup_session_history(retention_days=30, dry_run=True)

        assert stats["deleted_count"] == 1
        assert stats["dry_run"] is True
        assert os.path.exists(old_dir)  # Not actually deleted

    def test_empty_base_dir(self, tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """No errors when base directory is empty."""
        base = os.path.join(str(tmp_path), "empty")
        os.makedirs(base)
        monkeypatch.setattr("ypl.agent_harness_service.executors.context.AHS_SESSIONS_DIR", base)

        stats = cleanup_session_history(retention_days=30)

        assert stats["deleted_count"] == 0
        assert stats["skipped_count"] == 0

    def test_nonexistent_base_dir(self, tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """No errors when base directory does not exist."""
        monkeypatch.setattr("ypl.agent_harness_service.executors.context.AHS_SESSIONS_DIR", "/nonexistent/path")

        stats = cleanup_session_history(retention_days=30)

        assert stats["deleted_count"] == 0
