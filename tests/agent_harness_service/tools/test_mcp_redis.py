"""Unit tests for ypl/mcp_server/tools/redis.py."""

from __future__ import annotations
import json
from unittest.mock import AsyncMock, patch

from ypl.mcp_server.tools.redis import get_redis_value, scan_redis_keys


def _make_redis_client(
    get_value: bytes | None = None,
    ttl: int = -1,
    scan_pages: list[tuple[int, list[str]]] | None = None,
) -> AsyncMock:
    """Build an async mock Redis client."""
    client = AsyncMock()
    client.get.return_value = get_value
    client.ttl.return_value = ttl

    if scan_pages is None:
        scan_pages = [(0, [])]
    client.scan.side_effect = scan_pages

    return client


# ---------------------------------------------------------------------------
# get_redis_value
# ---------------------------------------------------------------------------


class TestGetRedisValue:
    async def test_key_not_found(self) -> None:
        redis = _make_redis_client(get_value=None)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("missing:key")

        assert result["success"] is True
        assert result["exists"] is False
        assert result["value"] is None

    async def test_string_value(self) -> None:
        redis = _make_redis_client(get_value=b"hello_world", ttl=3600)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("some:key")

        assert result["success"] is True
        assert result["exists"] is True
        assert result["value"] == "hello_world"
        assert result["is_json"] is False
        assert result["ttl"] == "3600 seconds"

    async def test_json_value(self) -> None:
        payload = {"feature": "enabled", "rollout": 50}
        redis = _make_redis_client(get_value=json.dumps(payload).encode(), ttl=-1)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("feature_flag:foo")

        assert result["success"] is True
        assert result["is_json"] is True
        assert result["value"] == payload
        assert result["ttl"] == "no expiry"

    async def test_ttl_no_expiry(self) -> None:
        redis = _make_redis_client(get_value=b"val", ttl=-1)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("k")
        assert result["ttl"] == "no expiry"

    async def test_ttl_key_does_not_exist(self) -> None:
        redis = _make_redis_client(get_value=b"val", ttl=-2)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("k")
        assert result["ttl"] == "key does not exist"

    async def test_exception_returns_error(self) -> None:
        redis = AsyncMock()
        redis.get.side_effect = ConnectionError("redis down")
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("k")
        assert result["success"] is False
        assert "redis down" in result["error"]

    async def test_bytes_decoded_to_string(self) -> None:
        redis = _make_redis_client(get_value=b"plain text", ttl=60)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await get_redis_value.fn("k")
        assert result["value"] == "plain text"
        assert result["is_json"] is False


# ---------------------------------------------------------------------------
# scan_redis_keys
# ---------------------------------------------------------------------------


class TestScanRedisKeys:
    async def test_scan_returns_matching_keys(self) -> None:
        # One full scan page (cursor=0 means complete)
        scan_pages = [(0, [b"app_setting:foo", b"app_setting:bar"])]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("app_setting:*", max_keys=100)

        assert result["success"] is True
        assert result["count"] == 2
        assert sorted(result["keys"]) == ["app_setting:bar", "app_setting:foo"]
        assert result["truncated"] is False

    async def test_scan_decodes_bytes(self) -> None:
        scan_pages = [(0, [b"feature_flag:x"])]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("feature_flag:*")

        assert "feature_flag:x" in result["keys"]

    async def test_scan_truncates_to_max_keys(self) -> None:
        keys = [f"k:{i}".encode() for i in range(10)]
        scan_pages = [(0, keys)]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("k:*", max_keys=5)

        assert result["count"] == 5
        assert result["truncated"] is True

    async def test_scan_no_keys_found(self) -> None:
        scan_pages = [(0, [])]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("nonexistent:*")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["keys"] == []
        assert result["truncated"] is False

    async def test_scan_multi_page(self) -> None:
        # First page returns cursor 1, second returns cursor 0 (done)
        scan_pages = [
            (1, [b"k:1", b"k:2"]),
            (0, [b"k:3"]),
        ]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("k:*", max_keys=100)

        assert result["count"] == 3
        assert result["truncated"] is False

    async def test_exception_returns_error(self) -> None:
        redis = AsyncMock()
        redis.scan.side_effect = ConnectionError("redis down")
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("k:*")
        assert result["success"] is False
        assert "redis down" in result["error"]

    async def test_string_keys_not_re_decoded(self) -> None:
        # Some redis clients return str directly
        scan_pages = [(0, ["str_key:1", "str_key:2"])]
        redis = _make_redis_client(scan_pages=scan_pages)
        with patch("ypl.mcp_server.tools.redis.get_redis_client", AsyncMock(return_value=redis)):
            result = await scan_redis_keys.fn("str_key:*")

        assert "str_key:1" in result["keys"]
