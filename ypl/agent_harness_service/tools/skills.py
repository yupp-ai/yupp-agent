"""Skill loading tool for the harness MCP server.

Provides the load_skill tool that fetches full skill documentation by name,
allowing agents to access domain-specific guidance on demand.
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
        "descriptions — use this tool to load the full content when needed."
    ),
)
def load_skill(skill_name: str) -> str:
    """Load and return a skill's SKILL.md content.

    Args:
        skill_name: Name of the skill to load (e.g., 'workspace-guide',
            'memory-guide', 'review-pr'). Must match a directory name
            under the skills directory.

    Returns:
        The full skill content with YAML frontmatter stripped.
        On error, returns a descriptive error message.
    """
    logger.info("MCP tool: load_skill", skill_name=skill_name)

    from ypl.agent_harness_service.common.constants import AHS_SKILLS_DIR

    # Validate skill_name to prevent path traversal
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        return f"[ERROR] Invalid skill name: {skill_name!r}"

    skill_path = os.path.join(AHS_SKILLS_DIR, skill_name, "SKILL.md")
    if not os.path.isfile(skill_path):
        # List available skills to help the agent
        available: list[str] = []
        if os.path.isdir(AHS_SKILLS_DIR):
            available = sorted(
                d for d in os.listdir(AHS_SKILLS_DIR) if os.path.isfile(os.path.join(AHS_SKILLS_DIR, d, "SKILL.md"))
            )
        return f"[ERROR] Skill '{skill_name}' not found. Available skills: {', '.join(available)}"

    try:
        with open(skill_path) as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] Failed to read skill '{skill_name}': {e}"

    # Strip YAML frontmatter (--- ... ---)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3 :].lstrip("\n")

    return content
