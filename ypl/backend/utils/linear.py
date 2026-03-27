import re
from typing import Any

import pandas as pd
import requests

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

API_URL = "https://api.linear.app/graphql"

# Linear priority values
PRIORITY_NO_PRIORITY = 0
PRIORITY_URGENT = 1
PRIORITY_HIGH = 2
PRIORITY_MEDIUM = 3
PRIORITY_LOW = 4

PRIORITY_LABELS: dict[str, int] = {
    "no_priority": PRIORITY_NO_PRIORITY,
    "urgent": PRIORITY_URGENT,
    "high": PRIORITY_HIGH,
    "medium": PRIORITY_MEDIUM,
    "low": PRIORITY_LOW,
}


class LinearClient:
    def __init__(self, search_limit: int = 50):
        if not settings.LINEAR_API_KEY:
            raise ValueError("LINEAR_API_KEY is not set")

        self.headers = {"Authorization": f"{settings.LINEAR_API_KEY}"}
        self.search_limit = search_limit

    def post(self, params: dict[str, Any]) -> Any:
        response = requests.post(API_URL, headers=self.headers, json=params)

        if response.status_code != 200:
            message = response.text or f"Error: HTTP {response.status_code}"
            raise Exception(message)

        return response.json()

    def _get_issue_filter_query(self, label_id: str | None = None, assignee_id: str | None = None) -> dict[str, Any]:
        filter = {}
        if label_id:
            filter["labels"] = {"id": {"eq": label_id}}
        if assignee_id:
            filter["assignee"] = {"id": {"eq": assignee_id}}

        return filter

    def get_issues(self, label: str | None = None, assignee: str | None = None, limit: int = 20) -> dict[str, Any]:
        filter_dict = self._get_issue_filter_query(label_id=label, assignee_id=assignee)
        query = """
            query IssuesByLabel($first: Int, $after: String, $filter: IssueFilter, $orderBy: PaginationOrderBy) {
                issues(first: $first, after: $after, filter: $filter, orderBy: $orderBy) {
                    nodes {
                        id
                        title
                        description
                        url
                        updatedAt
                        createdAt
                        assignee {
                            email
                            displayName
                        }
                    }
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                }
            }
        """

        all_issues: list[dict[str, Any]] = []
        after_cursor = None

        while True and len(all_issues) < limit:
            variables = {
                "first": self.search_limit,
                "after": after_cursor,
                "filter": filter_dict,
                "orderBy": "updatedAt",
            }

            params = {
                "query": query,
                "variables": variables,
            }

            response = self.post(params)
            data = response.get("data", {})
            issues_result = data.get("issues", {})

            nodes = issues_result.get("nodes", [])
            all_issues.extend(nodes)

            page_info = issues_result.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)

            if not has_next_page:
                break

            after_cursor = page_info.get("endCursor")

        return {"data": {"issues": {"nodes": all_issues}}}

    def get_all_users(self) -> dict[str, Any]:
        """Fetch all users/assignees from Linear."""
        query = """
            query GetAllUsers($first: Int, $after: String) {
                users(first: $first, after: $after) {
                    nodes {
                        id
                        email
                        displayName
                        name
                        active
                        avatarUrl
                    }
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                }
            }
        """

        all_users = []
        after_cursor = None

        while True:
            variables = {"first": self.search_limit, "after": after_cursor}

            params = {
                "query": query,
                "variables": variables,
            }

            response = self.post(params)
            data = response.get("data", {})
            users_result = data.get("users", {})

            nodes = users_result.get("nodes", [])
            all_users.extend(nodes)

            page_info = users_result.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)

            if not has_next_page:
                break

            after_cursor = page_info.get("endCursor")

        return {"data": {"users": {"nodes": all_users}}}

    def get_teams(self) -> dict[str, Any]:
        """Fetch all teams from Linear."""
        query = """
            query GetTeams {
                teams {
                    nodes {
                        id
                        name
                        key
                        description
                    }
                }
            }
        """
        return dict(self.post({"query": query}))

    def get_workflow_states(self, team_id: str) -> dict[str, Any]:
        """Fetch workflow states (statuses) for a team."""
        query = """
            query GetWorkflowStates($teamId: ID!) {
                workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                    nodes {
                        id
                        name
                        color
                        type
                        position
                    }
                }
            }
        """
        return dict(self.post({"query": query, "variables": {"teamId": team_id}}))

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Fetch a single issue by ID or identifier (e.g., 'ENG-123')."""
        query = """
            query GetIssue($id: String!) {
                issue(id: $id) {
                    id
                    identifier
                    title
                    description
                    url
                    priority
                    createdAt
                    updatedAt
                    state {
                        id
                        name
                        type
                    }
                    assignee {
                        id
                        email
                        displayName
                    }
                    team {
                        id
                        name
                        key
                    }
                }
            }
        """
        return dict(self.post({"query": query, "variables": {"id": issue_id}}))

    def search_issues(
        self,
        query_term: str | None = None,
        team_id: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search issues with optional filters."""
        filter_dict: dict[str, Any] = {}
        if team_id:
            filter_dict["team"] = {"id": {"eq": team_id}}
        if state_id:
            filter_dict["state"] = {"id": {"eq": state_id}}
        if assignee_id:
            filter_dict["assignee"] = {"id": {"eq": assignee_id}}
        if query_term:
            filter_dict["or"] = [
                {"title": {"containsIgnoreCase": query_term}},
                {"description": {"containsIgnoreCase": query_term}},
            ]

        query = """
            query SearchIssues($first: Int, $filter: IssueFilter, $orderBy: PaginationOrderBy) {
                issues(first: $first, filter: $filter, orderBy: $orderBy) {
                    nodes {
                        id
                        identifier
                        title
                        description
                        url
                        priority
                        createdAt
                        updatedAt
                        state {
                            id
                            name
                            type
                        }
                        assignee {
                            id
                            email
                            displayName
                        }
                        team {
                            id
                            name
                            key
                        }
                    }
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                }
            }
        """
        variables: dict[str, Any] = {
            "first": min(limit, self.search_limit),
            "filter": filter_dict,
            "orderBy": "updatedAt",
        }
        return dict(self.post({"query": query, "variables": variables}))

    def create_issue(
        self,
        title: str,
        team_id: str,
        description: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        priority: int | None = None,
        project_id: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Linear issue.

        Args:
            title: Issue title (required).
            team_id: ID of the team to create the issue in (required).
            description: Optional issue description (supports markdown).
            state_id: Optional workflow state ID. Defaults to the team's default state.
            assignee_id: Optional user ID to assign the issue to.
            priority: Optional priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).
            project_id: Optional Linear project UUID to assign the issue to.
            parent_id: Optional parent issue UUID for subtask hierarchy.

        Returns:
            GraphQL response with issueCreate.success and issueCreate.issue.
        """
        mutation = """
            mutation CreateIssue($input: IssueCreateInput!) {
                issueCreate(input: $input) {
                    success
                    issue {
                        id
                        identifier
                        title
                        url
                        state {
                            id
                            name
                        }
                        team {
                            id
                            name
                            key
                        }
                    }
                }
            }
        """
        issue_input: dict[str, Any] = {"title": title, "teamId": team_id}
        if description is not None:
            issue_input["description"] = description
        if state_id is not None:
            issue_input["stateId"] = state_id
        if assignee_id is not None:
            issue_input["assigneeId"] = assignee_id
        if priority is not None:
            issue_input["priority"] = priority
        if project_id is not None:
            issue_input["projectId"] = project_id
        if parent_id is not None:
            issue_input["parentId"] = parent_id

        return dict(self.post({"query": mutation, "variables": {"input": issue_input}}))

    def update_issue(
        self,
        issue_id: str,
        title: str | None = None,
        description: str | None = None,
        state_id: str | None = None,
        assignee_id: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing Linear issue.

        Args:
            issue_id: Issue ID or identifier (e.g., 'abc123' or 'ENG-123').
            title: New title for the issue.
            description: New description for the issue (supports markdown).
            state_id: New workflow state ID.
            assignee_id: New assignee user ID. Pass empty string to unassign.
            priority: New priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low).

        Returns:
            GraphQL response with issueUpdate.success and issueUpdate.issue.
        """
        mutation = """
            mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
                issueUpdate(id: $id, input: $input) {
                    success
                    issue {
                        id
                        identifier
                        title
                        url
                        state {
                            id
                            name
                        }
                        assignee {
                            id
                            email
                            displayName
                        }
                        team {
                            id
                            name
                            key
                        }
                    }
                }
            }
        """
        issue_input: dict[str, Any] = {}
        if title is not None:
            issue_input["title"] = title
        if description is not None:
            issue_input["description"] = description
        if state_id is not None:
            issue_input["stateId"] = state_id
        if assignee_id is not None:
            # Empty string means "unassign" - convert to None for Linear API
            issue_input["assigneeId"] = assignee_id or None
        if priority is not None:
            issue_input["priority"] = priority

        if not issue_input:
            raise ValueError("At least one field must be provided to update")

        return dict(self.post({"query": mutation, "variables": {"id": issue_id, "input": issue_input}}))

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a Linear project's metadata by ID.

        Args:
            project_id: Linear project UUID.

        Returns:
            GraphQL response with ``data.project`` containing id, name,
            description, and state.
        """
        query = """
            query GetProject($id: String!) {
                project(id: $id) {
                    id
                    name
                    description
                    state
                }
            }
        """
        return dict(self.post({"query": query, "variables": {"id": project_id}}))

    def list_project_issues(
        self,
        project_id: str,
        include_completed: bool = False,
        updated_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all Linear issues for a project with parent and blocker relations.

        Handles pagination automatically.  The returned issue dicts include
        ``parent`` and ``relations`` fields needed to reconstruct the AHS
        dependency graph during import.

        Args:
            project_id: Linear project UUID.
            include_completed: If True, include completed/cancelled issues.
            updated_after: Optional ISO 8601 datetime string.  If given, only
                issues updated after this timestamp are returned.

        Returns:
            Flat list of issue dicts, each containing id, identifier, title,
            description, state, priority, parent, updatedAt, and relations.
        """
        issue_filter: dict[str, Any] = {
            "project": {"id": {"eq": project_id}},
        }
        if not include_completed:
            issue_filter["state"] = {"type": {"nin": ["completed", "cancelled"]}}
        if updated_after is not None:
            issue_filter["updatedAt"] = {"gte": updated_after}

        query = """
            query ListProjectIssues($filter: IssueFilter, $after: String) {
                issues(first: 100, after: $after, filter: $filter, orderBy: createdAt) {
                    nodes {
                        id
                        identifier
                        title
                        description
                        state { id name type }
                        priority
                        parent { id }
                        updatedAt
                        relations {
                            nodes {
                                type
                                relatedIssue { id }
                            }
                        }
                    }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """

        all_issues: list[dict[str, Any]] = []
        after_cursor: str | None = None

        while True:
            variables: dict[str, Any] = {"filter": issue_filter, "after": after_cursor}
            response = self.post({"query": query, "variables": variables})

            # Check for GraphQL-level errors (can occur with HTTP 200)
            errors = response.get("errors")
            if errors:
                raise RuntimeError(f"list_project_issues failed: {errors[0].get('message', str(errors))}")

            data = response.get("data", {})
            issues_data = data.get("issues", {})
            if issues_data is None:
                raise RuntimeError(f"list_project_issues returned no issues data. Response: {response}")

            nodes = issues_data.get("nodes", [])
            all_issues.extend(nodes)

            page_info = issues_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after_cursor = page_info.get("endCursor")

        return all_issues

    def create_project(
        self,
        name: str,
        team_ids: list[str],
    ) -> dict[str, Any]:
        """Create a new Linear project.

        Args:
            name: Display name for the project (required).
            team_ids: IDs of the Linear teams to associate with the project.

        Returns:
            GraphQL response with ``projectCreate.success`` and
            ``projectCreate.project`` (id, name).
        """
        mutation = """
            mutation CreateProject($input: ProjectCreateInput!) {
                projectCreate(input: $input) {
                    success
                    project {
                        id
                        name
                    }
                }
            }
        """
        project_input: dict[str, Any] = {"name": name, "teamIds": team_ids}
        return dict(self.post({"query": mutation, "variables": {"input": project_input}}))

    def set_issue_blocked_by(self, issue_id: str, blocked_by_ids: list[str]) -> None:
        """Create ``blocked`` relations between an issue and its blockers.

        Calls ``issueRelationCreate`` once per blocker.  Errors are logged but
        do not raise — a partial set of relations is better than aborting the
        whole export.

        Args:
            issue_id: UUID of the issue that is being blocked.
            blocked_by_ids: UUIDs of the issues that block ``issue_id``.
        """
        mutation = """
            mutation CreateIssueRelation($input: IssueRelationCreateInput!) {
                issueRelationCreate(input: $input) {
                    success
                }
            }
        """
        for blocker_id in blocked_by_ids:
            try:
                response = self.post(
                    {
                        "query": mutation,
                        "variables": {
                            "input": {
                                "issueId": issue_id,
                                "relatedIssueId": blocker_id,
                                "type": "blocked",
                            }
                        },
                    }
                )
                # Check for GraphQL-level errors
                errors = response.get("errors")
                if errors:
                    logger.warning(
                        "GraphQL error creating issue relation",
                        issue_id=issue_id,
                        blocker_id=blocker_id,
                        error=errors[0].get("message", str(errors)),
                    )
                    continue
                # Check for success=False
                create_result = (response.get("data") or {}).get("issueRelationCreate", {})
                if not create_result.get("success"):
                    logger.warning(
                        "issueRelationCreate returned success=False",
                        issue_id=issue_id,
                        blocker_id=blocker_id,
                        response=response,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to create issue relation",
                    issue_id=issue_id,
                    blocker_id=blocker_id,
                    error=str(exc),
                )


