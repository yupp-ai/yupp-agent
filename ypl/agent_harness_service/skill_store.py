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


def parse_skill_frontmatter(content: str) -> SkillFrontmatter:
    """Extract YAML frontmatter from a skill markdown body.

    Recognises the standard ``---``-delimited block at the top of the file
    (the same shape used by on-disk ``SKILL.md`` files). Missing or
    malformed frontmatter is treated as "no frontmatter" — callers should
    still be able to save the skill, the catalog row just won't carry
    structured metadata.
    """
    if not content.startswith("---"):
        return SkillFrontmatter(name=None, description=None, trigger_keywords=[], raw={})

    end = content.find("---", 3)
    if end == -1:
        return SkillFrontmatter(name=None, description=None, trigger_keywords=[], raw={})

    block = content[3:end]
    raw: dict[str, Any] = {}
    # Intentionally not pulling in PyYAML for a 3-key shape. The disk
    # catalog builder uses the same line-by-line parser; staying compatible
    # avoids divergent behaviour between disk and DB skills.
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw[key.strip()] = value.strip().strip("\"'")

    name = raw.get("name") or None
    description = raw.get("description") or None
    trigger_keywords = _parse_trigger_keywords(raw.get("trigger_keywords"))
    return SkillFrontmatter(
        name=name,
        description=description,
        trigger_keywords=trigger_keywords,
        raw=raw,
    )


def _parse_trigger_keywords(value: Any) -> list[str]:
    """Normalise the ``trigger_keywords`` field into a flat list of strings.

    Accepts the two shapes operators tend to write by hand:

    * ``trigger_keywords: foo, bar, baz`` (single line, comma-separated)
    * ``trigger_keywords: [foo, bar, baz]`` (single line, JSON-ish array)

    Multi-line YAML arrays would require a real parser; we skip them rather
    than misinterpret. Anything beyond :data:`_MAX_TRIGGER_KEYWORDS` is
    truncated.
    """
    if value is None or value == "":
        return []
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    parts = [p.strip().strip("\"'") for p in raw.split(",")]
    keywords = [p for p in parts if p]
    return keywords[:_MAX_TRIGGER_KEYWORDS]


def strip_frontmatter(content: str) -> str:
    """Return ``content`` with a leading ``---`` block removed, if present."""
    if not content.startswith("---"):
        return content
    end = content.find("---", 3)
    if end == -1:
        return content
    return content[end + 3 :].lstrip("\n")


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
