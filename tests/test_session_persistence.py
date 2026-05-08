"""Unit tests for the session-persistence backend selector.

The interesting behaviour lives in ``_resolve_backend()`` which:

1. Honours an explicit ``SESSION_PERSISTENCE_BACKEND`` value (single knob,
   overrides the ENVIRONMENT gate).
2. Falls back to the legacy ENVIRONMENT-based default when the flag is
   unset (off in ``local|test|selfhosted``, gcs everywhere else).

We also verify ``sync_session()`` dispatches to the right underlying
sync helper for each non-off backend. The actual upload code paths
(``sync_dir_to_gcs`` / ``sync_dir_to_s3``) are exercised in their own
dedicated tests; here we mock them to keep the selector tests fast and
hermetic.

Note: asyncio_mode = "auto" — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_settings() -> Any:
    """Reset SESSION_PERSISTENCE_BACKEND / ENVIRONMENT for each test.

    We patch the ``settings`` object on the session_persistence module so
    each test gets a clean slate without polluting the real environment.
    """
    with patch("ypl.agent_harness_service.core.session_persistence.settings") as mock_settings:
        mock_settings.SESSION_PERSISTENCE_BACKEND = ""
        mock_settings.ENVIRONMENT = "local"
        mock_settings.S3_REGION = ""
        mock_settings.S3_ENDPOINT_URL = ""
        yield mock_settings


# ---------------------------------------------------------------------------
# _resolve_backend
# ---------------------------------------------------------------------------


class TestResolveBackend:
    def test_unset_in_local_returns_off(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = ""
        _patch_settings.ENVIRONMENT = "local"
        assert _resolve_backend() == "off"

    def test_unset_in_test_returns_off(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = ""
        _patch_settings.ENVIRONMENT = "test"
        assert _resolve_backend() == "off"

    def test_unset_in_selfhosted_returns_off(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = ""
        _patch_settings.ENVIRONMENT = "selfhosted"
        assert _resolve_backend() == "off"

    def test_unset_in_staging_returns_gcs(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = ""
        _patch_settings.ENVIRONMENT = "staging"
        assert _resolve_backend() == "gcs"

    def test_unset_in_production_returns_gcs(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = ""
        _patch_settings.ENVIRONMENT = "production"
        assert _resolve_backend() == "gcs"

    def test_explicit_off_overrides_production(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "off"
        _patch_settings.ENVIRONMENT = "production"
        assert _resolve_backend() == "off"

    def test_explicit_s3_overrides_production_default(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "s3"
        _patch_settings.ENVIRONMENT = "production"
        assert _resolve_backend() == "s3"

    def test_explicit_gcs_in_local_enables_persistence(self, _patch_settings: Any) -> None:
        """A staging-like deployment running locally can opt in via the flag."""
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "gcs"
        _patch_settings.ENVIRONMENT = "local"
        assert _resolve_backend() == "gcs"

    def test_explicit_s3_in_local_enables_persistence(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "s3"
        _patch_settings.ENVIRONMENT = "local"
        assert _resolve_backend() == "s3"

    def test_normalises_whitespace_and_case(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "  S3  "
        _patch_settings.ENVIRONMENT = "production"
        assert _resolve_backend() == "s3"

    def test_unknown_value_falls_back_to_environment_default(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core.session_persistence import _resolve_backend

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "azure"
        _patch_settings.ENVIRONMENT = "production"
        # Unknown backend is treated as unset, so we fall through to the
        # ENVIRONMENT-based default (gcs in production).
        assert _resolve_backend() == "gcs"


# ---------------------------------------------------------------------------
# sync_session dispatch
# ---------------------------------------------------------------------------


class TestSyncSessionDispatch:
    async def test_off_returns_zero_without_calling_helpers(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core import session_persistence as sp

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "off"
        _patch_settings.ENVIRONMENT = "production"

        gcs_mock = AsyncMock(return_value=99)
        s3_mock = AsyncMock(return_value=99)
        with (
            patch.object(sp, "sync_dir_to_gcs", gcs_mock),
            patch.object(sp, "sync_dir_to_s3", s3_mock),
        ):
            result = await sp.sync_session("session-uuid")
        assert result == 0
        gcs_mock.assert_not_awaited()
        s3_mock.assert_not_awaited()

    async def test_gcs_backend_calls_gcs_sync(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core import session_persistence as sp

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "gcs"
        _patch_settings.ENVIRONMENT = "production"

        gcs_mock = AsyncMock(return_value=3)
        s3_mock = AsyncMock(return_value=0)
        with (
            patch.object(sp, "sync_dir_to_gcs", gcs_mock),
            patch.object(sp, "sync_dir_to_s3", s3_mock),
        ):
            result = await sp.sync_session("session-uuid")
        assert result == 3
        gcs_mock.assert_awaited_once()
        s3_mock.assert_not_awaited()

        # Verify GCS-specific kwargs are forwarded.
        kwargs = gcs_mock.call_args.kwargs
        assert kwargs["bucket"]  # default AHS_GCS_SESSION_BUCKET
        assert kwargs["lock_key"] == "session:session-uuid"
        assert kwargs["subdirs"] == ("attachments", "history")
        assert kwargs["gcs_prefix"].endswith("/session-uuid")

    async def test_s3_backend_calls_s3_sync(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core import session_persistence as sp

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "s3"
        _patch_settings.ENVIRONMENT = "production"
        _patch_settings.S3_REGION = "us-east-1"
        _patch_settings.S3_ENDPOINT_URL = ""

        gcs_mock = AsyncMock(return_value=0)
        s3_mock = AsyncMock(return_value=5)
        with (
            patch.object(sp, "sync_dir_to_gcs", gcs_mock),
            patch.object(sp, "sync_dir_to_s3", s3_mock),
        ):
            result = await sp.sync_session("abc-123")
        assert result == 5
        gcs_mock.assert_not_awaited()
        s3_mock.assert_awaited_once()

        kwargs = s3_mock.call_args.kwargs
        assert kwargs["bucket"]  # default AHS_S3_SESSION_BUCKET
        assert kwargs["lock_key"] == "session:abc-123"
        assert kwargs["subdirs"] == ("attachments", "history")
        assert kwargs["s3_prefix"].endswith("/abc-123")
        assert kwargs["region"] == "us-east-1"
        # Empty endpoint string normalised to None for the S3 client.
        assert kwargs["endpoint_url"] is None

    async def test_s3_backend_forwards_minio_endpoint(self, _patch_settings: Any) -> None:
        from ypl.agent_harness_service.core import session_persistence as sp

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "s3"
        _patch_settings.ENVIRONMENT = "production"
        _patch_settings.S3_REGION = ""
        _patch_settings.S3_ENDPOINT_URL = "http://minio:9000"

        s3_mock = AsyncMock(return_value=0)
        with (
            patch.object(sp, "sync_dir_to_gcs", AsyncMock()),
            patch.object(sp, "sync_dir_to_s3", s3_mock),
        ):
            await sp.sync_session("xyz")
        kwargs = s3_mock.call_args.kwargs
        assert kwargs["region"] is None
        assert kwargs["endpoint_url"] == "http://minio:9000"

    async def test_legacy_alias_dispatches_through_sync_session(self, _patch_settings: Any) -> None:
        """``sync_session_to_gcs`` is kept as a backwards-compat alias."""
        from ypl.agent_harness_service.core import session_persistence as sp

        _patch_settings.SESSION_PERSISTENCE_BACKEND = "off"
        _patch_settings.ENVIRONMENT = "production"
        result = await sp.sync_session_to_gcs("anything")
        assert result == 0
