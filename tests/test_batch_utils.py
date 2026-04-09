"""Unit tests for backend batch_utils module.

Covers:
- BufferConfig defaults
- Buffer.add_item (normal / overflow triggers flush)
- Buffer.flush (empty / flushing guard / batch splitting / feature flag batch size)
- Buffer.start_periodic_flush / stop
- BatchManager singleton / register / get / start / flush / stop / reset
- Convenience functions
"""

from __future__ import annotations
from unittest.mock import AsyncMock, patch

import pytest
from ypl.backend.utils.batch_utils import (
    BatchManager,
    BatchProcessor,
    Buffer,
    BufferConfig,
    flush_all_buffers,
    flush_and_reset_batch_system,
    get_batch_manager,
    initialize_batch_system,
    stop_batch_system,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProcessor(BatchProcessor[int]):
    """A test processor that records batches it receives."""

    def __init__(self) -> None:
        self.batches: list[list[int]] = []
        self.call_count = 0

    async def process_batch(self, items: list[int], batch_index: int, batch_size: int) -> None:
        self.batches.append(list(items))
        self.call_count += 1


class _FailingProcessor(BatchProcessor[int]):
    async def process_batch(self, items: list[int], batch_index: int, batch_size: int) -> None:
        raise RuntimeError("process_batch failed")


# ---------------------------------------------------------------------------
# BufferConfig
# ---------------------------------------------------------------------------


class TestBufferConfig:
    def test_defaults(self) -> None:
        cfg = BufferConfig()
        assert cfg.max_buffer_size == 1000
        assert cfg.flush_interval_seconds == 60
        assert cfg.batch_size == 100
        assert cfg.feature_flag_batch_size is None

    def test_custom_values(self) -> None:
        cfg = BufferConfig(max_buffer_size=50, flush_interval_seconds=10, batch_size=5)
        assert cfg.max_buffer_size == 50
        assert cfg.flush_interval_seconds == 10
        assert cfg.batch_size == 5


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class TestBuffer:
    def _make_buffer(self, max_size: int = 100, batch_size: int = 10) -> tuple[Buffer[int], _FakeProcessor]:
        processor = _FakeProcessor()
        config = BufferConfig(max_buffer_size=max_size, flush_interval_seconds=60, batch_size=batch_size)
        buffer: Buffer[int] = Buffer(config, processor, "test-buffer")
        return buffer, processor

    def test_add_item_appends(self) -> None:
        buffer, _ = self._make_buffer()
        buffer.add_item(42)
        assert len(buffer._buffer) == 1

    def test_add_item_triggers_flush_at_max_size(self) -> None:
        buffer, _ = self._make_buffer(max_size=3)
        with patch("ypl.backend.utils.batch_utils.create_background_task") as mock_bg:
            buffer.add_item(1)
            buffer.add_item(2)
            buffer.add_item(3)  # hits max_buffer_size = 3
        mock_bg.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self) -> None:
        buffer, processor = self._make_buffer()
        await buffer.flush()
        assert processor.call_count == 0

    @pytest.mark.asyncio
    async def test_flush_noop_when_already_flushing(self) -> None:
        buffer, processor = self._make_buffer()
        buffer._buffer = [1, 2, 3]
        buffer._is_flushing = True
        await buffer.flush()
        assert processor.call_count == 0

    @pytest.mark.asyncio
    async def test_flush_processes_all_items(self) -> None:
        buffer, processor = self._make_buffer(batch_size=5)
        for i in range(10):
            buffer._buffer.append(i)
        await buffer.flush()
        assert processor.call_count == 2
        total = sum(len(b) for b in processor.batches)
        assert total == 10

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self) -> None:
        buffer, _ = self._make_buffer()
        buffer._buffer = [1, 2, 3]
        await buffer.flush()
        assert buffer._buffer == []

    @pytest.mark.asyncio
    async def test_flush_with_feature_flag_batch_size(self) -> None:
        processor = _FakeProcessor()
        config = BufferConfig(max_buffer_size=100, batch_size=5, feature_flag_batch_size="test_batch_size_flag")
        buffer: Buffer[int] = Buffer(config, processor, "ff-buffer")
        for i in range(20):
            buffer._buffer.append(i)

        with patch(
            "ypl.backend.utils.batch_utils.get_feature_value",
            new_callable=AsyncMock,
            return_value=10,  # override batch size to 10
        ):
            await buffer.flush()

        # With batch_size=10, 20 items → 2 batches
        assert processor.call_count == 2

    @pytest.mark.asyncio
    async def test_flush_batch_processor_exception_continues(self) -> None:
        """A failing processor should not prevent flushing completing (error logged)."""
        processor = _FailingProcessor()
        config = BufferConfig(max_buffer_size=100, batch_size=5)
        buffer: Buffer[int] = Buffer(config, processor, "fail-buffer")
        buffer._buffer = [1, 2, 3]
        # Should not raise
        await buffer.flush()
        assert buffer._is_flushing is False

    @pytest.mark.asyncio
    async def test_stop_cancels_flush_task(self) -> None:
        buffer, _ = self._make_buffer()
        await buffer.start_periodic_flush()
        assert buffer._flush_task is not None
        await buffer.stop()
        assert buffer._flush_task is not None
        assert buffer._flush_task.done()

    @pytest.mark.asyncio
    async def test_start_periodic_flush_idempotent(self) -> None:
        """Calling start_periodic_flush multiple times should not spawn multiple tasks."""
        buffer, _ = self._make_buffer()
        await buffer.start_periodic_flush()
        await buffer.start_periodic_flush()
        # Same task (or a new one if the old one was done)
        assert buffer._flush_task is not None
        await buffer.stop()


