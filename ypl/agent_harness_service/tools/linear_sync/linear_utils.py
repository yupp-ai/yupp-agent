"""Utilities for resolving Linear teams and projects by name, key, or slug.

Provides helper functions to look up Linear UUIDs from human-friendly identifiers,
making it easier to use Linear MCP tools without needing to know UUIDs upfront.

Note: These functions are synchronous since LinearClient uses the requests library.
Async callers should use asyncio.to_thread() to avoid blocking the event loop.
"""

from __future__ import annotations
import re

from ypl.backend.utils.linear import LinearClient
from ypl.structured_logger import get_logger

logger = get_logger()

# UUID regex pattern
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def is_uuid(value: str) -> bool:
    """Check if a string is a valid UUID."""
    return bool(UUID_PATTERN.match(value))


def list_teams(client: LinearClient | None = None) -> list[dict[str, str]]:
    """List all Linear teams accessible to the authenticated user.

    Args:
        client: Optional LinearClient instance. If not provided, a new one is created.

    Returns:
        List of team dicts with 'id', 'name', 'key', and 'description' fields.
    """
    if client is None:
        client = LinearClient()

    resp = client.get_teams()
    teams = resp.get("data", {}).get("teams", {}).get("nodes", [])
    return [
        {
            "id": team.get("id", ""),
            "name": team.get("name", ""),
            "key": team.get("key", ""),
            "description": team.get("description", ""),
        }
        for team in teams
    ]


def list_projects(client: LinearClient | None = None, limit: int = 50) -> list[dict[str, str]]:
    """List Linear projects accessible to the authenticated user.

    Args:
        client: Optional LinearClient instance. If not provided, a new one is created.
        limit: Maximum number of projects to return (default 50).

    Returns:
        List of project dicts with 'id', 'name', and 'slug_id' fields.
    """
    if client is None:
        client = LinearClient()

    query = """
        query ListProjects($first: Int!) {
            projects(first: $first) {
                nodes {
                    id
                    name
                    slugId
                }
            }
        }
    """
    resp = client.post({"query": query, "variables": {"first": limit}})
    projects = resp.get("data", {}).get("projects", {}).get("nodes", [])
    return [
        {
            "id": proj.get("id", ""),
            "name": proj.get("name", ""),
            "slug_id": proj.get("slugId", ""),
        }
        for proj in projects
    ]


def resolve_team_id(team_input: str, client: LinearClient | None = None) -> str | None:
    """Resolve a team name or key to its UUID.

    If the input is already a valid UUID, it is returned as-is.
    Otherwise, searches for a team by key (case-insensitive) first,
    then by name (case-insensitive).

    Args:
        team_input: Team UUID, key (e.g., 'YUP'), or name (e.g., 'Yupp AI').
        client: Optional LinearClient instance.

    Returns:
        The team UUID if found, or None if no match.
    """
    if is_uuid(team_input):
        return team_input

    if client is None:
        client = LinearClient()

    teams = list_teams(client)
    team_input_lower = team_input.lower()

    # First try exact match on key
    for team in teams:
        if team.get("key", "").lower() == team_input_lower:
            logger.debug(
                "Resolved Linear team by key",
                input=team_input,
                team_id=team["id"],
                team_name=team["name"],
            )
            return team["id"]

    # Then try exact match on name
    for team in teams:
        if team.get("name", "").lower() == team_input_lower:
            logger.debug(
                "Resolved Linear team by name",
                input=team_input,
                team_id=team["id"],
                team_name=team["name"],
            )
            return team["id"]

    logger.warning("Could not resolve Linear team", input=team_input)
    return None


def resolve_project_id(project_input: str, client: LinearClient | None = None) -> str | None:
    """Resolve a project name or slug to its UUID.

    If the input is already a valid UUID, it is returned as-is.
    Otherwise, searches for a project by exact name match first,
    then by slug, then returns the single result if only one matches.

    Args:
        project_input: Project UUID, name (e.g., 'Litweezer'), or slug ID.
        client: Optional LinearClient instance.

    Returns:
        The project UUID if found, or None if no match or multiple ambiguous matches.
    """
    if is_uuid(project_input):
        return project_input

    if client is None:
        client = LinearClient()

    # Search projects by name - try exact match first to avoid noisy partial matches
    query = """
        query SearchProjects($filter: ProjectFilter) {
            projects(first: 100, filter: $filter) {
                nodes {
                    id
                    name
                    slugId
                }
            }
        }
    """
    # First try exact name match (case-insensitive)
    resp = client.post(
        {
            "query": query,
            "variables": {"filter": {"name": {"eqIgnoreCase": project_input}}},
        }
    )
    projects = resp.get("data", {}).get("projects", {}).get("nodes", [])

    # If no exact match, fall back to contains search
    if not projects:
        resp = client.post(
            {
                "query": query,
                "variables": {"filter": {"name": {"containsIgnoreCase": project_input}}},
            }
        )
        projects = resp.get("data", {}).get("projects", {}).get("nodes", [])

    project_input_lower = project_input.lower()

    # First try exact match on name
    for proj in projects:
        if proj.get("name", "").lower() == project_input_lower:
            project_id: str = proj["id"]
            logger.debug(
                "Resolved Linear project by name",
                input=project_input,
                project_id=project_id,
                project_name=proj["name"],
            )
            return project_id

    # Then try match on slug from name-search results
    for proj in projects:
        if proj.get("slugId", "").lower() == project_input_lower:
            project_id = proj["id"]
            logger.debug(
                "Resolved Linear project by slug",
                input=project_input,
                project_id=project_id,
                project_name=proj["name"],
            )
            return project_id

    # Try searching by slugId directly before falling back to single-match heuristic.
    # This ensures that if input is a valid slug, we find the correct project even when
    # containsIgnoreCase returned an unrelated project whose name happens to contain the input.
    resp = client.post(
        {
            "query": query,
            "variables": {"filter": {"slugId": {"eqIgnoreCase": project_input}}},
        }
    )
    slug_projects = resp.get("data", {}).get("projects", {}).get("nodes", [])
    if len(slug_projects) == 1:
        proj = slug_projects[0]
        project_id = proj["id"]
        logger.debug(
            "Resolved Linear project by slug (direct search)",
            input=project_input,
            project_id=project_id,
            project_name=proj["name"],
        )
        return project_id

    # If only one result from name search and no direct slug match, use it as last resort
    if len(projects) == 1:
        proj = projects[0]
        project_id = proj["id"]
        logger.debug(
            "Resolved Linear project (single match)",
            input=project_input,
            project_id=project_id,
            project_name=proj["name"],
        )
        return project_id

    # Multiple or no matches
    if projects:
        logger.warning(
            "Multiple Linear projects match input",
            input=project_input,
            matches=[{"id": p["id"], "name": p["name"]} for p in projects],
        )
    else:
        logger.warning("No Linear projects match input", input=project_input)

    return None
