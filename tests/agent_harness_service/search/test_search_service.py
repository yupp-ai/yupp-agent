"""Unit tests for ypl/agent_harness_service/search/search_service.py.

Covers pure-logic functions that require no DB:
  - _ilike: ILIKE pattern escaping
  - _score_row: additive scoring algorithm
  - _merge_messages_into_sessions: no-op merge hook

And DB-backed search functions with fully mocked sessions:
  - execute_search: concurrent query dispatch, empty-types guard, exception handling
  - _search_sessions, _search_projects, _search_tasks, _search_schedules,
    _search_artifacts (all via execute_search with mocked DB)
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.search.search_service import (
    _SCORE_AGENT_FILTER_MATCH,
    _SCORE_DATA_TEXT_MATCH,
    _SCORE_DESCRIPTION_MATCH,
    _SCORE_RECENT_BOOST,
    _SCORE_TITLE_MATCH,
    _ilike,
    _merge_messages_into_sessions,
    _score_row,
)
from ypl.agent_harness_service.search.search_types import (
    SearchQuery,
    SearchResponse,
    SearchType,
    SessionMessageResult,
    SessionResult,
)

# ---------------------------------------------------------------------------
# _ilike
# ---------------------------------------------------------------------------


class TestIlike:
    def test_plain_term_wrapped_in_percent(self) -> None:
        assert _ilike("hello") == "%hello%"

    def test_percent_sign_escaped(self) -> None:
        result = _ilike("50% off")
        assert "\\%" in result
        assert result.startswith("%")
        assert result.endswith("%")

    def test_underscore_escaped(self) -> None:
        result = _ilike("file_name")
        assert "\\_" in result

    def test_backslash_escaped(self) -> None:
        result = _ilike("path\\to\\file")
        assert "\\\\" in result

    def test_empty_string(self) -> None:
        assert _ilike("") == "%%"

    def test_special_chars_combined(self) -> None:
        result = _ilike("100%_off\\deal")
        # All three special chars should be escaped
        assert "\\%" in result
        assert "\\_" in result
        assert "\\\\" in result

    def test_normal_text_unchanged_except_wrapping(self) -> None:
        term = "search term"
        result = _ilike(term)
        assert result == f"%{term}%"


# ---------------------------------------------------------------------------
# _score_row
# ---------------------------------------------------------------------------


class TestScoreRow:
    def _recent_dt(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=1)

    def _old_dt(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=30)

    def test_title_match_adds_1_0(self) -> None:
        score = _score_row(
            "hello",
            title="Hello World",
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_TITLE_MATCH)

    def test_description_match_adds_0_5(self) -> None:
        score = _score_row(
            "hello",
            title=None,
            description="Say hello there",
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_DESCRIPTION_MATCH)

    def test_data_text_match_adds_0_3(self) -> None:
        score = _score_row(
            "hello",
            title=None,
            description=None,
            data_text='{"msg": "hello"}',
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_DATA_TEXT_MATCH)

    def test_agent_filter_match_adds_0_2(self) -> None:
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col="eng-raccoon",
            agent_filter="raccoon",
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_AGENT_FILTER_MATCH)

    def test_recent_row_adds_0_1(self) -> None:
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=self._recent_dt(),
        )
        assert score == pytest.approx(_SCORE_RECENT_BOOST)

    def test_old_row_no_recency_bonus(self) -> None:
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=self._old_dt(),
        )
        assert score == 0.0

    def test_all_fields_match_maximum_score(self) -> None:
        score = _score_row(
            "hello",
            title="hello title",
            description="hello description",
            data_text="hello data",
            agent_name_col="hello-agent",
            agent_filter="hello",
            created_at=self._recent_dt(),
        )
        expected = (
            _SCORE_TITLE_MATCH
            + _SCORE_DESCRIPTION_MATCH
            + _SCORE_DATA_TEXT_MATCH
            + _SCORE_AGENT_FILTER_MATCH
            + _SCORE_RECENT_BOOST
        )
        assert score == pytest.approx(expected)

    def test_no_match_returns_zero(self) -> None:
        score = _score_row(
            "xyz",
            title="Hello World",
            description="Something else",
            data_text='{"k": "v"}',
            agent_name_col="other-agent",
            agent_filter=None,
            created_at=None,
        )
        assert score == 0.0

    def test_case_insensitive_title_match(self) -> None:
        score = _score_row(
            "HELLO",
            title="hello world",
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_TITLE_MATCH)

    def test_case_insensitive_description_match(self) -> None:
        score = _score_row(
            "WORLD",
            title=None,
            description="hello world",
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_DESCRIPTION_MATCH)

    def test_agent_filter_no_match_no_bonus(self) -> None:
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col="eng-raccoon",
            agent_filter="bookkeeper",
            created_at=None,
        )
        assert score == 0.0

    def test_agent_filter_none_no_bonus(self) -> None:
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col="eng-raccoon",
            agent_filter=None,
            created_at=None,
        )
        assert score == 0.0

    def test_naive_created_at_treated_as_utc(self) -> None:
        # A naive datetime just 1 hour ago should still get recency boost
        naive_recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        score = _score_row(
            "anything",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=naive_recent,
        )
        assert score == pytest.approx(_SCORE_RECENT_BOOST)

    def test_title_and_description_both_match(self) -> None:
        score = _score_row(
            "report",
            title="Weekly report",
            description="This is a report summary",
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == pytest.approx(_SCORE_TITLE_MATCH + _SCORE_DESCRIPTION_MATCH)

    def test_none_title_no_title_bonus(self) -> None:
        score = _score_row(
            "hello",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=None,
        )
        assert score == 0.0

    def test_exactly_7_days_ago_still_recent(self) -> None:
        """Row created exactly 7 days ago should still get the recency boost."""
        # Use slightly under 7 days to avoid flakiness at the boundary
        almost_7_days = datetime.now(UTC) - timedelta(days=6, hours=23, minutes=59)
        score = _score_row(
            "x",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=almost_7_days,
        )
        assert score == pytest.approx(_SCORE_RECENT_BOOST)

    def test_8_days_ago_not_recent(self) -> None:
        eight_days_ago = datetime.now(UTC) - timedelta(days=8)
        score = _score_row(
            "x",
            title=None,
            description=None,
            data_text=None,
            agent_name_col=None,
            agent_filter=None,
            created_at=eight_days_ago,
        )
        assert score == 0.0


# ---------------------------------------------------------------------------
# _merge_messages_into_sessions (no-op hook)
# ---------------------------------------------------------------------------


class TestMergeMessagesIntoSessions:
    def test_does_not_raise(self) -> None:
        sessions: list[SessionResult] = []
        messages: list[SessionMessageResult] = []
        # Should not raise (it's a no-op hook)
        _merge_messages_into_sessions(sessions, messages)

    def test_non_empty_lists_no_raise(self) -> None:
        sessions = [
            SessionResult(
                id=str(uuid.uuid4()),
                score=1.0,
                agent_name="eng-raccoon",
                status="COMPLETED",
                message_count=3,
            )
        ]
        messages = [
            SessionMessageResult(
                id=str(uuid.uuid4()),
                score=0.5,
                session_id=str(uuid.uuid4()),
                turn_number=1,
                role="user",
            )
        ]
        # Should complete without error
        _merge_messages_into_sessions(sessions, messages)


# ---------------------------------------------------------------------------
# execute_search — with fully mocked per-type search functions
# ---------------------------------------------------------------------------


def _make_async_session_ctx(rows: list[Any] | None = None) -> MagicMock:
    """Create an async context manager for get_async_session_read_replica."""
    if rows is None:
        rows = []
    mappings_result = MagicMock()
    mappings_result.all = MagicMock(return_value=rows)
    execute_result = MagicMock()
    execute_result.mappings = MagicMock(return_value=mappings_result)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestExecuteSearch:
    async def test_empty_types_list_returns_empty_response(self) -> None:
        from ypl.agent_harness_service.search.search_service import execute_search

        # types=[] means no types enabled → coro_map is empty
        query = SearchQuery(q="hello", types=[])
        response = await execute_search(query, "user-123")
        assert isinstance(response, SearchResponse)
        assert response.total_count == 0
        assert response.results == {}

    async def test_all_types_run_concurrently_no_db_error(self) -> None:
        from ypl.agent_harness_service.search.search_service import execute_search

        ctx = _make_async_session_ctx(rows=[])

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="test")
            response = await execute_search(query, "user-abc")

        assert isinstance(response, SearchResponse)
        assert response.total_count == 0

    async def test_exception_in_one_type_does_not_kill_others(self) -> None:
        """If one per-type coroutine raises, execute_search should still return results from the rest."""
        from ypl.agent_harness_service.search.search_service import execute_search

        # Patch _search_sessions to raise; all others return empty lists
        ctx_empty = _make_async_session_ctx(rows=[])

        async def _raise(*args: Any, **kwargs: Any) -> list[Any]:
            raise RuntimeError("DB exploded")

        with (
            patch(
                "ypl.agent_harness_service.search.search_service._search_sessions",
                side_effect=_raise,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
                return_value=ctx_empty,
            ),
        ):
            query = SearchQuery(q="test", types=[SearchType.SESSIONS, SearchType.PROJECTS])
            response = await execute_search(query, "user-xyz")

        # Session type failed; response still valid; projects might be empty
        assert isinstance(response, SearchResponse)
        assert SearchType.SESSIONS not in response.results

    async def test_single_type_filter(self) -> None:
        """Requesting only PROJECTS skips SESSION/TASK/etc coroutines."""
        from ypl.agent_harness_service.search.search_service import execute_search

        ctx = _make_async_session_ctx(rows=[])

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="budget", types=[SearchType.PROJECTS])
            response = await execute_search(query, "user-123")

        assert isinstance(response, SearchResponse)
        # No SESSIONS key in results (empty results dict or only projects)
        assert SearchType.SESSIONS not in response.results

    async def test_results_only_include_non_empty_buckets(self) -> None:
        """Buckets with zero results should be omitted from response.results."""
        from ypl.agent_harness_service.search.search_service import execute_search

        ctx = _make_async_session_ctx(rows=[])

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="anythingthatdoesnotmatch")
            response = await execute_search(query, "user-123")

        assert response.total_count == 0
        assert response.results == {}

    async def test_total_count_sums_all_buckets(self) -> None:
        """total_count should equal the sum of all per-type result list lengths."""
        from ypl.agent_harness_service.search.search_service import execute_search

        session_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())

        fake_session_result = SessionResult(
            id=session_id,
            title="test session",
            score=1.0,
            agent_name="eng-raccoon",
        )

        async def _fake_search_sessions(q: Any, uid: str) -> list[SessionResult]:
            return [fake_session_result]

        async def _fake_search_projects(q: Any, uid: str) -> list[Any]:
            from ypl.agent_harness_service.search.search_types import ProjectResult

            return [
                ProjectResult(
                    id=project_id,
                    name="test project",
                    score=0.5,
                )
            ]

        async def _empty(*args: Any) -> list[Any]:
            return []

        with (
            patch(
                "ypl.agent_harness_service.search.search_service._search_sessions",
                side_effect=_fake_search_sessions,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service._search_session_messages",
                side_effect=_empty,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service._search_projects",
                side_effect=_fake_search_projects,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service._search_tasks",
                side_effect=_empty,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service._search_schedules",
                side_effect=_empty,
            ),
            patch(
                "ypl.agent_harness_service.search.search_service._search_artifacts",
                side_effect=_empty,
            ),
        ):
            query = SearchQuery(q="test")
            response = await execute_search(query, "user-123")

        assert response.total_count == 2
        assert SearchType.SESSIONS in response.results
        assert SearchType.PROJECTS in response.results


# ---------------------------------------------------------------------------
# _search_sessions — row scoring and filtering
# ---------------------------------------------------------------------------


class TestSearchSessions:
    def _make_session_row(
        self,
        title: str = "My session",
        agent_name: str = "eng-raccoon",
        status: str = "COMPLETED",
        created_at: datetime | None = None,
        context_text: str | None = None,
    ) -> MagicMock:
        data: dict[str, Any] = {
            "agent_session_id": uuid.uuid4(),
            "title": title,
            "status": status,
            "trigger": "slack",
            "model": "claude-3-5-sonnet",
            "parent_session_id": None,
            "created_at": created_at or datetime.now(UTC) - timedelta(days=1),
            "context_text": context_text,
            "agent_name": agent_name,
            "message_count": 5,
        }
        row = MagicMock()
        row.__getitem__ = lambda self, key: data[key]
        return row

    async def test_title_match_included(self) -> None:
        from ypl.agent_harness_service.search.search_service import _search_sessions
        from ypl.agent_harness_service.search.search_types import SearchQuery

        rows = [self._make_session_row(title="Deploy script session")]

        mappings_result = MagicMock()
        mappings_result.all = MagicMock(return_value=rows)
        exec_result = MagicMock()
        exec_result.mappings = MagicMock(return_value=mappings_result)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="deploy")
            results = await _search_sessions(query, "user-123")

        assert len(results) == 1
        assert results[0].title is not None
        assert "deploy" in results[0].title.lower()

    async def test_non_matching_row_excluded(self) -> None:
        from ypl.agent_harness_service.search.search_service import _search_sessions
        from ypl.agent_harness_service.search.search_types import SearchQuery

        # DB returns a row that doesn't match (over-fetch situation)
        rows = [self._make_session_row(title="Completely different", context_text=None)]

        mappings_result = MagicMock()
        mappings_result.all = MagicMock(return_value=rows)
        exec_result = MagicMock()
        exec_result.mappings = MagicMock(return_value=mappings_result)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="deploy")
            results = await _search_sessions(query, "user-123")

        assert results == []

    async def test_results_sorted_by_score_descending(self) -> None:
        from ypl.agent_harness_service.search.search_service import _search_sessions
        from ypl.agent_harness_service.search.search_types import SearchQuery

        # High-score: title match (recent)
        recent = datetime.now(UTC) - timedelta(hours=1)
        old = datetime.now(UTC) - timedelta(days=30)

        row_high = self._make_session_row(title="deploy script", created_at=recent)
        row_low = self._make_session_row(
            title="Completely unrelated - deploy mention in context",
            created_at=old,
            context_text="deploy here",
        )

        rows = [row_low, row_high]  # deliberately reversed

        mappings_result = MagicMock()
        mappings_result.all = MagicMock(return_value=rows)
        exec_result = MagicMock()
        exec_result.mappings = MagicMock(return_value=mappings_result)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="deploy")
            results = await _search_sessions(query, "user-123")

        assert len(results) == 2
        # First result should have the higher score
        assert results[0].score >= results[1].score

    async def test_limit_per_type_respected(self) -> None:
        from ypl.agent_harness_service.search.search_service import _search_sessions
        from ypl.agent_harness_service.search.search_types import SearchQuery

        rows = [self._make_session_row(title="deploy session") for _ in range(20)]

        mappings_result = MagicMock()
        mappings_result.all = MagicMock(return_value=rows)
        exec_result = MagicMock()
        exec_result.mappings = MagicMock(return_value=mappings_result)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "ypl.agent_harness_service.search.search_service.get_async_session_read_replica",
            return_value=ctx,
        ):
            query = SearchQuery(q="deploy", limit_per_type=5)
            results = await _search_sessions(query, "user-123")

        assert len(results) <= 5


# ---------------------------------------------------------------------------
# Score constants sanity checks
# ---------------------------------------------------------------------------


class TestScoreConstants:
    def test_title_highest_weight(self) -> None:
        assert _SCORE_TITLE_MATCH > _SCORE_DESCRIPTION_MATCH

    def test_description_higher_than_data(self) -> None:
        assert _SCORE_DESCRIPTION_MATCH > _SCORE_DATA_TEXT_MATCH

    def test_data_higher_than_agent(self) -> None:
        assert _SCORE_DATA_TEXT_MATCH > _SCORE_AGENT_FILTER_MATCH

    def test_agent_higher_than_recency(self) -> None:
        assert _SCORE_AGENT_FILTER_MATCH > _SCORE_RECENT_BOOST

    def test_max_possible_score(self) -> None:
        max_score = (
            _SCORE_TITLE_MATCH
            + _SCORE_DESCRIPTION_MATCH
            + _SCORE_DATA_TEXT_MATCH
            + _SCORE_AGENT_FILTER_MATCH
            + _SCORE_RECENT_BOOST
        )
        assert max_score == pytest.approx(2.1)


# ---------------------------------------------------------------------------
# SearchQuery validation
# ---------------------------------------------------------------------------


class TestSearchQueryValidation:
    def test_empty_q_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchQuery(q="")

    def test_whitespace_only_q_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchQuery(q="   ")

    def test_valid_q_accepted(self) -> None:
        q = SearchQuery(q="hello")
        assert q.q == "hello"

    def test_default_limit_per_type(self) -> None:
        q = SearchQuery(q="hello")
        assert q.limit_per_type == 10

    def test_types_defaults_to_none(self) -> None:
        q = SearchQuery(q="hello")
        assert q.types is None

    def test_limit_per_type_min_1(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchQuery(q="hello", limit_per_type=0)

    def test_limit_per_type_max_50(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchQuery(q="hello", limit_per_type=51)

    def test_all_filter_fields_optional(self) -> None:
        q = SearchQuery(q="hello")
        assert q.agent is None
        assert q.status is None
        assert q.date_from is None
        assert q.date_to is None


# ---------------------------------------------------------------------------
# Ilike edge cases
# ---------------------------------------------------------------------------


class TestIlikeEdgeCases:
    @pytest.mark.parametrize(
        "term,expected_contains",
        [
            ("hello", "hello"),
            ("50% off", "50\\% off"),
            ("file_name", "file\\_name"),
            ("path\\file", "path\\\\file"),
        ],
    )
    def test_parametrized_escaping(self, term: str, expected_contains: str) -> None:
        result = _ilike(term)
        assert expected_contains in result
        assert result.startswith("%")
        assert result.endswith("%")