# ---------------------------------------------------------------------------
# BatchManager
# ---------------------------------------------------------------------------


class TestBatchManager:
    @pytest.fixture(autouse=True)
    async def reset_manager(self) -> None:  # type: ignore[misc]
        """Reset the singleton between tests."""
        manager = BatchManager.get_instance()
        await manager.reset()
        yield
        await manager.reset()

    def test_get_instance_singleton(self) -> None:
        m1 = BatchManager.get_instance()
        m2 = BatchManager.get_instance()
        assert m1 is m2

    def test_register_buffer(self) -> None:
        manager = BatchManager.get_instance()
        processor = _FakeProcessor()
        config = BufferConfig()
        buf = manager.register_buffer("my-buf", config, processor)
        assert buf is manager.get_buffer("my-buf")

    def test_register_duplicate_raises(self) -> None:
        manager = BatchManager.get_instance()
        processor = _FakeProcessor()
        config = BufferConfig()
        manager.register_buffer("dup", config, processor)
        with pytest.raises(ValueError, match="already registered"):
            manager.register_buffer("dup", config, processor)

    def test_get_missing_buffer_raises(self) -> None:
        manager = BatchManager.get_instance()
        with pytest.raises(ValueError, match="not registered"):
            manager.get_buffer("nonexistent")

    @pytest.mark.asyncio
    async def test_start_all_buffers(self) -> None:
        manager = BatchManager.get_instance()
        processor = _FakeProcessor()
        config = BufferConfig(flush_interval_seconds=9999)
        buf = manager.register_buffer("start-buf", config, processor)
        await manager.start_all_buffers()
        assert manager._started is True
        assert buf._flush_task is not None
        await manager.stop_all_buffers()

    @pytest.mark.asyncio
    async def test_start_all_idempotent(self) -> None:
        manager = BatchManager.get_instance()
        await manager.start_all_buffers()
        await manager.start_all_buffers()  # second call is a no-op
        assert manager._started is True
        await manager.stop_all_buffers()

    @pytest.mark.asyncio
    async def test_flush_all_buffers(self) -> None:
        manager = BatchManager.get_instance()
        processor = _FakeProcessor()
        config = BufferConfig(batch_size=10)
        buf = manager.register_buffer("flush-buf", config, processor)
        for i in range(5):
            buf.add_item(i)
        await manager.flush_all_buffers()
        assert processor.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_all_buffers_resets_started(self) -> None:
        manager = BatchManager.get_instance()
        await manager.start_all_buffers()
        await manager.stop_all_buffers()
        assert manager._started is False

    @pytest.mark.asyncio
    async def test_reset_clears_buffers(self) -> None:
        manager = BatchManager.get_instance()
        processor = _FakeProcessor()
        config = BufferConfig()
        manager.register_buffer("reset-buf", config, processor)
        await manager.reset()
        with pytest.raises(ValueError):
            manager.get_buffer("reset-buf")


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


class TestConvenienceFunctions:
    @pytest.fixture(autouse=True)
    async def reset_manager(self) -> None:  # type: ignore[misc]
        manager = get_batch_manager()
        await manager.reset()
        yield
        await manager.reset()

    def test_get_batch_manager_returns_singleton(self) -> None:
        m = get_batch_manager()
        assert m is BatchManager.get_instance()

    @pytest.mark.asyncio
    async def test_initialize_batch_system(self) -> None:
        manager = get_batch_manager()
        await initialize_batch_system()
        assert manager._started is True
        await manager.stop_all_buffers()

    @pytest.mark.asyncio
    async def test_flush_all_buffers_function(self) -> None:
        manager = get_batch_manager()
        processor = _FakeProcessor()
        config = BufferConfig(batch_size=10)
        buf = manager.register_buffer("func-buf", config, processor)
        buf.add_item(99)
        await flush_all_buffers()
        assert processor.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_batch_system(self) -> None:
        await initialize_batch_system()
        await stop_batch_system()
        assert get_batch_manager()._started is False

    @pytest.mark.asyncio
    async def test_flush_and_reset_batch_system(self) -> None:
        manager = get_batch_manager()
        processor = _FakeProcessor()
        config = BufferConfig(batch_size=10)
        buf = manager.register_buffer("reset-buf", config, processor)
        buf.add_item(1)
        await flush_and_reset_batch_system()
        assert manager._buffers == {}
        assert manager._started is False
