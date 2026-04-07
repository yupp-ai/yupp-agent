"""Unit tests for ypl/mcp_server/lifespan.py.

Covers:
  - mcp_startup(): calls setup_asyncio_logging, initializes batch system
  - mcp_shutdown(): stops batch system, closes Sentry session, flushes GCP logging
  - mcp_shutdown() handles timeout (batch flush > 5s)
  - mcp_shutdown() handles errors gracefully (Sentry close error, batch stop error)

All tests run without real infrastructure — all I/O is mocked.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# mcp_startup
# ---------------------------------------------------------------------------


class TestMcpStartup:
    async def test_calls_setup_asyncio_logging(self) -> None:
        from ypl.mcp_server.lifespan import mcp_startup

        with (
            patch("ypl.mcp_server.lifespan.setup_asyncio_logging") as mock_logging,
            patch("ypl.mcp_server.lifespan.initialize_batch_system", new=AsyncMock()),
        ):
            await mcp_startup()

        mock_logging.assert_called_once()

    async def test_calls_initialize_batch_system(self) -> None:
        from ypl.mcp_server.lifespan import mcp_startup

        mock_init = AsyncMock()
        with (
            patch("ypl.mcp_server.lifespan.setup_asyncio_logging"),
            patch("ypl.mcp_server.lifespan.initialize_batch_system", new=mock_init),
        ):
            await mcp_startup()

        mock_init.assert_awaited_once()

    async def test_startup_order(self) -> None:
        """Logging must be set up before batch system."""
        from ypl.mcp_server.lifespan import mcp_startup

        call_order: list[str] = []

        def record_logging() -> None:
            call_order.append("logging")

        async def record_batch() -> None:
            call_order.append("batch")

        with (
            patch("ypl.mcp_server.lifespan.setup_asyncio_logging", side_effect=record_logging),
            patch("ypl.mcp_server.lifespan.initialize_batch_system", new=AsyncMock(side_effect=record_batch)),
        ):
            await mcp_startup()

        assert call_order == ["logging", "batch"]


# ---------------------------------------------------------------------------
# mcp_shutdown
# ---------------------------------------------------------------------------


class TestMcpShutdown:
    async def test_calls_stop_batch_system(self) -> None:
        from ypl.mcp_server.lifespan import mcp_shutdown

        mock_stop = AsyncMock()
        mock_sentry_close = AsyncMock()

        with (
            patch("ypl.mcp_server.lifespan.stop_batch_system", new=mock_stop),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_cloud_logging"),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_logging_client"),
            patch.dict(
                "sys.modules",
                {
                    "ypl.mcp_server.tools.sentry": MagicMock(close_sentry_session=mock_sentry_close),
                },
            ),
        ):
            await mcp_shutdown()

        mock_stop.assert_awaited_once()

    async def test_flushes_gcp_logging(self) -> None:
        from ypl.mcp_server.lifespan import mcp_shutdown

        mock_flush1 = MagicMock()
        mock_flush2 = MagicMock()

        with (
            patch("ypl.mcp_server.lifespan.stop_batch_system", new=AsyncMock()),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_cloud_logging", new=mock_flush1),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_logging_client", new=mock_flush2),
            patch.dict(
                "sys.modules",
                {
                    "ypl.mcp_server.tools.sentry": MagicMock(close_sentry_session=AsyncMock()),
                },
            ),
        ):
            await mcp_shutdown()

        mock_flush1.assert_called_once()
        mock_flush2.assert_called_once()

    async def test_handles_batch_timeout_gracefully(self) -> None:
        """Batch stop timeout does NOT crash shutdown — GCP flush still runs."""
        import asyncio

        from ypl.mcp_server.lifespan import mcp_shutdown

        async def slow_stop() -> None:
            await asyncio.sleep(100)  # will time out

        mock_flush1 = MagicMock()
        mock_flush2 = MagicMock()

        with (
            patch("ypl.mcp_server.lifespan.stop_batch_system", new=slow_stop),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_cloud_logging", new=mock_flush1),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_logging_client", new=mock_flush2),
            patch.dict(
                "sys.modules",
                {
                    "ypl.mcp_server.tools.sentry": MagicMock(close_sentry_session=AsyncMock()),
                },
            ),
            patch("asyncio.timeout") as mock_timeout,
        ):
            # Should complete quickly (timeout is 5s in code, but we mock asyncio.timeout)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=TimeoutError)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_timeout.return_value = cm
            await mcp_shutdown()

        # GCP flush must still have been called despite timeout
        mock_flush1.assert_called_once()
        mock_flush2.assert_called_once()

    async def test_handles_sentry_close_error_gracefully(self) -> None:
        """Sentry close error does NOT crash shutdown — GCP flush still runs."""
        from ypl.mcp_server.lifespan import mcp_shutdown

        mock_flush1 = MagicMock()
        mock_flush2 = MagicMock()
        broken_sentry = MagicMock(close_sentry_session=AsyncMock(side_effect=RuntimeError("sentry down")))

        with (
            patch("ypl.mcp_server.lifespan.stop_batch_system", new=AsyncMock()),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_cloud_logging", new=mock_flush1),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_logging_client", new=mock_flush2),
            patch.dict("sys.modules", {"ypl.mcp_server.tools.sentry": broken_sentry}),
        ):
            await mcp_shutdown()

        mock_flush1.assert_called_once()
        mock_flush2.assert_called_once()

    async def test_handles_batch_stop_exception_gracefully(self) -> None:
        """Exception during batch stop is caught — shutdown continues."""
        from ypl.mcp_server.lifespan import mcp_shutdown

        async def failing_stop() -> None:
            raise RuntimeError("batch system crashed")

        mock_flush1 = MagicMock()
        mock_flush2 = MagicMock()

        with (
            patch("ypl.mcp_server.lifespan.stop_batch_system", new=failing_stop),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_cloud_logging", new=mock_flush1),
            patch("ypl.mcp_server.lifespan.flush_and_close_google_logging_client", new=mock_flush2),
            patch.dict(
                "sys.modules",
                {
                    "ypl.mcp_server.tools.sentry": MagicMock(close_sentry_session=AsyncMock()),
                },
            ),
        ):
            await mcp_shutdown()

        mock_flush1.assert_called_once()
        mock_flush2.assert_called_once()


# ---------------------------------------------------------------------------
# Module import smoke tests
# ---------------------------------------------------------------------------


class TestLifespanModuleImports:
    def test_module_importable(self) -> None:
        import importlib

        mod = importlib.import_module("ypl.mcp_server.lifespan")
        assert mod is not None

    def test_mcp_startup_is_callable(self) -> None:
        from ypl.mcp_server.lifespan import mcp_startup

        assert callable(mcp_startup)

    def test_mcp_shutdown_is_callable(self) -> None:
        from ypl.mcp_server.lifespan import mcp_shutdown

        assert callable(mcp_shutdown)
