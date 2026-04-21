"""Unit tests for ypl/mcp_server/tools/twitter.py.

Covers:
- _extract_tweet_id: bare numeric ID, X URL, Twitter URL, invalid input
- _extract_urls: URL extraction from entities
- _get_cached_user_id: cache miss, cache hit, expired TTL
- _cache_user_id: stores entry
- search_twitter: empty query, query too long, bearer token error, 401, 429, non-200,
  successful response, HTTP timeout, generic exception
- get_user_timeline: empty username, bearer token error, user not found (404),
  user resolve rate-limit (429), user resolve non-200, no data in response,
  timeline fetch success, timeline 401, timeline 429, timeout
- get_tweet: invalid input, bearer token error, 401, 429, non-200,
  tweet not found (empty data), success with author, timeout

All tests run without real HTTP I/O.
"""

from __future__ import annotations
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
import ypl.mcp_server.tools.twitter as _twitter_mod
from ypl.mcp_server.tools.twitter import (
    _cache_user_id,
    _extract_tweet_id,
    _extract_urls,
    _get_cached_user_id,
    _twitter_get,
)

search_twitter = _twitter_mod.search_twitter.fn
get_user_timeline = _twitter_mod.get_user_timeline.fn
get_tweet = _twitter_mod.get_tweet.fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_httpx_response(status_code: int, json_data: dict[str, Any] | None = None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    return resp


# ---------------------------------------------------------------------------
# _extract_tweet_id
# ---------------------------------------------------------------------------


class TestExtractTweetId:
    def test_bare_numeric_id(self) -> None:
        assert _extract_tweet_id("12345678901234567") == "12345678901234567"

    def test_x_com_url(self) -> None:
        url = "https://x.com/user/status/2036359521329881448"
        assert _extract_tweet_id(url) == "2036359521329881448"

    def test_twitter_com_url(self) -> None:
        url = "https://twitter.com/user/status/1234567890"
        assert _extract_tweet_id(url) == "1234567890"

    def test_www_x_com_url_with_query_string(self) -> None:
        url = "https://www.x.com/alice/status/9999999?s=20"
        assert _extract_tweet_id(url) == "9999999"

    def test_invalid_input_returns_none(self) -> None:
        assert _extract_tweet_id("not-a-tweet") is None
        assert _extract_tweet_id("https://example.com") is None

    def test_whitespace_stripped(self) -> None:
        assert _extract_tweet_id("  12345  ") == "12345"


# ---------------------------------------------------------------------------
# _extract_urls
# ---------------------------------------------------------------------------


class TestExtractUrls:
    def test_none_entities_returns_empty(self) -> None:
        assert _extract_urls(None) == []

    def test_empty_entities_returns_empty(self) -> None:
        assert _extract_urls({}) == []

    def test_extracts_urls(self) -> None:
        entities = {
            "urls": [
                {"expanded_url": "https://example.com", "display_url": "example.com"},
                {"expanded_url": "https://other.com", "display_url": "other.com"},
            ]
        }
        result = _extract_urls(entities)
        assert len(result) == 2
        assert result[0]["expanded_url"] == "https://example.com"

    def test_skips_urls_without_expanded_url(self) -> None:
        entities = {"urls": [{"display_url": "example.com"}]}
        result = _extract_urls(entities)
        assert result == []


# ---------------------------------------------------------------------------
# _get_cached_user_id / _cache_user_id
# ---------------------------------------------------------------------------


class TestUserIdCache:
    def test_cache_miss_returns_none(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("no_user", None)
        assert _get_cached_user_id("no_user") is None

    def test_cache_hit_returns_user_id(self) -> None:
        _cache_user_id("testuser", "uid-999")
        assert _get_cached_user_id("testuser") == "uid-999"
        _twitter_mod._USER_ID_CACHE.pop("testuser", None)

    def test_expired_entry_returns_none(self) -> None:
        # Insert an expired entry
        _twitter_mod._USER_ID_CACHE["olduser"] = ("uid-old", time.monotonic() - 999999)
        assert _get_cached_user_id("olduser") is None
        assert "olduser" not in _twitter_mod._USER_ID_CACHE

    def test_cache_is_case_insensitive(self) -> None:
        _cache_user_id("UpperUser", "uid-123")
        assert _get_cached_user_id("upperuser") == "uid-123"
        _twitter_mod._USER_ID_CACHE.pop("upperuser", None)


# ---------------------------------------------------------------------------
# search_twitter
# ---------------------------------------------------------------------------


class TestSearchTwitter:
    async def test_empty_query_returns_error(self) -> None:
        result = await search_twitter("   ")
        assert "error" in result
        assert "empty" in result["error"].lower()

    async def test_query_too_long_returns_error(self) -> None:
        result = await search_twitter("x" * 513)
        assert "error" in result
        assert "long" in result["error"].lower()

    async def test_bearer_token_error_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.twitter._get_bearer_token",
            side_effect=ValueError("no token"),
        ):
            result = await search_twitter("hello")

        assert "error" in result
        assert "no token" in result["error"]

    async def test_401_returns_auth_error(self) -> None:
        resp = _make_httpx_response(401)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await search_twitter("test")

        assert "Authentication failed" in result["error"]

    async def test_429_returns_rate_limit_error(self) -> None:
        resp = _make_httpx_response(429)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await search_twitter("test")

        assert "Rate limit" in result["error"]

    async def test_non_200_returns_error(self) -> None:
        resp = _make_httpx_response(500, text="Server Error")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await search_twitter("test")

        assert "500" in result["error"]

    async def test_success_returns_tweets(self) -> None:
        data = {
            "data": [
                {
                    "id": "123",
                    "text": "Hello world",
                    "author_id": "uid1",
                    "created_at": "2024-01-01T00:00:00Z",
                    "public_metrics": {
                        "retweet_count": 5,
                        "like_count": 10,
                        "reply_count": 2,
                        "quote_count": 1,
                        "bookmark_count": 0,
                    },
                    "entities": None,
                }
            ],
            "includes": {
                "users": [
                    {
                        "id": "uid1",
                        "username": "alice",
                        "name": "Alice",
                        "public_metrics": {"followers_count": 1000},
                    }
                ]
            },
        }
        resp = _make_httpx_response(200, json_data=data)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await search_twitter("hello")

        assert "tweets" in result
        assert len(result["tweets"]) == 1
        tweet = result["tweets"][0]
        assert tweet["text"] == "Hello world"
        assert tweet["author_username"] == "alice"
        assert tweet["author_followers"] == 1000

    async def test_timeout_returns_error(self) -> None:
        timeout_err = httpx.TimeoutException("timeout")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=timeout_err)),
        ):
            result = await search_twitter("test")

        assert "timed out" in result["error"].lower()

    async def test_generic_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await search_twitter("test")

        assert "Unexpected error" in result["error"]

    async def test_max_results_clamped(self) -> None:
        """max_results is clamped to [10, 100]."""
        captured_params: list[dict[str, Any]] = []

        async def fake_twitter_get(client: Any, url: str, params: dict[str, Any], bearer_token: str) -> MagicMock:
            captured_params.append(params)
            return _make_httpx_response(200, json_data={"data": [], "includes": {}})

        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=fake_twitter_get)),
        ):
            await search_twitter("test", max_results=200)

        assert captured_params[0]["max_results"] == 100


