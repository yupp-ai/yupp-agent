---
name: search-twitter
description: Search recent tweets and look up individual posts on X/Twitter. Use when you need to find tweets about a topic, monitor mentions, research public sentiment, or get the content of a specific tweet given its URL or ID.
allowed-tools: mcp__agcouch-mcp-server__search_twitter, mcp__agcouch-mcp-server__get_tweet
---

# X/Twitter Skill

Two MCP tools are available:
- `search_twitter` — search recent tweets (last 7 days) with query operators
- `get_tweet` — look up a single tweet by ID or URL

## Quick Start

```python
# Search for tweets
search_twitter(query="agent harness", max_results=10)

# Look up a specific tweet by URL
get_tweet(tweet_id_or_url="https://x.com/anthropic/status/2036359521329881448")

# Or by bare tweet ID
get_tweet(tweet_id_or_url="2036359521329881448")
```

## Query Operators Reference

### Standalone Operators (can be used alone)

| Operator | Description | Example |
|----------|-------------|---------|
| `keyword` | Tokenized match in post body | `pepsi cola` |
| `"exact phrase"` | Exact phrase match | `"agent harness"` |
| `#hashtag` | Match hashtag | `#AI` |
| `@mention` | Match username mention | `@anthropic` |
| `$cashtag` | Match cashtag | `$TSLA` |
| `from:` | Posts by a specific user | `from:anthropic` |
| `to:` | Replies to a specific user | `to:elonmusk` |
| `retweets_of:` | Retweets of a user's posts | `retweets_of:openai` |
| `url:` | Posts containing a URL (tokenized) | `url:"github.com"` |
| `conversation_id:` | All posts in a thread | `conversation_id:1334987486343299072` |
| `context:` | Domain/entity context annotation | `context:10.799022225751871488` |
| `entity:` | Named entity string | `entity:"Claude"` |
| `list:` | Posts from list members | `list:123456` |

### Conjunction-Required Operators (must combine with a standalone operator)

| Operator | Description | Example |
|----------|-------------|---------|
| `is:retweet` | Include/exclude retweets | `AI -is:retweet` |
| `is:reply` | Include/exclude replies | `from:anthropic is:reply` |
| `is:quote` | Include/exclude quote tweets | `LLM is:quote` |
| `is:verified` | Posts from verified users | `#AI is:verified` |
| `has:hashtags` | Posts containing hashtags | `from:openai has:hashtags` |
| `has:links` | Posts containing URLs | `"agent harness" has:links` |
| `has:media` | Posts with images/video/GIFs | `AI has:media` |
| `has:images` | Posts with images | `#meme has:images` |
| `has:video_link` | Posts with native video | `tutorial has:video_link` |
| `has:mentions` | Posts with @mentions | `#AI has:mentions` |
| `has:cashtags` | Posts with $cashtags | `earnings has:cashtags` |
| `has:geo` | Posts with geolocation | `event has:geo` |
| `lang:` | Filter by language (BCP 47) | `AI lang:en` |

### Location Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `place:` | Tagged place name | `place:"San Francisco"` |
| `place_country:` | Country code filter | `place_country:US` |
| `point_radius:` | Geographic radius search | `point_radius:[lon lat radius]` |
| `bounding_box:` | Geographic bounding box | `bounding_box:[w s e n]` |

### Boolean & Grouping

| Syntax | Description | Example |
|--------|-------------|---------|
| `OR` | Logical OR | `cat OR dog` |
| (space) | Logical AND (implicit) | `cat dog` |
| `()` | Group expressions | `(AI OR LLM) lang:en` |
| `-` | Negate/exclude | `-is:retweet` |

## Time Constraints

Narrow results within the 7-day window using these optional parameters:

| Parameter | Format | Description |
|-----------|--------|-------------|
| `start_time` | `YYYY-MM-DDTHH:mm:ssZ` | Oldest UTC timestamp (inclusive). Must be within last 7 days. |
| `end_time` | `YYYY-MM-DDTHH:mm:ssZ` | Newest UTC timestamp (exclusive). |
| `since_id` | Tweet ID (1-19 digits) | Results newer than this tweet ID. |
| `until_id` | Tweet ID (1-19 digits) | Results older than this tweet ID. |
| `sort_order` | `recency` or `relevancy` | Result ordering (default varies by endpoint). |

**Notes:**
- `start_time` and `since_id` are mutually exclusive (API will error if both set).
- `end_time` and `until_id` are mutually exclusive.
- All timestamps are UTC in ISO 8601 second granularity.

## Limits

- **Query length**: Max 512 characters (self-serve tier)
- **Results per request**: 10–100 (default 20)
- **Time range**: Recent search covers the last 7 days only
- **Rate limits**: 450 requests per 15-minute window (app-level)

## Example Queries

```python
# Simple keyword search
search_twitter(query="agent harness", max_results=20)

# Tweets from a user, English only, no retweets
search_twitter(query="from:anthropic lang:en -is:retweet", max_results=50)

# Multiple keywords with media
search_twitter(query="(Claude OR GPT) has:media -is:reply", max_results=30)

# Exact phrase with links
search_twitter(query="\"harness engineering\" has:links lang:en", max_results=10)

# Monitor mentions of a product
search_twitter(query="@yuaboratory OR #yupp", max_results=100)

# Time-constrained search (replace with actual recent timestamps)
search_twitter(query="AI agents", start_time="<YYYY-MM-DDTHH:mm:ssZ>", sort_order="recency")

# Search between specific timestamps (must be within last 7 days)
search_twitter(query="LLM benchmark", start_time="<START_YYYY-MM-DDTHH:mm:ssZ>", end_time="<END_YYYY-MM-DDTHH:mm:ssZ>")

# Paginate using next_token from a previous response
search_twitter(query="Claude", next_token="<next_token_from_previous_response>")
```

## Tweet Lookup (`get_tweet`)

Look up a single tweet by its URL or numeric ID. Returns full tweet content, author details, metrics, and entities.

### Accepted Input Formats

- Full URL: `https://x.com/user/status/123456789`
- Legacy URL: `https://twitter.com/user/status/123456789`
- URLs with query params: `https://x.com/user/status/123456789?s=20`
- Bare tweet ID: `123456789`

### Response Fields

| Field | Description |
|-------|-------------|
| `text` | Full tweet text |
| `author` | `username`, `name`, `verified`, `profile_image_url` |
| `created_at` | ISO 8601 creation timestamp |
| `metrics` | `like_count`, `retweet_count`, `reply_count`, `quote_count` |
| `lang` | BCP 47 language code |
| `conversation_id` | Thread root tweet ID |
| `in_reply_to_user_id` | User ID being replied to (if reply) |
| `entities` | URLs, mentions, hashtags, cashtags extracted from text |

### Examples

```python
# Look up a tweet by URL (most common usage)
get_tweet(tweet_id_or_url="https://x.com/anthropic/status/2036359521329881448")

# Look up by bare ID
get_tweet(tweet_id_or_url="2036359521329881448")

# Works with old twitter.com URLs too
get_tweet(tweet_id_or_url="https://twitter.com/elonmusk/status/2036359521329881448")
```
