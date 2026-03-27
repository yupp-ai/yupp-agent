import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Optional, TypeVar

from ypl.backend.feature_flags import get_feature_value
from ypl.backend.utils.async_utils import create_background_task
from ypl.structured_logger import get_logger

logger = get_logger()

T = TypeVar("T")


@dataclass
class BufferConfig:
    """Configuration for a buffer."""

    max_buffer_size: int = 1000
    flush_interval_seconds: int = 60
    batch_size: int = 100
    feature_flag_batch_size: str | None = None  # Feature flag name for dynamic batch size


class BatchProcessor[T](ABC):
    """Abstract base class for batch processors."""

    @abstractmethod
    async def process_batch(self, items: list[T], batch_index: int, batch_size: int) -> None:
        """Process a batch of items."""


class Buffer[T]:
    """Generic buffer for batching items."""

    def __init__(self, config: BufferConfig, processor: BatchProcessor[T], buffer_name: str):
        self.config = config
        self.processor = processor
        self.buffer_name = buffer_name
        self._buffer: list[T] = []
        self._flush_task: asyncio.Task | None = None
        self._is_flushing = False

    def add_item(self, item: T) -> None:
        """Add an item to the buffer."""
        self._buffer.append(item)
        if len(self._buffer) >= self.config.max_buffer_size:
            create_background_task(self.flush())

    async def start_periodic_flush(self) -> None:
        """Start periodic flush task."""
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._periodic_flush())

    async def _periodic_flush(self) -> None:
        """Periodically flush the buffer."""
        while True:
            await asyncio.sleep(self.config.flush_interval_seconds)
            try:
                await self.flush()
            except Exception:
                logger.exception(
                    f"Error flushing {self.buffer_name} buffer",
                )

    async def flush(self) -> None:
        """Flush the buffer to the database."""
        if self._is_flushing or not self._buffer:
            return

        self._is_flushing = True
        try:
            logger.info("Flushing buffer", buffer_name=self.buffer_name, buffer_size=len(self._buffer))

            # Get batch size from feature flag if configured
            batch_size = self.config.batch_size
            if self.config.feature_flag_batch_size:
                batch_size = await get_feature_value(self.config.feature_flag_batch_size) or batch_size

            # Process items in batches
            buffer_to_write = self._buffer
            self._buffer = []  # Clear buffer before processing

            for i in range(0, len(buffer_to_write), batch_size):
                batch = buffer_to_write[i : i + batch_size]
                try:
                    await self.processor.process_batch(batch, i, batch_size)
                except Exception:
                    logger.exception(
                        f"Error processing batch of {self.buffer_name} buffer",
                        batch_index=i,
                    )
        finally:
            self._is_flushing = False

    async def stop(self) -> None:
        """Stop the periodic flush task."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task


class BatchManager:
    """Manager for all buffers in the application."""

    _instance: ClassVar[Optional["BatchManager"]] = None
    _buffers: dict[str, Buffer[Any]] = {}
    _started = False

    @classmethod
    def get_instance(cls) -> "BatchManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_buffer(self, name: str, config: BufferConfig, processor: BatchProcessor[Any]) -> Buffer[Any]:
        """Register a new buffer."""
        if name in self._buffers:
            raise ValueError(f"Buffer '{name}' is already registered")

        buffer = Buffer(config, processor, name)
        self._buffers[name] = buffer
        return buffer

    def get_buffer(self, name: str) -> Buffer[Any]:
        """Get a registered buffer."""
        if name not in self._buffers:
            raise ValueError(f"Buffer '{name}' is not registered")
        return self._buffers[name]

    async def start_all_buffers(self) -> None:
        """Start all registered buffers."""
        if self._started:
            return

        for buffer in self._buffers.values():
            await buffer.start_periodic_flush()

        self._started = True
        logger.info("Started buffers", buffer_count=len(self._buffers))

    async def flush_all_buffers(self) -> None:
        """Flush all registered buffers."""
        for buffer in self._buffers.values():
            await buffer.flush()

    async def stop_all_buffers(self) -> None:
        """Stop all registered buffers."""
        for buffer in self._buffers.values():
            await buffer.stop()

        self._started = False
        await self.flush_all_buffers()
        logger.info("Stopped all buffers, and flushed the remaining items in the buffers")

    async def reset(self) -> None:
        """Reset the batch manager."""
        self._buffers.clear()
        self._started = False


# Convenience functions for global access
def get_batch_manager() -> BatchManager:
    """Get the global batch manager instance."""
    return BatchManager.get_instance()


async def initialize_batch_system() -> None:
    """Initialize the batch system. This should be called when the application starts."""
    await get_batch_manager().start_all_buffers()


async def flush_all_buffers() -> None:
    """Flush all buffers. This should be called during application shutdown."""
    await get_batch_manager().flush_all_buffers()


async def stop_batch_system() -> None:
    """Stop the batch system. This should be called during application shutdown."""
    await get_batch_manager().stop_all_buffers()


async def flush_and_reset_batch_system() -> None:
    """
    Flush and reset the batch system. This should be called after stop_batch_system has been calleds.
    Separating the two calls, to handle the cases where:
    - In cron jobs, we wait for all background tasks to complete before returning.
        - Each buffer has a background task that flushes the buffer to the database periodically.
        - Thus the batch system should be stopped first, so the waiting will not timeout.
    - Other background tasks could push new items into the buffer after stop_batch_system has been called.
        - Thus the buffers should be flushed first, so the new items are not lost.
        - Then reset the batch system, so the batch system goes back to the initial state. This is for
          the cases, where we run multiple cli commands in sequence, chained by python code.
    """
    await get_batch_manager().flush_all_buffers()
    await get_batch_manager().reset()