# ---------------------------------------------------------------------------
# get_user_timeline
# ---------------------------------------------------------------------------


class TestGetUserTimeline:
    async def test_empty_username_returns_error(self) -> None:
        result = await get_user_timeline("@")
        assert "error" in result
        assert "empty" in result["error"].lower()

    async def test_strips_leading_at(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("alice", None)

        user_resp_data = {"data": {"id": "uid1"}}
        timeline_data: dict[str, Any] = {
            "data": [{"id": "tw1", "text": "Hi", "public_metrics": {}, "entities": None, "created_at": "2024-01-01"}]
        }

        user_resp = _make_httpx_response(200, json_data=user_resp_data)
        timeline_resp = _make_httpx_response(200, json_data=timeline_data)

        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=[user_resp, timeline_resp])),
        ):
            result = await get_user_timeline("@alice")

        assert "tweets" in result
        _twitter_mod._USER_ID_CACHE.pop("alice", None)

    async def test_bearer_token_error(self) -> None:
        err = ValueError("no secret")
        with patch("ypl.mcp_server.tools.twitter._get_bearer_token", side_effect=err):
            result = await get_user_timeline("someuser")

        assert "error" in result
        assert "no secret" in result["error"]

    async def test_user_not_found_404(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("nobody", None)

        resp = _make_httpx_response(404)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("nobody")

        assert "not found" in result["error"].lower()

    async def test_user_resolve_rate_limit(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("ratelimited", None)

        resp = _make_httpx_response(429)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("ratelimited")

        assert "Rate limit" in result["error"]

    async def test_user_resolve_non_200(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("baduser", None)

        resp = _make_httpx_response(503)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("baduser")

        assert "503" in result["error"]

    async def test_user_data_empty_returns_error(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("emptyuser", None)

        resp = _make_httpx_response(200, json_data={"data": None})
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("emptyuser")

        assert "not found" in result["error"].lower()

    async def test_timeline_success_from_cache(self) -> None:
        _cache_user_id("cached_user", "uid-cached")

        timeline_data = {
            "data": [
                {
                    "id": "tw1",
                    "text": "Cached tweet",
                    "created_at": "2024-01-01",
                    "public_metrics": {
                        "retweet_count": 1,
                        "like_count": 2,
                        "reply_count": 0,
                        "quote_count": 0,
                        "bookmark_count": 0,
                    },
                    "entities": {"urls": [{"expanded_url": "https://link.com", "display_url": "link.com"}]},
                }
            ]
        }
        resp = _make_httpx_response(200, json_data=timeline_data)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("cached_user")

        assert "tweets" in result
        assert result["tweets"][0]["text"] == "Cached tweet"
        assert len(result["tweets"][0]["urls"]) == 1
        _twitter_mod._USER_ID_CACHE.pop("cached_user", None)

    async def test_timeline_401(self) -> None:
        _cache_user_id("auth_fail_user", "uid-auth")

        resp = _make_httpx_response(401)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_user_timeline("auth_fail_user")

        assert "Authentication failed" in result["error"]
        _twitter_mod._USER_ID_CACHE.pop("auth_fail_user", None)

    async def test_timeline_timeout(self) -> None:
        _cache_user_id("timeout_user", "uid-timeout")

        timeout_err = httpx.TimeoutException("timeout")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=timeout_err)),
        ):
            result = await get_user_timeline("timeout_user")

        assert "timed out" in result["error"].lower()
        _twitter_mod._USER_ID_CACHE.pop("timeout_user", None)

    async def test_username_resolve_timeout(self) -> None:
        _twitter_mod._USER_ID_CACHE.pop("slow_user", None)

        resolve_timeout = httpx.TimeoutException("timeout")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=resolve_timeout)),
        ):
            result = await get_user_timeline("slow_user")

        assert "Timed out" in result["error"]


