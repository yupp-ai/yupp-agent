"""Helpers for SKILL artifacts.

SKILL artifacts share the scoped-inline storage shape with MEMORY artifacts
(``inline_content`` + ``memory_scope`` / ``memory_scope_subject``), with one
addition: the markdown body usually starts with YAML frontmatter that
declares the skill's ``name`` / ``description`` / optional
``trigger_keywords``. We persist a parsed copy of that frontmatter into
``artifact_metadata->'skill'`` so the system-prompt catalog builder can
list available skills without reading the body of every row.

The helpers here are intentionally tiny and synchronous so they can be
re-used from the catalog merger (which runs at session start) and from
the ``save_skill`` MCP tool (which runs in an async context).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any

# Maximum number of trigger keywords we'll persist on a single skill — guards
# against runaway frontmatter from imported / generated content.
_MAX_TRIGGER_KEYWORDS = 32


@dataclass(frozen=True)
class SkillFrontmatter:
    """Parsed view of a SKILL artifact's leading YAML block.

    Only the keys we currently understand are surfaced. Anything else in the
    frontmatter is round-tripped through ``raw`` so we don't silently drop
    fields a future skill format might rely on.
    """

    name: str | None
    description: str | None
    trigger_keywords: list[str]
    raw: dict[str, Any]


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    """Split a leading ``---``-delimited YAML block from the body.

    The opening and closing delimiters must each be a line whose only
    content is ``---`` (trailing whitespace ignored). Returns
    ``(block, body)`` where ``block`` is the text between the delimiters;
    when there is no well-formed frontmatter, returns ``(None, content)``.

    Requiring the closing ``---`` to be on a line by itself (rather than the
    old ``content.find("---", 3)`` substring scan) avoids matching a ``---``
    embedded mid-line or inside the body. One genuinely ambiguous shape
    remains — a body that opens with a markdown horizontal rule directly
    after a single pseudo-frontmatter line — which no line-based parser can
    disambiguate without a real YAML reader; that is an accepted edge.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None, content
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    return None, content


def _strip_matched_quotes(value: str) -> str:
    """Strip exactly one matched pair of surrounding quotes, if present.

    Unlike ``str.strip("\\"'")`` (which strips *every* leading/trailing quote
    and so mangles values like ``'"x"'`` into ``x``), this removes only a
    single matched ``"..."`` or ``'...'`` pair.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _as_optional_str(value: Any) -> str | None:
    """Return a non-empty string value, else ``None`` (ignoring list values)."""
    if isinstance(value, str) and value:
        return value
    return None


def parse_skill_frontmatter(content: str) -> SkillFrontmatter:
    """Extract YAML frontmatter from a skill markdown body.

    Recognises the standard ``---``-delimited block at the top of the file
    (the same shape used by on-disk ``SKILL.md`` files). Missing or
    malformed frontmatter is treated as "no frontmatter" — callers should
    still be able to save the skill, the catalog row just won't carry
    structured metadata.

    Intentionally not pulling in PyYAML for this small shape; the parser
    handles the by-hand shapes operators actually write: ``key: value``
    (with matched-quote stripping) and multi-line ``key:`` followed by
    indented ``- item`` lists.
    """
    block, _ = _split_frontmatter(content)
    if block is None:
        return SkillFrontmatter(name=None, description=None, trigger_keywords=[], raw={})

    raw: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            # Empty inline value → possibly a multi-line YAML list. Collect any
            # following indented ``- item`` lines. Without this the idiomatic
            # block-list shape silently parsed to an empty value and dropped
            # every item (a silent-data-loss bug for ad-hoc DB skills).
            items: list[str] = []
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped == "-":
                    items.append("")
                elif stripped.startswith("- "):
                    items.append(_strip_matched_quotes(stripped[2:].strip()))
                else:
                    break
                i += 1
            raw[key] = items if items else ""
        else:
            raw[key] = _strip_matched_quotes(value)

    name = _as_optional_str(raw.get("name"))
    description = _as_optional_str(raw.get("description"))
    trigger_keywords = _parse_trigger_keywords(raw.get("trigger_keywords"))
    return SkillFrontmatter(
        name=name,
        description=description,
        trigger_keywords=trigger_keywords,
        raw=raw,
    )


def _parse_trigger_keywords(value: Any) -> list[str]:
    """Normalise the ``trigger_keywords`` field into a flat list of strings.

    Accepts the shapes operators write by hand:

    * ``trigger_keywords: foo, bar, baz`` (single line, comma-separated)
    * ``trigger_keywords: [foo, bar, baz]`` (single line, JSON-ish array)
    * a multi-line block list (``- foo`` / ``- bar`` on indented lines),
      which :func:`parse_skill_frontmatter` pre-collects into a ``list``.

    Anything beyond :data:`_MAX_TRIGGER_KEYWORDS` is truncated.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        keywords = [k for k in (_strip_matched_quotes(str(v).strip()) for v in value) if k]
        return keywords[:_MAX_TRIGGER_KEYWORDS]
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    parts = [_strip_matched_quotes(p.strip()) for p in raw.split(",")]
    keywords = [p for p in parts if p]
    return keywords[:_MAX_TRIGGER_KEYWORDS]


def strip_frontmatter(content: str) -> str:
    """Return ``content`` with a leading ``---`` block removed, if present."""
    block, body = _split_frontmatter(content)
    if block is None:
        return content
    return body.lstrip("\n")


def build_skill_metadata(frontmatter: SkillFrontmatter, *, override_name: str) -> dict[str, Any]:
    """Build the JSONB payload stored under ``artifact_metadata['skill']``.

    ``override_name`` wins over any ``name:`` value parsed from the
    frontmatter — the MCP tool's ``name`` argument is the source of truth
    for what slug / catalog entry the skill is registered under.
    """
    return {
        "skill": {
            "name": override_name,
            "description": frontmatter.description,
            "trigger_keywords": frontmatter.trigger_keywords,
            "frontmatter": frontmatter.raw,
        }
    }
