"""Parsing utilities for yupp-agent.

Slim version — only includes functions used by AHS/SAG/MCP services.
"""

import re
from datetime import UTC, datetime


def parse_rfc3339_timestamp(value: str) -> datetime | None:
    """Parse RFC3339-ish timestamp strings into UTC datetimes.

    Supports `Z` suffix and fractional seconds beyond microseconds (truncated).
    """
    if not value:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    # `datetime.fromisoformat()` supports up to microseconds; truncate nanoseconds if present.
    if "." in normalized:
        prefix, rest = normalized.split(".", 1)
        match = re.match(r"^(?P<digits>\d+)(?P<suffix>.*)$", rest)
        if match:
            digits = match.group("digits")
            suffix = match.group("suffix")
            microseconds = (digits + "000000")[:6]
            normalized = f"{prefix}.{microseconds}{suffix}"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)
