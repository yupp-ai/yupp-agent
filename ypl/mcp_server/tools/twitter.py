"""MCP tools for Twitter/X integration.

Provides tools for:
- Searching recent tweets (last 7 days) via the v2 recent search endpoint
- Fetching a user's timeline via the v2 user tweets endpoint
- Looking up individual tweets by ID or URL via the v2 tweet lookup endpoint

Note: Only recent search is supported. Full-archive search is not implemented.
"""

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import httpx
from google.api_core import exceptions as core_exceptions
from google.cloud import secretmanager_v1 as secretmanager

from ypl.backend.config import settings
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

_X_API_BASE_URL = "https://api.x.com/2"
_X_API_RECENT_SEARCH_URL = f"{_X_API_BASE_URL}/tweets/search/recent"
_X_API_TWEET_LOOKUP_URL = _X_API_BASE_URL + "/tweets/{tweet_id}"
_X_API_USER_BY_USERNAME_URL = _X_API_BASE_URL + "/users/by/username/{username}"
_X_API_USER_TWEETS_URL = _X_API_BASE_URL + "/users/{user_id}/tweets"

_X_API_BEARER_TOKEN_SECRET_NAME = "twitter-api-bearer-token"
_MAX_RESULTS_LIMIT = 100
_RATE_LIMIT_WAIT_SECONDS = 60
_SINCE_HOURS_MIN = 1
_SINCE_HOURS_MAX = 168  # Twitter v2 recent search covers last 7 days

# Tweet/user fields requested from the API
_TWEET_FIELDS = "created_at,author_id,public_metrics,entities"
_USER_FIELDS = "username,name,public_metrics"
# Fields for single-user lookup (get_tweet expansions and get_user_timeline resolve step)
_USER_LOOKUP_FIELDS = "username,name,verified,profile_image_url"

# Extended fields for single tweet lookup
_TWEET_LOOKUP_FIELDS = "created_at,author_id,text,public_metrics,lang,conversation_id,in_reply_to_user_id,entities"

# Regex to extract tweet ID from various X/Twitter URL formats
_TWEET_URL_PATTERN = re.compile(r"(?:https?://)?(?:(?:www\.)?(?:twitter\.com|x\.com))/\w+/status/(\d+)")

# In-process cache: username (lowercase) → (user_id, monotonic_timestamp)
_USER_ID_CACHE: dict[str, tuple[str, float]] = {}
_USER_ID_CACHE_TTL_SECONDS = 86400  # 24 hours


@lru_cache
def _get_secret_manager_client() -> secretmanager.SecretManagerServiceAsyncClient:
    """Return a cached async Secret Manager client."""
    return secretmanager.SecretManagerServiceAsyncClient()


# TODO: Extract _get_secret_manager_client + _get_bearer_token into a shared GCP Secret Manager
# util (e.g. ypl/backend/utils/gcp_secrets.py) so other modules (SAG, etc.) can reuse it
# without copy-pasting the client setup. Tracked in PR #11258 review.
async def _get_bearer_token() -> str:
    """Fetch the X/Twitter API bearer token from GCP Secret Manager.

    Called at request time — never cached in memory to avoid accidental exposure.
    Raises ValueError if the secret cannot be retrieved.
    """
    if not settings.GCP_PROJECT_ID:
        raise ValueError("GCP_PROJECT_ID not configured; cannot fetch Twitter bearer token")

    client = _get_secret_manager_client()
    name = f"projects/{settings.GCP_PROJECT_ID}/secrets/{_X_API_BEARER_TOKEN_SECRET_NAME}/versions/latest"
    try:
        response = await client.access_secret_version(request={"name": name}, timeout=5.0)
        return response.payload.data.decode("UTF-8")
    except core_exceptions.NotFound:
        raise ValueError(f"GCP secret '{_X_API_BEARER_TOKEN_SECRET_NAME}' not found") from None
    except Exception as e:
        raise ValueError(f"Failed to fetch Twitter bearer token from Secret Manager: {e}") from e


