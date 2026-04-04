"""Tests for linear.py — Linear project management MCP tools."""

from __future__ import annotations
from unittest.mock import MagicMock, patch

from ypl.agent_harness_service.tools.linear import (
    linear_create_issue as _linear_create_issue_tool,
)
from ypl.agent_harness_service.tools.linear import (
    linear_get_issue as _linear_get_issue_tool,
)
from ypl.agent_harness_service.tools.linear import (
    linear_get_workflow_states as _linear_get_workflow_states_tool,
)
from ypl.agent_harness_service.tools.linear import (
    linear_list_teams as _linear_list_teams_tool,
)
from ypl.agent_harness_service.tools.linear import (
    linear_search_issues as _linear_search_issues_tool,
)
from ypl.agent_harness_service.tools.linear import (
    linear_update_issue as _linear_update_issue_tool,
)

# Unwrap FunctionTool to get raw callables
linear_create_issue = _linear_create_issue_tool.fn
linear_get_issue = _linear_get_issue_tool.fn
linear_get_workflow_states = _linear_get_workflow_states_tool.fn
linear_list_teams = _linear_list_teams_tool.fn
linear_search_issues = _linear_search_issues_tool.fn
linear_update_issue = _linear_update_issue_tool.fn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEAM_ID = "team-abc-123"
ISSUE_ID = "ENG-42"
STATE_ID = "state-xyz-789"


def _make_client(return_value: dict) -> MagicMock:
    client = MagicMock()
    client.get_teams.return_value = return_value
    client.get_workflow_states.return_value = return_value
    client.get_issue.return_value = return_value
    client.search_issues.return_value = return_value
    client.create_issue.return_value = return_value
    client.update_issue.return_value = return_value
    return client


# ---------------------------------------------------------------------------
# linear_list_teams
# ---------------------------------------------------------------------------


class TestLinearListTeams:
    def test_success(self) -> None:
        nodes = [{"id": TEAM_ID, "name": "Engineering", "key": "ENG"}]
        response = {"data": {"teams": {"nodes": nodes}}}
        client = _make_client(response)

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_list_teams()

        assert result == {"teams": nodes}

    def test_graphql_error_returned(self) -> None:
        response = {"errors": [{"message": "Unauthorized"}]}
        client = _make_client(response)

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_list_teams()

        assert result == {"error": "Unauthorized"}

    def test_exception_returns_error(self) -> None:
        client = MagicMock()
        client.get_teams.side_effect = RuntimeError("network failure")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_list_teams()

        assert "error" in result
        assert "network failure" in result["error"]

    def test_empty_teams(self) -> None:
        response: dict = {"data": {"teams": {"nodes": []}}}
        client = _make_client(response)

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_list_teams()

        assert result == {"teams": []}


# ---------------------------------------------------------------------------
# linear_get_workflow_states
# ---------------------------------------------------------------------------


class TestLinearGetWorkflowStates:
    def test_success_sorted_by_position(self) -> None:
        nodes = [
            {"id": "s2", "name": "Done", "type": "completed", "position": 2},
            {"id": "s1", "name": "Todo", "type": "backlog", "position": 1},
        ]
        response = {"data": {"workflowStates": {"nodes": nodes}}}
        client = MagicMock()
        client.get_workflow_states.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_workflow_states(TEAM_ID)

        assert result["states"][0]["id"] == "s1"
        assert result["states"][1]["id"] == "s2"
        client.get_workflow_states.assert_called_once_with(TEAM_ID)

    def test_graphql_error(self) -> None:
        response = {"errors": [{"message": "Team not found"}]}
        client = MagicMock()
        client.get_workflow_states.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_workflow_states(TEAM_ID)

        assert result == {"error": "Team not found"}

    def test_exception_returns_error(self) -> None:
        client = MagicMock()
        client.get_workflow_states.side_effect = ConnectionError("timeout")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_workflow_states(TEAM_ID)

        assert "error" in result


# ---------------------------------------------------------------------------
# linear_get_issue
# ---------------------------------------------------------------------------


class TestLinearGetIssue:
    def test_success(self) -> None:
        issue = {"id": ISSUE_ID, "title": "Fix bug", "state": {"name": "In Progress"}}
        response = {"data": {"issue": issue}}
        client = MagicMock()
        client.get_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_issue(ISSUE_ID)

        assert result == {"issue": issue}

    def test_issue_not_found(self) -> None:
        response = {"data": {"issue": None}}
        client = MagicMock()
        client.get_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_issue("UNKNOWN-999")

        assert result == {"error": "Issue not found"}

    def test_graphql_error(self) -> None:
        response = {"errors": [{"message": "Access denied"}]}
        client = MagicMock()
        client.get_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_issue(ISSUE_ID)

        assert result == {"error": "Access denied"}

    def test_exception(self) -> None:
        client = MagicMock()
        client.get_issue.side_effect = ValueError("bad id")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_get_issue("bad-id")

        assert "error" in result


# ---------------------------------------------------------------------------
# linear_search_issues
# ---------------------------------------------------------------------------


