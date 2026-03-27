"""MCP tools for Redis key inspection.

Provides tools to get values by exact key and scan keys by pattern.
"""

import json
from typing import Any

from ypl.db.redis import get_redis_client
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()


@mcp_server.tool(
    name="get_redis_value",
    description=(
        "Get a value from Redis by exact key. Use to inspect feature flags (feature_flag:<name>), "
        "dynamic app settings (app_setting:<name>), and cached values. Returns the value, TTL, "
        "and whether it's JSON-encoded."
    ),
)
async def get_redis_value(key: str) -> dict[str, Any]:
    """Get a value from Redis by exact key.

    Args:
        key: The exact Redis key to retrieve

    Returns:
        Dictionary containing the key's value and metadata
    """
    try:
        redis_client = await get_redis_client()

        # Get the value
        value = await redis_client.get(key)

        if value is None:
            return {
                "success": True,
                "key": key,
                "exists": False,
                "value": None,
            }

        # Try to parse as JSON
        parsed_value: Any = value
        is_json = False
        try:
            parsed_value = json.loads(value)
            is_json = True
        except (json.JSONDecodeError, TypeError):
            # If not JSON, ensure it's a string (not bytes)
            if isinstance(value, bytes):
                parsed_value = value.decode("utf-8", errors="replace")

        # Get TTL
        ttl = await redis_client.ttl(key)
        ttl_info = None
        if ttl == -1:
            ttl_info = "no expiry"
        elif ttl == -2:
            ttl_info = "key does not exist"
        else:
            ttl_info = f"{ttl} seconds"

        return {
            "success": True,
            "key": key,
            "exists": True,
            "value": parsed_value,
            "is_json": is_json,
            "ttl": ttl_info,
        }

    except Exception as e:
        logger.warning("Error getting Redis value", error=str(e), key=key)
        return {"success": False, "error": str(e), "key": key}


@mcp_server.tool(
    name="scan_redis_keys",
    description=(
        "Scan Redis keys matching a pattern (supports * wildcard). Use to discover keys before "
        "fetching values with get_redis_value. Common patterns: 'feature_flag:*', 'app_setting:*', "
        "'rate_limited:*'. Returns up to max_keys matching keys."
    ),
)
async def scan_redis_keys(pattern: str, max_keys: int = 100) -> dict[str, Any]:
    """Scan Redis keys matching a pattern.

    Args:
        pattern: Redis key pattern (supports * wildcard)
        max_keys: Maximum number of keys to return

    Returns:
        Dictionary containing matching keys
    """
    try:
        redis_client = await get_redis_client()

        # Use SCAN to find keys (safer than KEYS for production)
        keys: list[str] = []
        cursor = 0
        scan_completed = False

        while len(keys) < max_keys:
            cursor, batch = await redis_client.scan(cursor=cursor, match=pattern, count=100)
            # Decode bytes to strings if needed
            for key in batch:
                if isinstance(key, bytes):
                    keys.append(key.decode("utf-8", errors="replace"))
                else:
                    keys.append(key)
            if cursor == 0:  # Completed full scan
                scan_completed = True
                break

        # Truncate to max_keys and determine if truncated
        truncated = not scan_completed or len(keys) > max_keys
        keys = keys[:max_keys]

        return {
            "success": True,
            "pattern": pattern,
            "count": len(keys),
            "keys": sorted(keys),
            "truncated": truncated,
        }

    except Exception as e:
        logger.warning("Error scanning Redis keys", error=str(e), pattern=pattern)
        return {"success": False, "error": str(e), "pattern": pattern}