async def _twitter_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    bearer_token: str,
) -> httpx.Response:
    """GET request to the Twitter v2 API with one 60s rate-limit retry."""
    headers = {"Authorization": f"Bearer {bearer_token}"}
    response = await client.get(url, params=params, headers=headers)
    if response.status_code == 429:
        logger.warning("Twitter API rate limited; waiting 60s and retrying once", url=url)
        await asyncio.sleep(_RATE_LIMIT_WAIT_SECONDS)
        response = await client.get(url, params=params, headers=headers)
        if response.status_code == 429:
            logger.warning("Twitter API still rate limited after retry", url=url)
    return response


def _extract_urls(entities: dict[str, Any] | None) -> list[dict[str, str]]:
    """Extract URL objects from tweet entities."""
    if not entities:
        return []
    return [
        {"expanded_url": u.get("expanded_url", ""), "display_url": u.get("display_url", "")}
        for u in entities.get("urls", [])
        if u.get("expanded_url")
    ]


def _get_cached_user_id(username: str) -> str | None:
    """Return cached user_id for username if present and not expired."""
    entry = _USER_ID_CACHE.get(username.lower())
    if entry is None:
        return None
    user_id, cached_at = entry
    if time.monotonic() - cached_at > _USER_ID_CACHE_TTL_SECONDS:
        del _USER_ID_CACHE[username.lower()]
        return None
    return user_id


def _cache_user_id(username: str, user_id: str) -> None:
    """Cache username → user_id with current timestamp."""
    _USER_ID_CACHE[username.lower()] = (user_id, time.monotonic())


@mcp_server.tool(
    name="search_twitter",
    description=(
        "Search recent tweets (last 7 days) on X/Twitter using the v2 API. "
        "Accepts a search query string with support for X API operators: "
        'keywords, "exact phrases", from:user, #hashtag, @mention, '
        "has:links, is:retweet, is:reply, lang:xx, -negation, OR, and grouping with (). "
        "Returns up to max_results tweets (default 20, max 100) posted within the last since_hours hours. "
        "Includes extracted URLs from tweet entities — useful for discovering linked articles."
    ),
)
async def search_twitter(
    query: str,
    max_results: int = 20,
    since_hours: int = 24,
) -> dict[str, Any]:
    """Search recent tweets on X/Twitter.

    Args:
        query: Search query string supporting X API operators (max 512 chars).
        max_results: Number of results to return (10-100, default 20).
        since_hours: How many hours back to search (1-168, default 24, max 7 days = 168h).

    Returns:
        Dict with ``tweets`` key containing a list of tweet dicts (text, author info,
        engagement metrics, extracted URLs). On error, returns ``{"error": "..."}``.
    """
    if not query or not query.strip():
        return {"error": "Query cannot be empty"}

    if len(query) > 512:
        return {"error": f"Query too long ({len(query)} chars). Max is 512 characters."}

    max_results = max(10, min(max_results, _MAX_RESULTS_LIMIT))
    since_hours = max(_SINCE_HOURS_MIN, min(since_hours, _SINCE_HOURS_MAX))

    try:
        bearer_token = await _get_bearer_token()
    except ValueError as e:
        return {"error": str(e)}

    start_time = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "tweet.fields": _TWEET_FIELDS,
        "expansions": "author_id",
        "user.fields": _USER_FIELDS,
        "start_time": start_time,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _twitter_get(client, _X_API_RECENT_SEARCH_URL, params, bearer_token)

        if response.status_code == 401:
            return {"error": "Authentication failed. Check twitter-api-bearer-token secret."}
        if response.status_code == 429:
            return {"error": "Rate limit exceeded. Try again later."}
        if response.status_code != 200:
            return {"error": f"X API returned status {response.status_code}: {response.text[:500]}"}

        data = response.json()
        tweets = data.get("data", [])

        # Build author_id → user info map from expansions
        user_map: dict[str, dict[str, Any]] = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

        results = []
        for tweet in tweets:
            user = user_map.get(tweet.get("author_id", ""), {})
            metrics = tweet.get("public_metrics", {})
            results.append(
                {
                    "id": tweet.get("id"),
                    "text": tweet.get("text"),
                    "author_username": user.get("username"),
                    "author_name": user.get("name"),
                    "author_followers": (user.get("public_metrics") or {}).get("followers_count"),
                    "created_at": tweet.get("created_at"),
                    "retweet_count": metrics.get("retweet_count"),
                    "like_count": metrics.get("like_count"),
                    "reply_count": metrics.get("reply_count"),
                    "quote_count": metrics.get("quote_count"),
                    "bookmark_count": metrics.get("bookmark_count"),
                    "urls": _extract_urls(tweet.get("entities")),
                }
            )

        return {"tweets": results}

    except httpx.TimeoutException:
        logger.warning("Twitter search timed out", query=query)
        return {"error": "Request to X API timed out"}
    except Exception as e:
        logger.exception("Twitter search failed", query=query, error=str(e))
        return {"error": f"Unexpected error: {str(e)}"}


