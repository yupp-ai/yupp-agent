"""Translate a free-text search string into a structured SearchQuery using Haiku.

Usage::

    query = await text_to_iql("failed tasks for eng-raccoon last week")
    # → SearchQuery(q="failed tasks", types=[SearchType.TASKS], agent="eng-raccoon",
    #               status="FAILED", date_from=...)

Fallback: any error (LLM call failure, timeout, invalid JSON) returns ``SearchQuery(q=text)``
so callers always get a usable query.
"""

from __future__ import annotations
import asyncio
import json
import os
import re
from datetime import UTC, datetime

import anthropic
from pydantic import ValidationError

from ypl.agent_harness_service.search.search_types import SearchQuery, SearchType
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_HAIKU_TIMEOUT_S = 2.0
_MAX_TOKENS = 256

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""You are a search query parser for the Yupp Agent Harness Service (AHS).
Your job: convert a natural-language search string into a JSON object that matches the SearchQuery schema.

## SearchQuery schema

```json
{{
  "q":               "<string>  REQUIRED — main free-text search term",
  "types":           ["<SearchType>", ...] | null,
  "agent":           "<string>" | null,
  "creator_user_id": "<string>" | null,
  "status":          "<string>" | null,
  "date_from":       "<ISO-8601 datetime>" | null,
  "date_to":         "<ISO-8601 datetime>" | null,
  "limit_per_type":  <int 1-50, default 10>
}}
```

## SearchType values
{", ".join(t.value for t in SearchType)}

## Valid status values per type
- sessions:         ACTIVE, COMPLETED, STALE
- session_messages: (no status filter)
- projects:         ACTIVE, COMPLETED, CANCELLED, FAILED
- tasks:            PENDING, BLOCKED, READY, IN_PROGRESS, IN_REVIEW, COMPLETED, FAILED, CANCELLED
- schedules:        active, paused, cancelled
- artifact_pr:      open, closed, merged
- artifact_doc:     (no status filter)

## Rules
1. Always set "q" to the core search term(s) — strip meta-filters like "for agent X" or "since yesterday".
2. Only include fields you can infer with high confidence from the input.
3. Set "types" only when the input clearly refers to specific entity types; otherwise leave null.
4. For relative dates (e.g. "last week"), compute ISO-8601 dates relative to today.
5. Output ONLY valid JSON — no markdown fences, no explanation, no extra keys.
"""

# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


async def text_to_iql(text: str) -> SearchQuery:
    """Translate *text* into a structured :class:`SearchQuery` using Claude Haiku.

    On any LLM failure (network timeout, invalid JSON/schema) the function
    returns a bare ``SearchQuery(q=text)`` so callers always receive a usable
    query.

    Args:
        text: Raw user search input (must be non-empty and non-whitespace).

    Returns:
        A validated :class:`SearchQuery`.

    Raises:
        ValueError: If *text* is empty or whitespace-only.
    """
    if not text or not text.strip():
        raise ValueError("text_to_iql: search text cannot be empty or whitespace-only")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("iql_translator: ANTHROPIC_API_KEY not set, using fallback")
        return SearchQuery(q=text)

    try:
        raw_json = await asyncio.wait_for(
            _call_haiku(text, api_key),
            timeout=_HAIKU_TIMEOUT_S,
        )
        # Strip markdown fences (```json ... ```) in case the model wraps its output
        # despite explicit instructions not to.
        clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_json.strip(), flags=re.DOTALL)
        return SearchQuery.model_validate_json(clean_json)
    except TimeoutError:
        logger.warning("iql_translator: Haiku timed out after %.1fs, using fallback", _HAIKU_TIMEOUT_S)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("iql_translator: invalid response from Haiku (%s), using fallback", exc)
    except Exception as exc:
        logger.warning("iql_translator: Haiku call failed (%s), using fallback", exc)

    return SearchQuery(q=text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _call_haiku(text: str, api_key: str) -> str:
    """Call Haiku and return the raw response text."""
    # Inject today's date at call time so relative date expressions ("last week",
    # "yesterday") are resolved correctly.  The module-level _SYSTEM_PROMPT cannot
    # carry this because it is built once at import time.
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    system_with_date = f"Today's date (UTC): {today_utc}\n\n{_SYSTEM_PROMPT}"
    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_with_date,
        messages=[{"role": "user", "content": text}],
    )
    first_block = response.content[0] if response.content else None
    if not first_block or not hasattr(first_block, "text"):
        raise ValueError("Haiku returned no text content")
    return first_block.text.strip()
