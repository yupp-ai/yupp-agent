"""Linear project management tools for the harness MCP server.

Provides tools for agents to interact with Linear: list teams, query workflow
states, fetch/search/create/update issues.
"""

from __future__ import annotations
from typing import Any

from ypl.agent_harness_service.tools.mcp_instance import mcp
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_linear_client() -> Any:
    """Create and return a LinearClient. Raises on missing API key."""
    from ypl.backend.utils.linear import LinearClient

    return LinearClient()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="linear_list_teams",
    description=(
        "List all Linear teams (projects) available in the workspace. "
        "Returns each team's ID, name, and key. Use team IDs when creating "
        "or filtering issues with other Linear tools."
    ),
)
def linear_list_teams() -> dict[str, Any]:
    """List all Linear teams.

    Returns:
        Dict with 'teams' list, each containing id, name, key, description.
        On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_list_teams")
    try:
        client = _get_linear_client()
        response = client.get_teams()
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("teams", {}).get("nodes", [])
        return {"teams": nodes}
    except Exception as e:
        logger.error("linear_list_teams failed", exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_get_workflow_states",
    description=(
        "Get the workflow states (statuses) for a Linear team. "
        "Returns state IDs, names, types (e.g., 'backlog', 'started', 'completed'), "
        "and colors. Use state IDs when creating or updating issues."
    ),
)
def linear_get_workflow_states(team_id: str) -> dict[str, Any]:
    """Get workflow states for a Linear team.

    Args:
        team_id: The Linear team ID (get from linear_list_teams).

    Returns:
        Dict with 'states' list, each containing id, name, type, color, position.
        On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_get_workflow_states", team_id=team_id)
    try:
        client = _get_linear_client()
        response = client.get_workflow_states(team_id)
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("workflowStates", {}).get("nodes", [])
        # Sort by position for predictable ordering
        nodes_sorted = sorted(nodes, key=lambda x: x.get("position", 0))
        return {"states": nodes_sorted}
    except Exception as e:
        logger.error("linear_get_workflow_states failed", team_id=team_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_get_issue",
    description=(
        "Fetch a single Linear issue by its ID or identifier (e.g., 'ENG-123'). "
        "Returns full issue details including title, description, state, assignee, "
        "priority, and URLs."
    ),
)
def linear_get_issue(issue_id: str) -> dict[str, Any]:
    """Fetch a single Linear issue.

    Args:
        issue_id: Issue ID (UUID) or identifier (e.g., 'ENG-123').

    Returns:
        Dict with issue fields on success. On error, returns {'error': '<message>'}.
    """
    logger.info("MCP tool: linear_get_issue", issue_id=issue_id)
    try:
        client = _get_linear_client()
        response = client.get_issue(issue_id)
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        issue = (response.get("data") or {}).get("issue")
        if issue is None:
            return {"error": "Issue not found"}
        return {"issue": issue}
    except Exception as e:
        logger.error("linear_get_issue failed", issue_id=issue_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_search_issues",
    description=(
        "Search Linear issues with optional filters. Can filter by team, state, "
        "assignee, and/or a text query (matches title and description). "
        "Returns matching issues sorted by most recently updated."
    ),
)
def linear_search_issues(
    query: str | None = None,
    team_id: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search Linear issues.

    Args:
        query: Optional text to search in issue titles and descriptions.
        team_id: Optional team ID to filter by.
        state_id: Optional workflow state ID to filter by.
        assignee_id: Optional user ID to filter by assignee.
        limit: Maximum number of results to return (default 20, max 50).

    Returns:
        Dict with 'issues' list on success. On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_search_issues",
        query=query,
        team_id=team_id,
        state_id=state_id,
        assignee_id=assignee_id,
        limit=limit,
    )
    try:
        client = _get_linear_client()
        response = client.search_issues(
            query_term=query,
            team_id=team_id,
            state_id=state_id,
            assignee_id=assignee_id,
            limit=min(limit, 50),
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Unknown error")}
        nodes = response.get("data", {}).get("issues", {}).get("nodes", [])
        return {"issues": nodes, "count": len(nodes)}
    except Exception as e:
        logger.error("linear_search_issues failed", exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_create_issue",
    description=(
        "Create a new issue in Linear. Requires a title and team ID (get from "
        "linear_list_teams). Optionally specify a workflow state (get from "
        "linear_get_workflow_states), assignee, description, and priority. "
        "Priority values: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low."
    ),
)
def linear_create_issue(
    title: str,
    team_id: str,
    description: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    """Create a new Linear issue.

    Args:
        title: Issue title (required).
        team_id: Team ID to create the issue in (required). Use linear_list_teams.
        description: Optional issue description (supports markdown).
        state_id: Optional workflow state ID. Defaults to team's default state.
        assignee_id: Optional user ID to assign the issue to.
        priority: Optional priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).

    Returns:
        Dict with 'issue' (id, identifier, title, url, state, team) on success.
        On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_create_issue",
        title=title,
        team_id=team_id,
        has_description=description is not None,
        state_id=state_id,
        assignee_id=assignee_id,
        priority=priority,
    )
    try:
        client = _get_linear_client()
        response = client.create_issue(
            title=title,
            team_id=team_id,
            description=description,
            state_id=state_id,
            assignee_id=assignee_id,
            priority=priority,
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Create failed")}
        result = (response.get("data") or {}).get("issueCreate", {})
        if not result.get("success"):
            return {"error": "Create failed"}
        return {"issue": result.get("issue"), "success": True}
    except Exception as e:
        logger.error("linear_create_issue failed", title=title, team_id=team_id, exc_info=True)
        return {"error": str(e)}


@mcp.tool(
    name="linear_update_issue",
    description=(
        "Update an existing Linear issue. Specify the issue ID or identifier "
        "(e.g., 'ENG-123') and any fields to change: title, description, state, "
        "assignee, or priority. Only provided fields are updated. "
        "Priority values: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low."
    ),
)
def linear_update_issue(
    issue_id: str,
    title: str | None = None,
    description: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    """Update an existing Linear issue.

    Args:
        issue_id: Issue ID (UUID) or identifier (e.g., 'ENG-123').
        title: New title for the issue.
        description: New description (supports markdown).
        state_id: New workflow state ID (use linear_get_workflow_states).
        assignee_id: New assignee user ID. Pass empty string to unassign.
        priority: New priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).

    Returns:
        Dict with 'issue' (updated fields) on success.
        On error, returns {'error': '<message>'}.
    """
    logger.info(
        "MCP tool: linear_update_issue",
        issue_id=issue_id,
        title=title,
        state_id=state_id,
        assignee_id=assignee_id,
        priority=priority,
    )
    if not any(v is not None for v in (title, description, state_id, assignee_id, priority)):
        return {"error": "At least one field must be provided to update"}
    try:
        client = _get_linear_client()
        response = client.update_issue(
            issue_id=issue_id,
            title=title,
            description=description,
            state_id=state_id,
            assignee_id=assignee_id,
            priority=priority,
        )
        # Check for GraphQL-level errors before extracting data
        errors = response.get("errors")
        if errors:
            return {"error": errors[0].get("message", "Update failed")}
        result = (response.get("data") or {}).get("issueUpdate", {})
        if not result.get("success"):
            return {"error": "Update failed"}
        return {"issue": result.get("issue"), "success": True}
    except Exception as e:
        logger.error("linear_update_issue failed", issue_id=issue_id, exc_info=True)
        return {"error": str(e)}
