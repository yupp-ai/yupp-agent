"""Slack's hosted MCP in the external-MCP catalog + the OAuth quirks it needs.

Slack differs from RFC 6749 / Google in three ways the generic client must
absorb: comma-separated scopes, HTTP 200 ``{"ok": false}`` error bodies, and
comma-separated granted scopes in the token response.
"""

from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from ypl.db.external_mcp import McpAuthType, McpTransport
from ypl.external_mcp import catalog, oauth_client


def test_slack_catalog_entry() -> None:
    entry = catalog.get("slack")
    assert entry is not None
    assert entry.url == "https://mcp.slack.com/mcp"
    assert entry.transport == McpTransport.STREAMABLE_HTTP
    assert entry.auth_type == McpAuthType.OAUTH_OBO
    assert entry.authorize_url == "https://slack.com/oauth/v2_user/authorize"
    assert entry.token_url == "https://slack.com/api/oauth.v2.user.access"
    assert entry.scope_separator == ","
    assert {"search:read.public", "channels:history", "chat:write"} <= set(entry.scopes)
    # Must not share the Google client creds used for one-click install.
    assert entry.client_id_env == "SLACK_MCP_OAUTH_CLIENT_ID"
    assert entry.client_secret_env == "SLACK_MCP_OAUTH_CLIENT_SECRET"


def test_google_entries_keep_google_creds_and_space_scopes() -> None:
    for slug in ("gmail", "gdrive", "gcal"):
        entry = catalog.get(slug)
        assert entry is not None
        assert entry.client_id_env == "GOOGLE_AUTH_CLIENT_ID"
        assert entry.scope_separator == " "


def test_catalog_slugs_unique() -> None:
    slugs = [e.slug for e in catalog.CATALOG]
    assert len(slugs) == len(set(slugs))


def test_authorize_url_uses_scope_separator() -> None:
    url = oauth_client.build_authorize_url(
        authorize_url="https://slack.com/oauth/v2_user/authorize",
        client_id="cid",
        redirect_uri="https://ahs.x/mcp_oauth/callback",
        scopes=["search:read.public", "chat:write"],
        state="st",
        scope_separator=",",
    )
    assert parse_qs(urlparse(url).query)["scope"] == ["search:read.public,chat:write"]


def test_authorize_url_defaults_to_space() -> None:
    url = oauth_client.build_authorize_url(
        authorize_url="https://a.example/auth",
        client_id="cid",
        redirect_uri="https://x/cb",
        scopes=["a", "b"],
        state="st",
    )
    assert parse_qs(urlparse(url).query)["scope"] == ["a b"]


def _response(body: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://slack.com/api/x"))


def _patched_post(resp: httpx.Response) -> Any:
    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    async def _post(*_a: Any, **_k: Any) -> httpx.Response:
        return resp

    client.post = _post
    return patch.object(oauth_client.httpx, "AsyncClient", return_value=client)


_EXCHANGE_KW: dict[str, str] = {
    "token_url": "https://slack.com/api/oauth.v2.user.access",
    "client_id": "cid",
    "client_secret": "sec",
    "code": "c",
    "redirect_uri": "https://x/cb",
}


@pytest.mark.asyncio
async def test_exchange_code_raises_on_slack_ok_false() -> None:
    with (
        _patched_post(_response({"ok": False, "error": "invalid_code"})),
        pytest.raises(RuntimeError, match="invalid_code"),
    ):
        await oauth_client.exchange_code(**_EXCHANGE_KW)


@pytest.mark.asyncio
async def test_exchange_code_returns_slack_user_token() -> None:
    body = {"ok": True, "access_token": "xoxp-1", "token_type": "user", "scope": "a,b"}
    with _patched_post(_response(body)):
        payload = await oauth_client.exchange_code(**_EXCHANGE_KW)
    assert payload["access_token"] == "xoxp-1"


@pytest.mark.asyncio
async def test_refresh_raises_when_no_access_token() -> None:
    with (
        _patched_post(_response({"ok": False, "error": "invalid_refresh_token"})),
        pytest.raises(RuntimeError, match="invalid_refresh_token"),
    ):
        await oauth_client.refresh_access_token(
            token_url="https://slack.com/api/oauth.v2.user.access",
            client_id="cid",
            client_secret="sec",
            refresh_token="r",
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("a b", ["a", "b"]),
        ("a,b", ["a", "b"]),
        ("a, b", ["a", "b"]),
    ],
)
def test_parse_scopes(raw: str | None, expected: list[str] | None) -> None:
    assert oauth_client.parse_scopes(raw) == expected
