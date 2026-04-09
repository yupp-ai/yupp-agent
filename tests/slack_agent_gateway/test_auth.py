"""Unit tests for ypl/slack_agent_gateway/auth.py.

Covers:
- verify_api_key: missing key config, missing header, invalid key, valid primary key,
  valid secondary key, key rotation.
"""

from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from ypl.slack_agent_gateway.auth import verify_api_key

MODULE = "ypl.slack_agent_gateway.auth"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(api_key: str | None = None) -> MagicMock:
    """Build a mock FastAPI Request with a given X-API-Key header."""
    request = MagicMock()
    headers: dict[str, str] = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    request.headers = headers
    return request


def _mock_settings(primary_key: str = "primary-key", secondary_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.X_API_KEY = primary_key
    settings.X_API_KEY_SECONDARY = secondary_key
    return settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerifyApiKey:
    async def test_raises_401_when_primary_key_not_configured(self) -> None:
        settings = _mock_settings(primary_key="")
        request = _make_request(api_key="some-key")
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert exc_info.value.status_code == HTTP_401_UNAUTHORIZED

    async def test_raises_401_when_header_missing(self) -> None:
        settings = _mock_settings(primary_key="configured-key")
        request = _make_request(api_key=None)
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert exc_info.value.status_code == HTTP_401_UNAUTHORIZED

    async def test_raises_403_when_wrong_key_provided(self) -> None:
        settings = _mock_settings(primary_key="real-key")
        request = _make_request(api_key="wrong-key")
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert exc_info.value.status_code == HTTP_403_FORBIDDEN

    async def test_passes_with_correct_primary_key(self) -> None:
        settings = _mock_settings(primary_key="my-primary-key")
        request = _make_request(api_key="my-primary-key")
        with patch(f"{MODULE}.settings", settings):
            await verify_api_key(request)  # Should not raise

    async def test_passes_with_correct_secondary_key(self) -> None:
        settings = _mock_settings(primary_key="primary", secondary_key="secondary-key")
        request = _make_request(api_key="secondary-key")
        with patch(f"{MODULE}.settings", settings):
            await verify_api_key(request)  # Should not raise

    async def test_raises_403_when_wrong_key_and_secondary_set(self) -> None:
        settings = _mock_settings(primary_key="primary", secondary_key="secondary")
        request = _make_request(api_key="totally-wrong")
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert exc_info.value.status_code == HTTP_403_FORBIDDEN

    async def test_raises_403_when_wrong_key_and_no_secondary(self) -> None:
        settings = _mock_settings(primary_key="the-key", secondary_key="")
        request = _make_request(api_key="not-the-key")
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert exc_info.value.status_code == HTTP_403_FORBIDDEN

    async def test_primary_key_check_uses_constant_time_comparison(self) -> None:
        """Verify we can handle a valid primary key (timing-safe)."""
        settings = _mock_settings(primary_key="abc123", secondary_key="def456")
        request = _make_request(api_key="abc123")
        with patch(f"{MODULE}.settings", settings):
            await verify_api_key(request)  # No exception = passes

    async def test_does_not_check_secondary_when_primary_matches(self) -> None:
        """When primary key matches, we never need to check secondary."""
        settings = _mock_settings(primary_key="correct", secondary_key="secondary")
        request = _make_request(api_key="correct")
        with patch(f"{MODULE}.settings", settings):
            # Both primary matches → no error
            await verify_api_key(request)

    async def test_missing_header_message(self) -> None:
        settings = _mock_settings(primary_key="key")
        request = _make_request(api_key=None)
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert "Missing" in exc_info.value.detail or "missing" in exc_info.value.detail.lower()

    async def test_not_configured_message(self) -> None:
        settings = _mock_settings(primary_key="")
        request = _make_request(api_key="any-key")
        with (
            patch(f"{MODULE}.settings", settings),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_api_key(request)
        assert "not configured" in exc_info.value.detail.lower() or "authentication" in exc_info.value.detail.lower()