def maybe_extract_slack_thread_url(issue: dict[str, Any]) -> str | None:
    comments = [c["body"] for c in issue["comments"]["nodes"]]
    description = issue["description"]
    for text in comments + [description]:
        slack_thread_url = re.search(r"(https://yuppai.slack.com/archives/.*thread_ts=.*)", text)
        if slack_thread_url:
            return slack_thread_url.group(1)
    return None


def get_example_linear_issues_by_assignee(
    min_issues_per_assignee: int = 10, max_issues_per_assignee: int = 50
) -> pd.DataFrame:
    client = LinearClient()
    res = client.get_all_users()
    if not res.get("data", {}).get("users", {}).get("nodes"):
        raise ValueError("No users found")
    users = res["data"]["users"]["nodes"]
    active_users = {user["displayName"]: user["id"] for user in users if user["active"]}
    logger.info(f"Found {len(active_users)} active users")

    data: list[dict[str, Any]] = []
    for active_user, user_id in list(active_users.items()):
        res = client.get_issues(assignee=user_id, limit=max_issues_per_assignee)
        if not res.get("data", {}).get("issues", {}).get("nodes"):
            logger.warning(f"No issues found for {active_user}")
            continue
        issues: list[dict[str, Any]] = res["data"]["issues"]["nodes"]
        if len(issues) < min_issues_per_assignee:
            logger.warning(f"Fewer than {min_issues_per_assignee} issues for {active_user}")
            continue
        logger.info(f"Found {len(issues)} issues for {active_user}")
        data.extend(
            {
                "title": issue["title"],
                "updated_at": issue["updatedAt"],
                "url": issue["url"],
                "description": issue["description"],
                "assignee": active_user,
            }
            for issue in issues[:max_issues_per_assignee]
        )
    return pd.DataFrame(data)
