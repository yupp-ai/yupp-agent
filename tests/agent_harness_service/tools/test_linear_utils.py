"""Unit tests for ypl/agent_harness_service/tools/linear_sync/linear_utils.py.

Covers:
- is_uuid: valid, invalid, edge cases
- list_teams: returns structured team dicts from API response
- list_projects: returns structured project dicts from API response
- resolve_team_id: UUID passthrough, key match, name match, not found
- resolve_project_id: UUID passthrough, exact name match, slug match from name results,
  slug direct search, single-match fallback, multiple matches (None), no matches (None)
"""

from __future__ import annotations
from unittest.mock import MagicMock, patch

from ypl.agent_harness_service.tools.linear_sync.linear_utils import (
    is_uuid,
    list_projects,
    list_teams,
    resolve_project_id,
    resolve_team_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_VALID_UUID_2 = "11111111-2222-3333-4444-555555555555"


def _make_client(teams: list[dict] | None = None, post_responses: list[dict] | None = None) -> MagicMock:
    """Create a mock LinearClient."""
    client = MagicMock()
    client.get_teams.return_value = {"data": {"teams": {"nodes": teams or []}}}
    # post is called for arbitrary GraphQL queries
    if post_responses:
        client.post.side_effect = post_responses
    else:
        client.post.return_value = {"data": {"projects": {"nodes": []}}}
    return client


def _make_team(
    team_id: str = _VALID_UUID,
    name: str = "Eng Team",
    key: str = "ENG",
    description: str = "Engineering",
) -> dict:
    return {"id": team_id, "name": name, "key": key, "description": description}


def _make_project(proj_id: str = _VALID_UUID, name: str = "My Project", slug: str = "my-proj") -> dict:
    return {"id": proj_id, "name": name, "slugId": slug}


def _name_search_response(projects: list[dict]) -> dict:
    return {"data": {"projects": {"nodes": projects}}}


# ---------------------------------------------------------------------------
# is_uuid
# ---------------------------------------------------------------------------


class TestIsUuid:
    def test_valid_lowercase_uuid(self) -> None:
        assert is_uuid("a1b2c3d4-e5f6-7890-abcd-ef1234567890") is True

    def test_valid_uppercase_uuid(self) -> None:
        assert is_uuid("A1B2C3D4-E5F6-7890-ABCD-EF1234567890") is True

    def test_valid_mixed_case_uuid(self) -> None:
        assert is_uuid("A1b2C3d4-e5F6-7890-ABCD-ef1234567890") is True

    def test_short_string_is_not_uuid(self) -> None:
        assert is_uuid("abc123") is False

    def test_empty_string_is_not_uuid(self) -> None:
        assert is_uuid("") is False

    def test_team_key_is_not_uuid(self) -> None:
        assert is_uuid("ENG") is False

    def test_project_name_is_not_uuid(self) -> None:
        assert is_uuid("My Awesome Project") is False

    def test_slug_is_not_uuid(self) -> None:
        assert is_uuid("my-project-slug") is False

    def test_wrong_format_extra_chars(self) -> None:
        assert is_uuid("a1b2c3d4-e5f6-7890-abcd-ef123456789X") is False

    def test_wrong_segment_count(self) -> None:
        assert is_uuid("a1b2c3d4-e5f6-7890-abcd") is False


# ---------------------------------------------------------------------------
# list_teams
# ---------------------------------------------------------------------------


class TestListTeams:
    def test_returns_empty_list_when_no_teams(self) -> None:
        client = _make_client(teams=[])
        result = list_teams(client)
        assert result == []

    def test_returns_structured_team_dicts(self) -> None:
        client = _make_client(teams=[_make_team()])
        result = list_teams(client)
        assert len(result) == 1
        team = result[0]
        assert team["id"] == _VALID_UUID
        assert team["name"] == "Eng Team"
        assert team["key"] == "ENG"
        assert team["description"] == "Engineering"

    def test_returns_multiple_teams(self) -> None:
        client = _make_client(
            teams=[
                _make_team(_VALID_UUID, "Team A", "A"),
                _make_team(_VALID_UUID_2, "Team B", "B"),
            ]
        )
        result = list_teams(client)
        assert len(result) == 2
        names = {t["name"] for t in result}
        assert names == {"Team A", "Team B"}

    def test_creates_default_client_when_none_provided(self) -> None:
        """When client=None, a LinearClient should be created."""
        mock_client = _make_client(teams=[_make_team()])
        with patch(
            "ypl.agent_harness_service.tools.linear_sync.linear_utils.LinearClient",
            return_value=mock_client,
        ):
            result = list_teams(None)
        assert len(result) == 1

    def test_handles_missing_fields_gracefully(self) -> None:
        """Teams with missing fields should produce empty strings, not KeyError."""
        client = _make_client(teams=[{}])  # completely empty team dict
        result = list_teams(client)
        assert len(result) == 1
        assert result[0]["id"] == ""
        assert result[0]["name"] == ""
        assert result[0]["key"] == ""

    def test_handles_missing_data_key_gracefully(self) -> None:
        """API response missing 'data' key should return empty list."""
        client = MagicMock()
        client.get_teams.return_value = {}
        result = list_teams(client)
        assert result == []


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    def test_returns_empty_list_when_no_projects(self) -> None:
        client = _make_client(post_responses=[_name_search_response([])])
        result = list_projects(client, limit=10)
        assert result == []

    def test_returns_structured_project_dicts(self) -> None:
        proj = _make_project()
        client = _make_client(post_responses=[_name_search_response([proj])])
        result = list_projects(client, limit=10)
        assert len(result) == 1
        p = result[0]
        assert p["id"] == _VALID_UUID
        assert p["name"] == "My Project"
        assert p["slug_id"] == "my-proj"

    def test_passes_limit_to_query(self) -> None:
        client = _make_client(post_responses=[_name_search_response([])])
        list_projects(client, limit=25)
        call_args = client.post.call_args[0][0]
        assert call_args["variables"]["first"] == 25

    def test_handles_missing_data_gracefully(self) -> None:
        client = MagicMock()
        client.post.return_value = {}
        result = list_projects(client)
        assert result == []


# ---------------------------------------------------------------------------
# resolve_team_id
# ---------------------------------------------------------------------------


class TestResolveTeamId:
    def test_uuid_input_returned_as_is(self) -> None:
        result = resolve_team_id(_VALID_UUID)
        assert result == _VALID_UUID

    def test_resolves_by_key_case_insensitive(self) -> None:
        client = _make_client(
            teams=[
                _make_team(_VALID_UUID, "Engineering", "ENG"),
                _make_team(_VALID_UUID_2, "Product", "PROD"),
            ]
        )
        result = resolve_team_id("eng", client)
        assert result == _VALID_UUID

    def test_resolves_by_key_uppercase(self) -> None:
        client = _make_client(teams=[_make_team(_VALID_UUID, "Engineering", "ENG")])
        result = resolve_team_id("ENG", client)
        assert result == _VALID_UUID

    def test_resolves_by_name_when_key_not_matching(self) -> None:
        client = _make_client(teams=[_make_team(_VALID_UUID, "Engineering", "ENG")])
        result = resolve_team_id("engineering", client)
        assert result == _VALID_UUID

    def test_key_match_preferred_over_name_match(self) -> None:
        """When both key and name could match, key takes priority."""
        # If team key is 'Foo' and team name is 'foo', key match is tried first.
        client = _make_client(
            teams=[
                _make_team(_VALID_UUID, "Dummy", "FOO"),
                _make_team(_VALID_UUID_2, "Foo", "BAR"),
            ]
        )
        result = resolve_team_id("FOO", client)
        assert result == _VALID_UUID  # key match wins

    def test_returns_none_when_no_match(self) -> None:
        client = _make_client(teams=[_make_team(_VALID_UUID, "Engineering", "ENG")])
        result = resolve_team_id("nonexistent", client)
        assert result is None

    def test_creates_default_client_when_none_provided(self) -> None:
        mock_client = _make_client(teams=[_make_team(_VALID_UUID, "Engineering", "ENG")])
        with patch(
            "ypl.agent_harness_service.tools.linear_sync.linear_utils.LinearClient",
            return_value=mock_client,
        ):
            result = resolve_team_id("ENG")
        assert result == _VALID_UUID

    def test_empty_string_returns_none(self) -> None:
        client = _make_client(teams=[_make_team(_VALID_UUID, "Engineering", "ENG")])
        result = resolve_team_id("", client)
        assert result is None


# ---------------------------------------------------------------------------
# resolve_project_id
# ---------------------------------------------------------------------------


class TestResolveProjectId:
    def test_uuid_input_returned_as_is(self) -> None:
        result = resolve_project_id(_VALID_UUID)
        assert result == _VALID_UUID

    def test_resolves_by_exact_name(self) -> None:
        proj = _make_project(_VALID_UUID, "My Project", "my-proj")
        client = _make_client(
            post_responses=[
                _name_search_response([proj]),  # exact name search
                # slug search won't be reached
            ]
        )
        result = resolve_project_id("My Project", client)
        assert result == _VALID_UUID

    def test_resolves_by_name_case_insensitive(self) -> None:
        proj = _make_project(_VALID_UUID, "My Project", "my-proj")
        client = _make_client(
            post_responses=[
                _name_search_response([proj]),  # exact name (eqIgnoreCase)
            ]
        )
        result = resolve_project_id("my project", client)
        assert result == _VALID_UUID

    def test_falls_back_to_contains_search(self) -> None:
        proj = _make_project(_VALID_UUID, "My Awesome Project", "map")
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact name: no results
                _name_search_response([proj]),  # contains search: found
                _name_search_response([]),  # slug direct search: no results
            ]
        )
        result = resolve_project_id("My Awesome Project", client)
        assert result == _VALID_UUID

    def test_resolves_by_slug_from_name_results(self) -> None:
        proj = _make_project(_VALID_UUID, "Something Else", "my-slug")
        client = _make_client(
            post_responses=[
                _name_search_response([proj]),  # name search returns it
                # slug direct search won't be reached since slug matched
            ]
        )
        result = resolve_project_id("my-slug", client)
        assert result == _VALID_UUID

    def test_resolves_by_direct_slug_search(self) -> None:
        proj = _make_project(_VALID_UUID, "Totally Different Name", "target-slug")
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact name: no match
                _name_search_response([]),  # contains: no match
                _name_search_response([proj]),  # direct slug search: found
            ]
        )
        result = resolve_project_id("target-slug", client)
        assert result == _VALID_UUID

    def test_single_match_fallback_used_as_last_resort(self) -> None:
        proj = _make_project(_VALID_UUID, "Partial Match Project", "partial-slug")
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact name: empty
                _name_search_response([proj]),  # contains: one result
                _name_search_response([]),  # direct slug: empty
            ]
        )
        result = resolve_project_id("Partial", client)
        assert result == _VALID_UUID

    def test_returns_none_when_multiple_matches(self) -> None:
        # Use slugs that don't match the search input to avoid slug-match short circuit
        proj1 = _make_project(_VALID_UUID, "Alpha Project", "proj-one-slug")
        proj2 = _make_project(_VALID_UUID_2, "Alpha Beta Project", "proj-two-slug")
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact: empty
                _name_search_response([proj1, proj2]),  # contains: 2 results (no name exact match)
                _name_search_response([]),  # direct slug: empty
            ]
        )
        # Input "Alpha" won't match either name exactly, won't match either slug,
        # and there are 2 results from contains → function returns None
        result = resolve_project_id("Alpha", client)
        assert result is None

    def test_returns_none_when_no_match_at_all(self) -> None:
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact: empty
                _name_search_response([]),  # contains: empty
                _name_search_response([]),  # direct slug: empty
            ]
        )
        result = resolve_project_id("Nonexistent Project", client)
        assert result is None

    def test_creates_default_client_when_none_provided(self) -> None:
        proj = _make_project(_VALID_UUID, "Test Project", "test")
        mock_client = _make_client(
            post_responses=[
                _name_search_response([proj]),
            ]
        )
        with patch(
            "ypl.agent_harness_service.tools.linear_sync.linear_utils.LinearClient",
            return_value=mock_client,
        ):
            result = resolve_project_id("Test Project")
        assert result == _VALID_UUID

    def test_exact_name_match_preferred_over_slug(self) -> None:
        """If both name and slug match, exact name match takes priority."""
        # proj1 has name == input, proj2 has slug == input
        proj1 = _make_project(_VALID_UUID, "target", "some-slug")
        proj2 = _make_project(_VALID_UUID_2, "Other", "target")
        client = _make_client(
            post_responses=[
                _name_search_response([proj1, proj2]),
            ]
        )
        result = resolve_project_id("target", client)
        assert result == _VALID_UUID  # name match wins

    def test_slug_match_from_name_results_returns_correct_project(self) -> None:
        """When name search returns projects, slug match is tried before single-match."""
        # Two projects returned by name search; only one matches the slug
        proj1 = _make_project(_VALID_UUID, "Other Project", "not-the-slug")
        proj2 = _make_project(_VALID_UUID_2, "The One", "the-slug")
        client = _make_client(
            post_responses=[
                _name_search_response([]),  # exact: no match
                _name_search_response([proj1, proj2]),  # contains: 2 results
                _name_search_response([]),  # slug direct: not found
            ]
        )
        result = resolve_project_id("the-slug", client)
        # proj2 has slugId == 'the-slug', so it should be found via slug match
        assert result == _VALID_UUID_2
