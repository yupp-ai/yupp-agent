"""Unit tests for the OAuth callback + membership check.

The viewer defers allowlisting to AHS ``resolve_user``; these tests
stub that call and verify the callback sets the session only when AHS
returns a ``user_id``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from artifact_viewer.app import build_app
from starlette.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), follow_redirects=False)


def _stub_oauth(email: str = "alice@agcouch.com", name: str = "Alice") -> Any:
    """Build a minimal mock of the authlib oauth.google client.

    Covers the two calls the callback makes:
      - ``authorize_access_token(request)`` → returns a dict with userinfo
      - ``userinfo(token=...)``             → fallback fetch (not used here)
    """
    oauth = AsyncMock()
    oauth.google = AsyncMock()
    oauth.google.authorize_access_token = AsyncMock(
        return_value={"userinfo": {"email": email, "name": name, "picture": ""}}
    )
    oauth.google.userinfo = AsyncMock(return_value={"email": email, "name": name, "picture": ""})
    return oauth


class TestCallbackMembershipCheck:
    def test_allows_when_ahs_resolves_email(self, client: TestClient) -> None:
        """AHS returns a user_id → set session and redirect to next_url."""
        oauth = _stub_oauth(email="alice@agcouch.com")
        with (
            patch("artifact_viewer.auth.get_oauth", return_value=oauth),
            patch(
                "artifact_viewer.ahs_client.resolve_user",
                new=AsyncMock(return_value="user-uuid-alice"),
            ),
        ):
            resp = client.get("/auth/callback")
        assert resp.status_code == 307  # redirect to next_url (default /)
        assert resp.headers["location"] == "/"

    def test_denies_when_ahs_returns_not_found(self, client: TestClient) -> None:
        """AHS returns None (404) → redirect to /auth/error?reason=not_a_user."""
        oauth = _stub_oauth(email="external@gmail.com")
        with (
            patch("artifact_viewer.auth.get_oauth", return_value=oauth),
            patch(
                "artifact_viewer.ahs_client.resolve_user",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = client.get("/auth/callback")
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/auth/error?reason=not_a_user")
        assert "external%40gmail.com" in resp.headers["location"]

    def test_ahs_unavailable_surfaces_error(self, client: TestClient) -> None:
        """AHS raises (e.g. 500 / network) → redirect to error with ahs_unavailable."""
        from artifact_viewer.ahs_client import AHSError

        oauth = _stub_oauth(email="alice@agcouch.com")
        with (
            patch("artifact_viewer.auth.get_oauth", return_value=oauth),
            patch(
                "artifact_viewer.ahs_client.resolve_user",
                new=AsyncMock(side_effect=AHSError(500, "boom")),
            ),
        ):
            resp = client.get("/auth/callback")
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/auth/error?reason=ahs_unavailable")

    def test_missing_email_in_userinfo_is_rejected(self, client: TestClient) -> None:
        """Google didn't give us an email → deny without calling AHS."""
        oauth = AsyncMock()
        oauth.google = AsyncMock()
        oauth.google.authorize_access_token = AsyncMock(return_value={"userinfo": {"name": "No Email"}})
        oauth.google.userinfo = AsyncMock(return_value={"name": "No Email"})
        resolve_mock = AsyncMock()
        with (
            patch("artifact_viewer.auth.get_oauth", return_value=oauth),
            patch("artifact_viewer.ahs_client.resolve_user", new=resolve_mock),
        ):
            resp = client.get("/auth/callback")
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/auth/error?reason=no_email")
        resolve_mock.assert_not_called()


class TestErrorPage:
    def test_not_a_user_reason_renders_expected_copy(self, client: TestClient) -> None:
        resp = client.get("/auth/error?reason=not_a_user&email=someone@example.com")
        assert resp.status_code == 403
        body = resp.text
        assert "someone@example.com" in body
        assert "users" in body.lower()  # refers to the users table

    def test_ahs_unavailable_reason(self, client: TestClient) -> None:
        resp = client.get("/auth/error?reason=ahs_unavailable")
        assert resp.status_code == 403
        assert "unreachable" in resp.text.lower() or "upstream" in resp.text.lower()
