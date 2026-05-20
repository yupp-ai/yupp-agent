"""Skill loading tool for the harness MCP server.

Provides the ``load_skill`` tool that fetches full skill documentation by
name. Skills resolve in priority order:

1. **Disk** — ``.agents/skills/<name>/SKILL.md`` shipped with the repo.
   Curated and version-controlled; wins on name collision.
2. **DB (SKILL artifacts)** — ad-hoc skills saved via the ``save_skill``
   MCP tool. Scoped (agent / user / topic) so agents can ship reusable
   procedures without a code deploy. Within the DB path the lookup order
   is agent > user > topic.

The DB fallback turns the on-disk skills directory into a curated baseline
and lets ``save_skill``-authored content extend the catalog at runtime.
"""

from __future__ import annotations
import os

from ypl.agent_harness_service.tools.mcp_instance import mcp
from ypl.structured_logger import get_logger

logger = get_logger()


@mcp.tool(
    name="load_skill",
    description=(
        "Load a skill's full documentation by name. Skills provide domain-specific "
        "guidance for common tasks (e.g. workspace workflows, memory management, "
        "debugging patterns). The system prompt lists available skills with short "
        "descriptions — use this tool to load the full content when needed. "
        "Resolution priority: on-disk repo skills first, then SKILL artifacts "
        "saved via save_skill (agent scope > user > topic)."
    ),
)
async def load_skill(skill_name: str) -> str:
    """Load and return a skill's SKILL.md content.

    Args:
        skill_name: Name of the skill to load (e.g., 'workspace-guide',
            'memory-guide', 'fetch-from-db'). Must match a directory name
            under the skills directory OR a SKILL artifact slug visible
            to the caller.

    Returns:
        The full skill content with YAML frontmatter stripped.
        On error, returns a descriptive error message.
    """
    logger.info("MCP tool: load_skill", skill_name=skill_name)

    # Lazy imports keep ``tools/`` Layer-1 architecturally pure (see
    # AHS ARCHITECTURE.md): module-level imports from sibling Layer-1
    # packages or from wiring-layer modules would break the layering test.
    from ypl.agent_harness_service.common.constants import AHS_SKILLS_DIR
    from ypl.agent_harness_service.skill_store import strip_frontmatter

    # Validate skill_name to prevent path traversal in the disk path.
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        return f"[ERROR] Invalid skill name: {skill_name!r}"

    # 1. Disk path — curated skills shipped with the repo.
    skill_path = os.path.join(AHS_SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.isfile(skill_path):
        try:
            with open(skill_path) as f:
                content = f.read()
        except OSError as e:
            return f"[ERROR] Failed to read skill '{skill_name}': {e}"
        return strip_frontmatter(content)

    # 2. DB fallback — SKILL artifacts authored via save_skill.
    try:
        from ypl.mcp_server.tools.skill_artifacts import load_skill_artifact

        # ``shared_tool`` keeps the underlying coroutine reachable via
        # ``__wrapped__`` (set by functools.wraps). Call it directly so we
        # don't recurse through FastMCP's middleware chain from inside a tool.
        load_fn = getattr(load_skill_artifact, "__wrapped__", load_skill_artifact)
        result = await load_fn(name=skill_name)
    except Exception as e:
        logger.exception("DB fallback for load_skill failed", skill_name=skill_name)
        return f"[ERROR] Failed to read skill '{skill_name}' from DB fallback: {e}"

    if isinstance(result, dict) and result.get("success") and result.get("content"):
        return strip_frontmatter(str(result["content"]))

    # 3. Not found anywhere — list disk skills to help the agent.
    available: list[str] = []
    if os.path.isdir(AHS_SKILLS_DIR):
        available = sorted(
            d for d in os.listdir(AHS_SKILLS_DIR) if os.path.isfile(os.path.join(AHS_SKILLS_DIR, d, "SKILL.md"))
        )
    return (
        f"[ERROR] Skill '{skill_name}' not found on disk or in your visible SKILL artifacts. "
        f"Available disk skills: {', '.join(available)}"
    )
