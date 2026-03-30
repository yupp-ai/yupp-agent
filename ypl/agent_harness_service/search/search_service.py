"""ILIKE plain-text search service for the Agent Harness Service.

Runs per-type queries concurrently and returns a unified ``SearchResponse``.

Scoring (additive):
  +1.0  q matches title / name column
  +0.5  q matches description / message column
  +0.3  q matches data / context JSONB cast
  +0.2  agent filter matches
  +0.1  row was created within the last 7 days
"""

from __future__ import annotations
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text

from ypl.agent_harness_service.search.search_types import (
    ArtifactDocResult,
    ArtifactPRResult,
    ProjectResult,
    ScheduleResult,
    SearchQuery,
    SearchResponse,
    SearchType,
    SessionMessageResult,
    SessionResult,
    TaskResult,
)
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_RECENT_DAYS = 7

# ---------------------------------------------------------------------------
# Scoring weights — adjust here to tune relevance ranking
# ---------------------------------------------------------------------------
_SCORE_TITLE_MATCH = 1.0  # query term found in title / name column
_SCORE_DESCRIPTION_MATCH = 0.5  # query term found in description / message column
_SCORE_DATA_TEXT_MATCH = 0.3  # query term found in data / context JSONB text
_SCORE_AGENT_FILTER_MATCH = 0.2  # agent filter matches agent name column
_SCORE_RECENT_BOOST = 0.1  # row was created within the last _RECENT_DAYS days


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _ilike(val: str) -> str:
    """Wrap a search term as a SQL ILIKE pattern."""
    escaped = val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _score_row(
    q: str,
    *,
    title: str | None,
    description: str | None,
    data_text: str | None,
    agent_name_col: str | None,
    agent_filter: str | None,
    created_at: datetime | None,
) -> float:
    q_lower = q.lower()
    score = 0.0
    if title and q_lower in title.lower():
        score += _SCORE_TITLE_MATCH
    if description and q_lower in description.lower():
        score += _SCORE_DESCRIPTION_MATCH
    if data_text and q_lower in data_text.lower():
        score += _SCORE_DATA_TEXT_MATCH
    if agent_filter and agent_name_col and agent_filter.lower() in agent_name_col.lower():
        score += _SCORE_AGENT_FILTER_MATCH
    if created_at:
        # normalise to UTC-aware before comparing
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at >= _now_utc() - timedelta(days=_RECENT_DAYS):
            score += _SCORE_RECENT_BOOST
    return score


# ---------------------------------------------------------------------------
# Per-type query functions
# ---------------------------------------------------------------------------


