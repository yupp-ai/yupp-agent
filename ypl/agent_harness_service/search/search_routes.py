"""REST API route for AHS entity search.

Endpoints:
- POST /search — search across sessions, projects, tasks, schedules, and artifacts
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.search.iql_translator import text_to_iql
from ypl.agent_harness_service.search.search_service import execute_search
from ypl.agent_harness_service.search.search_types import SearchQuery, SearchResponse
from ypl.structured_logger import get_logger

_logger = get_logger()

search_router = APIRouter(prefix="", tags=["search"])


class SearchRequestBody(BaseModel):
    """POST /search request body.

    At least one of ``query`` or ``text`` must be provided.  When both are given,
    ``query`` takes precedence and ``text`` is ignored.
    """

    query: SearchQuery | None = Field(
        None,
        description=("Fully structured IQL query.  Takes precedence over 'text' when both are provided."),
    )
    text: str | None = Field(
        None,
        description=(
            "Free-text query; promoted to a structured SearchQuery via IQL translation.  "
            "Ignored when 'query' is also set."
        ),
    )
    user_id: str = Field(
        ...,
        description="Yupp user ID of the caller.  All results are scoped to this user.",
    )

    @model_validator(mode="after")
    def _require_query_or_text(self) -> SearchRequestBody:
        if self.query is None and (self.text is None or self.text.strip() == ""):
            raise ValueError("At least one of 'query' or 'text' must be provided.")
        return self


@search_router.post(
    "/search",
    dependencies=[Depends(verify_api_key)],
)
async def search_route(request: SearchRequestBody) -> SearchResponse:
    """Search AHS entities by free text or a fully structured IQL query.

    Two call modes:

    **Text mode** — set ``text``, omit ``query``:
        The plain-English string is translated to a structured ``SearchQuery`` via a
        Haiku LLM call (IQL translation).  On any LLM or parse failure the translator
        falls back to a bare ``SearchQuery(q=text)`` so the call always succeeds.
        The resolved IQL is logged before execution.

    **Structured mode** — set ``query`` (with or without ``text``):
        The caller provides a fully built ``SearchQuery`` directly, bypassing LLM
        translation entirely.  When both ``text`` and ``query`` are provided, ``query``
        takes precedence.

    All results are scoped to the calling user (``user_id``).
    """
    try:
        if request.query is not None:
            # Structured mode: use the explicit IQL query as-is.
            query = request.query
        else:
            # Text mode: translate via Haiku, then log the resolved IQL.
            if not request.text:
                raise HTTPException(status_code=400, detail="Either 'query' or 'text' must be provided")
            query = await text_to_iql(request.text.strip())
            _logger.info(
                "search_iql_resolved",
                original_text=request.text,
                resolved_q=query.q,
                resolved_types=query.types,
                resolved_agent=query.agent,
                resolved_status=query.status,
                resolved_date_from=query.date_from,
                resolved_date_to=query.date_to,
                creator_user_id=query.creator_user_id,
                limit_per_type=query.limit_per_type,
            )

        return await execute_search(query, caller_user_id=request.user_id)

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        _logger.exception("search_failed", user_id=request.user_id)
        raise HTTPException(status_code=500, detail="Search failed due to an internal error") from None