# ---------------------------------------------------------------------------
# get_tweet
# ---------------------------------------------------------------------------


class TestGetTweet:
    async def test_invalid_input_returns_error(self) -> None:
        result = await get_tweet("not-a-tweet-url-or-id")
        assert "error" in result
        assert "Could not extract" in result["error"]

    async def test_bearer_token_error(self) -> None:
        bearer_err = ValueError("no secret")
        with patch("ypl.mcp_server.tools.twitter._get_bearer_token", side_effect=bearer_err):
            result = await get_tweet("12345678901234567")

        assert "no secret" in result["error"]

    async def test_401_returns_auth_error(self) -> None:
        resp = _make_httpx_response(401)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("12345678901234567")

        assert "Authentication failed" in result["error"]

    async def test_429_returns_rate_limit_error(self) -> None:
        resp = _make_httpx_response(429)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("12345678901234567")

        assert "Rate limit" in result["error"]

    async def test_non_200_returns_error(self) -> None:
        resp = _make_httpx_response(503, text="Service Unavailable")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("12345678901234567")

        assert "503" in result["error"]

    async def test_tweet_not_found_empty_data(self) -> None:
        data = {"data": None, "errors": [{"detail": "Tweet not found (deleted)"}]}
        resp = _make_httpx_response(200, json_data=data)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("12345678901234567")

        assert "error" in result
        assert "not found" in result["error"].lower() or "deleted" in result["error"].lower()

    async def test_success_with_author(self) -> None:
        data = {
            "data": {
                "id": "123",
                "text": "Test tweet",
                "author_id": "uid1",
                "created_at": "2024-01-01T00:00:00Z",
                "lang": "en",
                "conversation_id": "123",
                "in_reply_to_user_id": None,
                "public_metrics": {"like_count": 5},
                "entities": None,
            },
            "includes": {
                "users": [
                    {
                        "id": "uid1",
                        "username": "alice",
                        "name": "Alice",
                        "verified": True,
                        "profile_image_url": "https://example.com/img.jpg",
                    }
                ]
            },
        }
        resp = _make_httpx_response(200, json_data=data)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("123")

        assert "tweet" in result
        tweet = result["tweet"]
        assert tweet["text"] == "Test tweet"
        assert tweet["author"]["username"] == "alice"
        assert tweet["author"]["verified"] is True

    async def test_success_with_url_input(self) -> None:
        data = {
            "data": {
                "id": "9999",
                "text": "URL tweet",
                "author_id": "uid2",
                "created_at": "2024-01-01",
                "lang": "en",
                "conversation_id": "9999",
                "in_reply_to_user_id": None,
                "public_metrics": {},
                "entities": None,
            },
            "includes": {"users": []},
        }
        resp = _make_httpx_response(200, json_data=data)
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(return_value=resp)),
        ):
            result = await get_tweet("https://x.com/user/status/9999")

        assert "tweet" in result
        assert result["tweet"]["id"] == "9999"
        # No author when users list is empty
        assert result["tweet"]["author"] is None

    async def test_timeout_returns_error(self) -> None:
        tweet_timeout = httpx.TimeoutException("timeout")
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=tweet_timeout)),
        ):
            result = await get_tweet("12345678901234567")

        assert "timed out" in result["error"].lower()

    async def test_generic_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.twitter._get_bearer_token", return_value="tok"),
            patch("ypl.mcp_server.tools.twitter._twitter_get", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await get_tweet("12345678901234567")

        assert "Unexpected error" in result["error"]


# ---------------------------------------------------------------------------
# _twitter_get — rate limit retry logic (lightweight)
# ---------------------------------------------------------------------------


class TestTwitterGet:
    async def test_no_retry_on_200(self) -> None:
        client = AsyncMock()
        resp = _make_httpx_response(200)
        client.get = AsyncMock(return_value=resp)

        result = await _twitter_get(client, "https://api.x.com/2/tweets/search/recent", {}, "tok")
        assert result.status_code == 200
        client.get.assert_called_once()

    async def test_retries_once_on_429(self) -> None:
        client = AsyncMock()
        resp_429 = _make_httpx_response(429)
        resp_200 = _make_httpx_response(200)
        client.get = AsyncMock(side_effect=[resp_429, resp_200])

        with patch("ypl.mcp_server.tools.twitter.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await _twitter_get(client, "https://api.x.com/2/test", {}, "tok")

        mock_sleep.assert_called_once_with(60)
        assert result.status_code == 200
        assert client.get.call_count == 2