class TestLinearSearchIssues:
    def test_success(self) -> None:
        nodes = [{"id": "i1", "title": "Issue 1"}]
        response = {"data": {"issues": {"nodes": nodes}}}
        client = MagicMock()
        client.search_issues.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_search_issues(query="bug")

        assert result == {"issues": nodes, "count": 1}
        client.search_issues.assert_called_once_with(
            query_term="bug", team_id=None, state_id=None, assignee_id=None, limit=20
        )

    def test_limit_capped_at_50(self) -> None:
        response: dict = {"data": {"issues": {"nodes": []}}}
        client = MagicMock()
        client.search_issues.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            linear_search_issues(limit=100)

        call_kwargs = client.search_issues.call_args[1]
        assert call_kwargs["limit"] == 50

    def test_filters_passed_through(self) -> None:
        response: dict = {"data": {"issues": {"nodes": []}}}
        client = MagicMock()
        client.search_issues.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            linear_search_issues(team_id=TEAM_ID, state_id=STATE_ID, assignee_id="user-1")

        call_kwargs = client.search_issues.call_args[1]
        assert call_kwargs["team_id"] == TEAM_ID
        assert call_kwargs["state_id"] == STATE_ID
        assert call_kwargs["assignee_id"] == "user-1"

    def test_graphql_error(self) -> None:
        response = {"errors": [{"message": "Rate limited"}]}
        client = MagicMock()
        client.search_issues.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_search_issues()

        assert result == {"error": "Rate limited"}

    def test_exception(self) -> None:
        client = MagicMock()
        client.search_issues.side_effect = Exception("unexpected")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_search_issues()

        assert "error" in result


# ---------------------------------------------------------------------------
# linear_create_issue
# ---------------------------------------------------------------------------


class TestLinearCreateIssue:
    def test_success(self) -> None:
        new_issue = {"id": "new-1", "identifier": "ENG-99", "title": "New issue", "url": "https://linear.app/..."}
        response = {"data": {"issueCreate": {"success": True, "issue": new_issue}}}
        client = MagicMock()
        client.create_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_create_issue(title="New issue", team_id=TEAM_ID)

        assert result == {"issue": new_issue, "success": True}
        client.create_issue.assert_called_once()

    def test_create_failed_flag(self) -> None:
        response = {"data": {"issueCreate": {"success": False, "issue": None}}}
        client = MagicMock()
        client.create_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_create_issue(title="Fail", team_id=TEAM_ID)

        assert result == {"error": "Create failed"}

    def test_graphql_error(self) -> None:
        response = {"errors": [{"message": "Invalid team"}]}
        client = MagicMock()
        client.create_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_create_issue(title="Test", team_id="bad-id")

        assert result == {"error": "Invalid team"}

    def test_all_optional_fields_passed(self) -> None:
        new_issue = {"id": "i1", "title": "New"}
        response = {"data": {"issueCreate": {"success": True, "issue": new_issue}}}
        client = MagicMock()
        client.create_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            linear_create_issue(
                title="Test",
                team_id=TEAM_ID,
                description="desc",
                state_id=STATE_ID,
                assignee_id="user-1",
                priority=2,
            )

        call_kwargs = client.create_issue.call_args[1]
        assert call_kwargs["description"] == "desc"
        assert call_kwargs["state_id"] == STATE_ID
        assert call_kwargs["assignee_id"] == "user-1"
        assert call_kwargs["priority"] == 2

    def test_exception(self) -> None:
        client = MagicMock()
        client.create_issue.side_effect = Exception("API error")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_create_issue(title="Test", team_id=TEAM_ID)

        assert "error" in result


# ---------------------------------------------------------------------------
# linear_update_issue
# ---------------------------------------------------------------------------


class TestLinearUpdateIssue:
    def test_success(self) -> None:
        updated = {"id": ISSUE_ID, "title": "Updated", "state": {"name": "Done"}}
        response = {"data": {"issueUpdate": {"success": True, "issue": updated}}}
        client = MagicMock()
        client.update_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_update_issue(issue_id=ISSUE_ID, title="Updated")

        assert result == {"issue": updated, "success": True}

    def test_no_fields_returns_error(self) -> None:
        # When no fields provided, should return error without calling the client
        client = MagicMock()
        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_update_issue(issue_id=ISSUE_ID)

        assert result == {"error": "At least one field must be provided to update"}
        client.update_issue.assert_not_called()

    def test_update_failed_flag(self) -> None:
        response = {"data": {"issueUpdate": {"success": False, "issue": None}}}
        client = MagicMock()
        client.update_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_update_issue(issue_id=ISSUE_ID, state_id="bad-state")

        assert result == {"error": "Update failed"}

    def test_graphql_error(self) -> None:
        response = {"errors": [{"message": "Issue locked"}]}
        client = MagicMock()
        client.update_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_update_issue(issue_id=ISSUE_ID, title="new")

        assert result == {"error": "Issue locked"}

    def test_multiple_fields(self) -> None:
        updated = {"id": ISSUE_ID}
        response = {"data": {"issueUpdate": {"success": True, "issue": updated}}}
        client = MagicMock()
        client.update_issue.return_value = response

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            linear_update_issue(issue_id=ISSUE_ID, title="new", description="desc", priority=1)

        call_kwargs = client.update_issue.call_args[1]
        assert call_kwargs["title"] == "new"
        assert call_kwargs["description"] == "desc"
        assert call_kwargs["priority"] == 1

    def test_exception(self) -> None:
        client = MagicMock()
        client.update_issue.side_effect = Exception("network timeout")

        with patch("ypl.agent_harness_service.tools.linear._get_linear_client", return_value=client):
            result = linear_update_issue(issue_id=ISSUE_ID, title="x")

        assert "error" in result
