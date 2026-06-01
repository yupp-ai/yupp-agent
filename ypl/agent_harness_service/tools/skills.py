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
    #
    # Resolve the slug directly against artifact_store rather than routing the
    # read through the mcp_server ``load_skill_artifact`` FunctionTool. The work
    # is just "scope-resolved slug lookup + read body", both already AHS APIs,
    # so inlining keeps ``tools/`` free of an AHS→mcp_server import edge (the
    # caller context comes from the Layer-0 ``mcp_common.auth_context``). The
    # implicit scope order — agent > user > topic — mirrors save_skill /
    # load_skill_artifact so per-agent skills shadow shared ones.
    try:
        from ypl.agent_harness_service.artifact_store import get_artifact_by_slug, read_artifact_content
        from ypl.db.agent_harness import AgentArtifactType
        from ypl.mcp_common.auth_context import current_request_context

        ctx = current_request_context()
        user_id = (ctx.requesting_user_id if ctx else None) or None
        agent_name = (ctx.ahs_agent_name if ctx else None) or None

        # Caller visibility, highest priority first.
        scopes_to_try: list[tuple[str, str | None]] = []
        if agent_name is not None:
            scopes_to_try.append(("agent", agent_name))
        if user_id is not None:
            scopes_to_try.append(("user", user_id))
        scopes_to_try.append(("topic", None))

        artifact = None
        for candidate_scope, candidate_subject in scopes_to_try:
            artifact = await get_artifact_by_slug(
                skill_name,
                artifact_type=AgentArtifactType.SKILL,
                memory_scope=candidate_scope,
                memory_scope_subject=candidate_subject,
            )
            if artifact is not None:
                break

        if artifact is not None:
            data, _content_type = await read_artifact_content(artifact)
            return strip_frontmatter(data.decode("utf-8"))
    except Exception as e:
        logger.exception("DB fallback for load_skill failed", skill_name=skill_name)
        return f"[ERROR] Failed to read skill '{skill_name}' from DB fallback: {e}"

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