@retry_db
async def _search_sessions(
    query: SearchQuery,
    caller_user_id: str,
) -> list[SessionResult]:
    q = query.q
    pat = _ilike(q)
    q_lower = q.lower()

    sql = text(
        """
        SELECT
            s.agent_session_id,
            s.title,
            s.status,
            s.trigger,
            s.model,
            s.parent_session_id,
            s.created_at,
            s.context::text  AS context_text,
            a.name           AS agent_name,
            -- TODO(search): correlated subquery; rewrite as LEFT JOIN + CTE if limit_per_type scales up (#11275)
            (SELECT COUNT(*) FROM agent_session_messages m
             WHERE m.agent_session_id = s.agent_session_id
               AND m.deleted_at IS NULL)  AS message_count
        FROM   agent_sessions s
        JOIN   agents a ON a.agent_id = s.agent_id
        WHERE  s.deleted_at IS NULL
          AND  s.creator_user_id = :user_id
          AND  (
                 s.title      ILIKE :pat
              OR s.context::text ILIKE :pat
              )
          AND  (CAST(:agent AS text) IS NULL OR a.name ILIKE :agent_pat)
          AND  (CAST(:status AS text) IS NULL OR UPPER(s.status::text) = UPPER(:status))
          AND  (CAST(:date_from AS timestamptz) IS NULL OR s.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR s.created_at <= :date_to)
        ORDER BY s.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "pat": pat,
        "agent": query.agent,
        "agent_pat": _ilike(query.agent) if query.agent else None,
        "status": query.status,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,  # over-fetch then re-rank
    }

    results: list[SessionResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    for row in rows:
        score = _score_row(
            q,
            title=row["title"],
            description=None,
            data_text=row["context_text"],
            agent_name_col=row["agent_name"],
            agent_filter=query.agent,
            created_at=row["created_at"],
        )
        # title match check already embedded in score; guarantee at least title match
        title_val: str | None = row["title"]
        context_text: str | None = row["context_text"]
        if not ((title_val and q_lower in title_val.lower()) or (context_text and q_lower in context_text.lower())):
            continue  # skip rows that only matched due to oversized LIMIT

        results.append(
            SessionResult(
                id=str(row["agent_session_id"]),
                title=row["title"],
                score=score,
                created_at=row["created_at"],
                agent_name=row["agent_name"],
                status=str(row["status"]) if row["status"] else None,
                trigger=str(row["trigger"]) if row["trigger"] else None,
                model=row["model"],
                message_count=int(row["message_count"] or 0),
                parent_session_id=str(row["parent_session_id"]) if row["parent_session_id"] else None,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


@retry_db
async def _search_session_messages(
    query: SearchQuery,
    caller_user_id: str,
) -> list[SessionMessageResult]:
    """Return matched messages grouped under their parent session."""
    q = query.q
    pat = _ilike(q)

    sql = text(
        """
        SELECT
            m.agent_session_message_id,
            m.agent_session_id,
            m.turn_number,
            m.role,
            m.content,
            m.created_at,
            s.title         AS session_title,
            s.creator_user_id,
            a.name          AS agent_name
        FROM   agent_session_messages m
        JOIN   agent_sessions s ON s.agent_session_id = m.agent_session_id
        JOIN   agents a          ON a.agent_id = s.agent_id
        WHERE  m.deleted_at IS NULL
          AND  s.deleted_at IS NULL
          AND  s.creator_user_id = :user_id
          AND  m.completion_status::text != 'IN_PROGRESS'
          AND  m.content ILIKE :pat
          AND  (CAST(:agent AS text) IS NULL OR a.name ILIKE :agent_pat)
          AND  (CAST(:date_from AS timestamptz) IS NULL OR m.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR m.created_at <= :date_to)
        ORDER BY m.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "pat": pat,
        "agent": query.agent,
        "agent_pat": _ilike(query.agent) if query.agent else None,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,
    }

    results: list[SessionMessageResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    q_lower = q.lower()
    for row in rows:
        content: str | None = row["content"]
        snippet: str | None = None
        if content:
            idx = content.lower().find(q_lower)
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(content), idx + len(q) + 220)
                snippet = ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")

        score = _score_row(
            q,
            title=None,
            description=content,
            data_text=None,
            agent_name_col=row["agent_name"],
            agent_filter=query.agent,
            created_at=row["created_at"],
        )

        results.append(
            SessionMessageResult(
                id=str(row["agent_session_message_id"]),
                title=f"Turn {row['turn_number']} — {str(row['role']).lower()}",
                score=score,
                created_at=row["created_at"],
                session_id=str(row["agent_session_id"]),
                session_title=row["session_title"],
                role=str(row["role"]).lower() if row["role"] else None,
                turn_number=row["turn_number"],
                snippet=snippet,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


@retry_db
async def _search_projects(
    query: SearchQuery,
    caller_user_id: str,
) -> list[ProjectResult]:
    q = query.q
    pat = _ilike(q)

    sql = text(
        """
        SELECT
            p.agent_project_id,
            p.name,
            p.description,
            p.status,
            p.creator_user_id,
            p.slack_channel,
            p.created_at,
            p.project_data::text AS data_text
        FROM   agent_projects p
        WHERE  p.deleted_at IS NULL
          AND  p.creator_user_id = :user_id
          AND  (
                 p.name              ILIKE :pat
              OR p.description        ILIKE :pat
              OR p.project_data::text ILIKE :pat
              )
          AND  (CAST(:status AS text) IS NULL OR UPPER(p.status::text) = UPPER(:status))
          AND  (CAST(:date_from AS timestamptz) IS NULL OR p.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR p.created_at <= :date_to)
        ORDER BY p.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "pat": pat,
        "status": query.status,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,
    }

    results: list[ProjectResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()
        project_ids = [row["agent_project_id"] for row in rows]

        # Batch-fetch task counts per project
        task_counts: dict[uuid.UUID, dict[str, int]] = {}
        if project_ids:
            tc_sql = text(
                """
                SELECT agent_project_id, status, COUNT(*) AS cnt
                FROM   agent_tasks
                WHERE  deleted_at IS NULL
                  AND  agent_project_id = ANY(:project_ids)
                GROUP BY agent_project_id, status
                """
            )
            tc_rows = (await session.execute(tc_sql, {"project_ids": project_ids})).mappings().all()
            for tc in tc_rows:
                pid = tc["agent_project_id"]
                task_counts.setdefault(pid, {})
                task_counts[pid][str(tc["status"])] = int(tc["cnt"])

    q_lower = q.lower()
    for row in rows:
        score = _score_row(
            q,
            title=row["name"],
            description=row["description"],
            data_text=row["data_text"],
            agent_name_col=None,
            agent_filter=None,
            created_at=row["created_at"],
        )
        # Verify at least one column matches (guard against oversized LIMIT)
        name_val: str = row["name"]
        desc_val: str | None = row["description"]
        data_val: str | None = row["data_text"]
        if not (
            q_lower in name_val.lower()
            or (desc_val and q_lower in desc_val.lower())
            or (data_val and q_lower in data_val.lower())
        ):
            continue

        results.append(
            ProjectResult(
                id=str(row["agent_project_id"]),
                name=row["name"],
                score=score,
                created_at=row["created_at"],
                description=row["description"],
                status=str(row["status"]) if row["status"] else None,
                creator_user_id=row["creator_user_id"],
                slack_channel=row["slack_channel"],
                task_counts=task_counts.get(row["agent_project_id"]),
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


@retry_db
async def _search_tasks(
    query: SearchQuery,
    caller_user_id: str,
) -> list[TaskResult]:
    q = query.q
    pat = _ilike(q)

    sql = text(
        """
        SELECT
            t.agent_task_id,
            t.title,
            t.description,
            t.status,
            t.priority,
            t.depends_on,
            t.completed_at,
            t.created_at,
            t.agent_project_id,
            p.name          AS project_name,
            a.name          AS agent_name
        FROM   agent_tasks t
        JOIN   agent_projects p ON p.agent_project_id = t.agent_project_id
        LEFT JOIN agents a      ON a.agent_id = t.agent_id
        WHERE  t.deleted_at IS NULL
          AND  p.deleted_at IS NULL
          AND  p.creator_user_id = :user_id
          AND  (
                 t.title       ILIKE :pat
              OR t.description  ILIKE :pat
              )
          AND  (CAST(:agent AS text) IS NULL OR a.name ILIKE :agent_pat)
          AND  (CAST(:status AS text) IS NULL OR UPPER(t.status::text) = UPPER(:status))
          AND  (CAST(:date_from AS timestamptz) IS NULL OR t.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR t.created_at <= :date_to)
        ORDER BY t.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "pat": pat,
        "agent": query.agent,
        "agent_pat": _ilike(query.agent) if query.agent else None,
        "status": query.status,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,
    }

    results: list[TaskResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    q_lower = q.lower()
    for row in rows:
        title_val: str = row["title"]
        desc_val: str | None = row["description"]
        if not (q_lower in title_val.lower() or (desc_val and q_lower in desc_val.lower())):
            continue

        score = _score_row(
            q,
            title=title_val,
            description=desc_val,
            data_text=None,
            agent_name_col=row["agent_name"],
            agent_filter=query.agent,
            created_at=row["created_at"],
        )
        depends_on: list[str] | None = row["depends_on"]

        results.append(
            TaskResult(
                id=str(row["agent_task_id"]),
                title=title_val,
                score=score,
                created_at=row["created_at"],
                project_id=str(row["agent_project_id"]),
                project_name=row["project_name"],
                description=desc_val,
                status=str(row["status"]) if row["status"] else None,
                priority=str(row["priority"]) if row["priority"] else None,
                agent_name=row["agent_name"],
                depends_on=depends_on if isinstance(depends_on, list) else None,
                completed_at=row["completed_at"],
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


@retry_db
async def _search_schedules(
    query: SearchQuery,
    caller_user_id: str,
) -> list[ScheduleResult]:
    q = query.q
    pat = _ilike(q)

    sql = text(
        """
        SELECT
            s.agent_schedule_id,
            s.name,
            s.message,
            s.description,
            s.status,
            s.schedule_type,
            s.cron_expression,
            s.next_run_at,
            s.run_count,
            s.created_by_user,
            s.created_at,
            a.name AS agent_name
        FROM   agent_schedules s
        JOIN   agents a ON a.agent_id = s.agent_id
        WHERE  s.deleted_at IS NULL
          AND  s.created_by_user = :user_id
          AND  (
                 s.name        ILIKE :pat
              OR s.message      ILIKE :pat
              OR s.description  ILIKE :pat
              )
          AND  (CAST(:agent AS text) IS NULL OR a.name ILIKE :agent_pat)
          AND  (CAST(:status AS text) IS NULL OR UPPER(s.status::text) = UPPER(:status))
          AND  (CAST(:date_from AS timestamptz) IS NULL OR s.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR s.created_at <= :date_to)
        ORDER BY s.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "pat": pat,
        "agent": query.agent,
        "agent_pat": _ilike(query.agent) if query.agent else None,
        "status": query.status,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,
    }

    results: list[ScheduleResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    q_lower = q.lower()
    for row in rows:
        name_val: str | None = row["name"]
        msg_val: str = row["message"]
        desc_val: str | None = row["description"]
        if not (
            (name_val and q_lower in name_val.lower())
            or q_lower in msg_val.lower()
            or (desc_val and q_lower in desc_val.lower())
        ):
            continue

        # Pick the field that matched for the +0.5 description slot.
        # If q matches description but not message, use description.
        # If q matches message but not description (or description is absent), use message.
        # This ensures +0.5 fires whenever q appears in *either* text column.
        desc_matches = desc_val and q_lower in desc_val.lower()
        effective_desc = desc_val if desc_matches else msg_val
        score = _score_row(
            q,
            title=name_val,
            description=effective_desc,
            data_text=None,
            agent_name_col=row["agent_name"],
            agent_filter=query.agent,
            created_at=row["created_at"],
        )

        results.append(
            ScheduleResult(
                id=str(row["agent_schedule_id"]),
                name=name_val,
                score=score,
                created_at=row["created_at"],
                agent_name=row["agent_name"],
                schedule_type=str(row["schedule_type"]) if row["schedule_type"] else None,
                status=str(row["status"]) if row["status"] else None,
                cron_expression=row["cron_expression"],
                next_run_at=row["next_run_at"],
                run_count=int(row["run_count"] or 0),
                created_by_user=row["created_by_user"],
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


@retry_db
async def _search_artifacts(
    query: SearchQuery,
    caller_user_id: str,
    artifact_type_filter: str,  # "YUPPASTE" → ArtifactDocResult, "CODE_REVIEW" → ArtifactPRResult
) -> list[ArtifactPRResult | ArtifactDocResult]:
    q = query.q
    pat = _ilike(q)

    sql = text(
        """
        SELECT
            aa.agent_artifact_id,
            aa.artifact_type,
            aa.title,
            aa.description,
            aa.url,
            aa.agent_session_id,
            aa.agent_task_id,
            aa.artifact_metadata,
            aa.created_at,
            p.agent_project_id
        FROM   agent_artifacts aa
        LEFT JOIN agent_tasks t  ON t.agent_task_id = aa.agent_task_id AND t.deleted_at IS NULL
        LEFT JOIN agent_projects p ON p.agent_project_id = t.agent_project_id AND p.deleted_at IS NULL
        WHERE  aa.deleted_at IS NULL
          AND  aa.creator_user_id = :user_id
          AND  aa.artifact_type::text = :artifact_type
          AND  (
                 aa.title       ILIKE :pat
              OR aa.description  ILIKE :pat
              OR aa.url          ILIKE :pat
              )
          AND  (CAST(:date_from AS timestamptz) IS NULL OR aa.created_at >= :date_from)
          AND  (CAST(:date_to AS timestamptz) IS NULL OR aa.created_at <= :date_to)
        ORDER BY aa.created_at DESC
        LIMIT  :limit
        """
    )
    params: dict[str, Any] = {
        "user_id": caller_user_id,
        "artifact_type": artifact_type_filter,
        "pat": pat,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "limit": query.limit_per_type * 3,
    }

    results: list[ArtifactPRResult | ArtifactDocResult] = []
    async with get_async_session_read_replica() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    q_lower = q.lower()
    is_pr = artifact_type_filter == "CODE_REVIEW"

    for row in rows:
        title_val: str = row["title"]
        desc_val: str | None = row["description"]
        url_val: str = row["url"]
        if not (
            q_lower in title_val.lower() or (desc_val and q_lower in desc_val.lower()) or q_lower in url_val.lower()
        ):
            continue

        score = _score_row(
            q,
            title=title_val,
            description=desc_val,
            data_text=url_val,
            agent_name_col=None,
            agent_filter=None,
            created_at=row["created_at"],
        )
        meta: dict[str, Any] | None = row["artifact_metadata"]
        session_id_str = str(row["agent_session_id"]) if row["agent_session_id"] else None
        project_id_str = str(row["agent_project_id"]) if row["agent_project_id"] else None

        if is_pr:
            results.append(
                ArtifactPRResult(
                    id=str(row["agent_artifact_id"]),
                    title=title_val,
                    score=score,
                    created_at=row["created_at"],
                    url=url_val,
                    session_id=session_id_str,
                    project_id=project_id_str,
                    pr_number=meta.get("pr_number") if meta else None,
                    repo=meta.get("repo") if meta else None,
                    extra={k: v for k, v in meta.items() if k not in ("pr_number", "repo")} if meta else None,
                )
            )
        else:
            results.append(
                ArtifactDocResult(
                    id=str(row["agent_artifact_id"]),
                    title=title_val,
                    score=score,
                    created_at=row["created_at"],
                    url=url_val,
                    session_id=session_id_str,
                    project_id=project_id_str,
                    size_bytes=meta.get("size_bytes") if meta else None,
                    extra={k: v for k, v in meta.items() if k != "size_bytes"} if meta else None,
                )
            )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[: query.limit_per_type]


# ---------------------------------------------------------------------------
# Session-message / session merging
# ---------------------------------------------------------------------------


def _merge_messages_into_sessions(
    session_results: list[SessionResult],
    message_results: list[SessionMessageResult],
) -> None:
    """Attach matched messages to their parent ``SessionResult`` when present.

    This is a metadata hint only — ``SessionResult`` does not currently have a
    ``matched_messages`` field, so we just ensure duplicate sessions are not
    counted.  The message results bucket still contains all matched messages;
    sessions that appear in *both* buckets are expected (the front-end can
    de-duplicate on ``session_id``).
    """
    # No structural merge needed until SessionResult gains a messages list field.
    # Reserve this function as a hook for future enrichment.


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def execute_search(query: SearchQuery, caller_user_id: str) -> SearchResponse:
    """Run all enabled type queries concurrently and return a unified response.

    Args:
        query: Structured search query (IQL).
        caller_user_id: The Yupp user ID of the calling user.  All results are
            scoped to this user.  ``query.creator_user_id`` is used only as an
            additional filter within the caller's own data; it never overrides
            the auth scope.

    Returns:
        A ``SearchResponse`` with per-type result buckets.
    """
    enabled_types: list[SearchType] = list(query.types) if query.types is not None else list(SearchType)

    # Build the coroutine list in a deterministic order
    coro_map: dict[SearchType, Any] = {}
    if SearchType.SESSIONS in enabled_types:
        coro_map[SearchType.SESSIONS] = _search_sessions(query, caller_user_id)
    if SearchType.SESSION_MESSAGES in enabled_types:
        coro_map[SearchType.SESSION_MESSAGES] = _search_session_messages(query, caller_user_id)
    if SearchType.PROJECTS in enabled_types:
        coro_map[SearchType.PROJECTS] = _search_projects(query, caller_user_id)
    if SearchType.TASKS in enabled_types:
        coro_map[SearchType.TASKS] = _search_tasks(query, caller_user_id)
    if SearchType.SCHEDULES in enabled_types:
        coro_map[SearchType.SCHEDULES] = _search_schedules(query, caller_user_id)
    if SearchType.ARTIFACT_PR in enabled_types:
        coro_map[SearchType.ARTIFACT_PR] = _search_artifacts(query, caller_user_id, "CODE_REVIEW")
    if SearchType.ARTIFACT_DOC in enabled_types:
        coro_map[SearchType.ARTIFACT_DOC] = _search_artifacts(query, caller_user_id, "YUPPASTE")

    if not coro_map:
        return SearchResponse(query=query, results={}, total_count=0)

    keys = list(coro_map.keys())
    gathered = await asyncio.gather(*[coro_map[k] for k in keys], return_exceptions=True)

    results: dict[SearchType, list[Any]] = {}
    for key, outcome in zip(keys, gathered, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("search_type_query_failed", search_type=str(key), error=str(outcome))
            continue
        if outcome:
            results[key] = outcome

    # Merge session-message hits into matching session results
    if SearchType.SESSIONS in results and SearchType.SESSION_MESSAGES in results:
        _merge_messages_into_sessions(
            cast(list[SessionResult], results[SearchType.SESSIONS]),
            cast(list[SessionMessageResult], results[SearchType.SESSION_MESSAGES]),
        )

    total = sum(len(v) for v in results.values())
    return SearchResponse(query=query, results=results, total_count=total)