@mcp_server.tool(
    name="get_user_timeline",
    description=(
        "Fetch recent tweets from a specific X/Twitter user's timeline. "
        "Resolves the username to a user ID (cached 24h to avoid extra API calls), "
        "then fetches their tweets. Excludes retweets and replies by default. "
        "Returns up to max_results tweets (default 10, max 100) from the last since_hours hours."
    ),
)
async def get_user_timeline(
    username: str,
    max_results: int = 10,
    since_hours: int = 24,
) -> dict[str, Any]:
    """Fetch tweets from a user's timeline.

    Args:
        username: X/Twitter username (with or without leading @).
        max_results: Number of results to return (5-100, default 10).
        since_hours: How many hours back to fetch (1-168, default 24, max 7 days = 168h).

    Returns:
        Dict with ``tweets`` key containing a list of tweet dicts (text, metrics, URLs).
        On error, returns ``{"error": "..."}``.
    """
    username = username.lstrip("@").strip()
    if not username:
        return {"error": "Username cannot be empty"}

    max_results = max(5, min(max_results, _MAX_RESULTS_LIMIT))
    since_hours = max(_SINCE_HOURS_MIN, min(since_hours, _SINCE_HOURS_MAX))

    try:
        bearer_token = await _get_bearer_token()
    except ValueError as e:
        return {"error": str(e)}

    # Resolve username → user_id (with 24h in-process cache)
    # TODO: On a cache miss, username resolution and tweet fetch each open a separate AsyncClient.
    # Both hit api.x.com so connection reuse would save one TLS handshake. Refactor into a single
    # shared client if the multi-early-return structure of this function is simplified. (PR #11258)
    user_id = _get_cached_user_id(username)
    if user_id is None:
        resolve_url = _X_API_USER_BY_USERNAME_URL.format(username=username)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await _twitter_get(client, resolve_url, {}, bearer_token)
            if resp.status_code == 404:
                return {"error": f"User @{username} not found"}
            if resp.status_code == 429:
                return {"error": "Rate limit exceeded resolving username. Try again later."}
            if resp.status_code != 200:
                return {"error": f"X API returned status {resp.status_code} resolving @{username}"}
            user_data = resp.json().get("data")
            if not user_data:
                return {"error": f"User @{username} not found"}
            user_id = user_data["id"]
            _cache_user_id(username, user_id)
        except httpx.TimeoutException:
            return {"error": f"Timed out resolving username @{username}"}
        except Exception as e:
            logger.exception("Failed to resolve Twitter username", username=username, error=str(e))
            return {"error": f"Failed to resolve username: {str(e)}"}

    start_time = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {
        "max_results": max_results,
        "tweet.fields": _TWEET_FIELDS,
        "start_time": start_time,
        "exclude": "retweets,replies",
    }

    try:
        tweets_url = _X_API_USER_TWEETS_URL.format(user_id=user_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _twitter_get(client, tweets_url, params, bearer_token)

        if response.status_code == 401:
            return {"error": "Authentication failed. Check twitter-api-bearer-token secret."}
        if response.status_code == 429:
            return {"error": "Rate limit exceeded. Try again later."}
        if response.status_code != 200:
            return {"error": f"X API returned status {response.status_code}: {response.text[:500]}"}

        data = response.json()
        tweets = data.get("data", [])

        results = []
        for tweet in tweets:
            metrics = tweet.get("public_metrics", {})
            results.append(
                {
                    "id": tweet.get("id"),
                    "text": tweet.get("text"),
                    "created_at": tweet.get("created_at"),
                    "retweet_count": metrics.get("retweet_count"),
                    "like_count": metrics.get("like_count"),
                    "reply_count": metrics.get("reply_count"),
                    "quote_count": metrics.get("quote_count"),
                    "bookmark_count": metrics.get("bookmark_count"),
                    "urls": _extract_urls(tweet.get("entities")),
                }
            )

        return {"tweets": results}

    except httpx.TimeoutException:
        logger.warning("User timeline fetch timed out", username=username)
        return {"error": "Request to X API timed out"}
    except Exception as e:
        logger.exception("User timeline fetch failed", username=username, error=str(e))
        return {"error": f"Unexpected error: {str(e)}"}


def _extract_tweet_id(tweet_id_or_url: str) -> str | None:
    """Extract a tweet ID from a tweet ID string or X/Twitter URL.

    Supports URLs like:
      - https://x.com/user/status/123456789
      - https://twitter.com/user/status/123456789
      - https://www.x.com/user/status/123456789?s=20
    Also accepts a bare numeric tweet ID.
    """
    tweet_id_or_url = tweet_id_or_url.strip()
    if tweet_id_or_url.isdigit():
        return tweet_id_or_url
    match = _TWEET_URL_PATTERN.search(tweet_id_or_url)
    if match:
        return match.group(1)
    return None


@mcp_server.tool(
    name="get_tweet",
    description=(
        "Look up a single tweet/post on X/Twitter by its ID or URL. "
        "Accepts a numeric tweet ID (e.g. '2036359521329881448') or a full URL "
        "(e.g. 'https://x.com/user/status/2036359521329881448'). "
        "Returns the tweet text, author info (username, name, verified status), "
        "creation time, public metrics (likes, retweets, replies, quotes), "
        "language, conversation ID, and entities (URLs, mentions, hashtags)."
    ),
)
async def get_tweet(
    tweet_id_or_url: str,
) -> dict[str, Any]:
    """Look up a single tweet by ID or URL.

    Args:
        tweet_id_or_url: A numeric tweet ID or a full X/Twitter URL
            (e.g. 'https://x.com/user/status/123456789').

    Returns:
        Dictionary with tweet data including text, author, metrics, and entities.
    """
    tweet_id = _extract_tweet_id(tweet_id_or_url)
    if not tweet_id:
        return {
            "error": (
                "Could not extract tweet ID. Provide a numeric ID or a URL like https://x.com/user/status/123456789"
            ),
        }

    try:
        bearer_token = await _get_bearer_token()
    except ValueError as e:
        return {"error": str(e)}

    url = _X_API_TWEET_LOOKUP_URL.format(tweet_id=tweet_id)
    params: dict[str, str] = {
        "tweet.fields": _TWEET_LOOKUP_FIELDS,
        "expansions": "author_id",
        "user.fields": _USER_LOOKUP_FIELDS,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _twitter_get(client, url, params, bearer_token)

        if response.status_code == 401:
            return {"error": "Authentication failed. Check twitter-api-bearer-token secret."}
        if response.status_code == 429:
            return {"error": "Rate limit exceeded. Try again later."}
        if response.status_code != 200:
            return {"error": f"X API returned status {response.status_code}: {response.text[:500]}"}

        data = response.json()
        tweet = data.get("data")
        if not tweet:
            errors = data.get("errors", [])
            error_msg = errors[0].get("detail", "Tweet not found") if errors else "Tweet not found"
            return {"error": error_msg}

        # Resolve author from expansions
        author: dict[str, Any] | None = None
        users = data.get("includes", {}).get("users", [])
        if users:
            user = users[0]
            author = {
                "id": user.get("id"),
                "username": user.get("username"),
                "name": user.get("name"),
                "verified": user.get("verified"),
                "profile_image_url": user.get("profile_image_url"),
            }

        return {
            "tweet": {
                "id": tweet.get("id"),
                "text": tweet.get("text"),
                "author_id": tweet.get("author_id"),
                "author": author,
                "created_at": tweet.get("created_at"),
                "lang": tweet.get("lang"),
                "conversation_id": tweet.get("conversation_id"),
                "in_reply_to_user_id": tweet.get("in_reply_to_user_id"),
                "metrics": tweet.get("public_metrics"),
                "entities": tweet.get("entities"),
            },
        }

    except httpx.TimeoutException:
        logger.warning("Tweet lookup timed out", tweet_id=tweet_id)
        return {"error": "Request to X API timed out"}
    except Exception as e:
        logger.exception("Tweet lookup failed", tweet_id=tweet_id, error=str(e))
        return {"error": f"Unexpected error: {str(e)}"}
