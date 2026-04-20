"""Agent Memory Viewer — Browse and search shared agent memory files."""

from __future__ import annotations
import re
from datetime import UTC, datetime
from typing import Any

import streamlit as st
from google.cloud import storage
from pydantic import BaseModel
from sqlalchemy import text
from ypl.backend.config import settings
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.streamlit_server.auth import require_auth

_BUCKET_NAME = settings.AGENT_MEMORY_BUCKET
_MEMORY_PREFIX = "memory/"

st.set_page_config(page_title="Agent Memory Viewer", layout="wide")
require_auth()

st.title("Agent Memory Viewer")


class MemoryFileInfo(BaseModel):
    topic: str
    blob_name: str
    size_bytes: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


# `from __future__ import annotations` makes all type hints lazy strings.
# Pydantic v2 needs model_rebuild() to resolve them before validation.
MemoryFileInfo.model_rebuild()


# ── GCS functions ─────────────────────────────────────────────────────────


@st.cache_resource
def _get_storage_client() -> storage.Client:
    """Cached GCS client singleton."""
    return storage.Client()


@st.cache_data(ttl=120, show_spinner=False)
def _list_memory_files_cached() -> list[dict[str, Any]]:
    """List all .md files under gs://yupp-agents/memory/. Returns serializable dicts for cache."""
    client = _get_storage_client()
    bucket = client.bucket(_BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=_MEMORY_PREFIX)

    files: list[dict[str, Any]] = []
    for blob in blobs:
        name: str = blob.name
        if not name.endswith(".md"):
            continue
        topic = name.removeprefix(_MEMORY_PREFIX).removesuffix(".md")
        if not topic:
            continue
        files.append(
            {
                "topic": topic,
                "blob_name": name,
                "size_bytes": blob.size or 0,
                "created_at": blob.time_created,
                "updated_at": blob.updated,
            }
        )

    files.sort(
        key=lambda f: f["updated_at"] or f["created_at"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return files


def _to_models(raw: list[dict[str, Any]]) -> list[MemoryFileInfo]:
    return [MemoryFileInfo(**d) for d in raw]


@st.cache_data(ttl=300, show_spinner=False)
def _read_memory_file_cached(blob_name: str) -> str:
    """Download and return the content of a memory file (cached 5 min)."""
    client = _get_storage_client()
    bucket = client.bucket(_BUCKET_NAME)
    blob = bucket.blob(blob_name)
    result: str = blob.download_as_text(encoding="utf-8")
    return result


# ── DB query functions ────────────────────────────────────────────────────


async def _search_sections_async(
    query: str,
    mode: str = "hybrid",
    topic_filter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search agent memory sections via the shared search module."""
    from ypl.backend.llm.agent_memory_search import search_agent_memory

    return await search_agent_memory(
        query=query,
        mode=mode,
        top_k=limit,
        topic=topic_filter,
        full_content=False,
    )


@retry_db
async def _list_sections_for_topic_async(topic: str) -> list[dict[str, Any]]:
    """List all live sections for a topic with metadata."""
    sql = """
        SELECT
            s.section_key,
            s.section_title,
            LENGTH(s.content) AS content_length,
            s.last_indexed_at,
            s.source_generation,
            EXISTS(
                SELECT 1 FROM agent_memory_section_embeddings e
                WHERE e.agent_memory_section_id = s.agent_memory_section_id
            ) AS has_embedding
        FROM agent_memory_sections s
        WHERE s.topic = :topic AND s.deleted_at IS NULL
        ORDER BY s.created_at ASC
    """

    async with get_async_session_read_replica() as session:
        result = await session.execute(text(sql), {"topic": topic})
        rows = result.all()

    return [dict(row._mapping) for row in rows]


@retry_db
async def _get_topic_stats_async() -> dict[str, dict[str, Any]]:
    """Aggregate section count + last_indexed_at per topic from DB."""
    sql = """
        SELECT
            s.topic,
            COUNT(*) AS section_count,
            MAX(s.last_indexed_at) AS last_indexed,
            MAX(s.source_generation) AS source_generation,
            COUNT(*) FILTER (
                WHERE EXISTS(
                    SELECT 1 FROM agent_memory_section_embeddings e
                    WHERE e.agent_memory_section_id = s.agent_memory_section_id
                )
            ) AS with_embeddings
        FROM agent_memory_sections s
        WHERE s.deleted_at IS NULL
        GROUP BY s.topic
    """

    async with get_async_session_read_replica() as session:
        result = await session.execute(text(sql))
        rows = result.all()

    return {
        row.topic: {
            "section_count": row.section_count,
            "last_indexed": row.last_indexed,
            "source_generation": row.source_generation,
            "with_embeddings": row.with_embeddings,
        }
        for row in rows
    }


@st.cache_data(ttl=120, show_spinner=False)
def _get_topic_stats() -> dict[str, dict[str, Any]]:
    return run_coroutine_in_lit_worker(_get_topic_stats_async())


@st.cache_data(ttl=60, show_spinner=False)
def _list_sections_for_topic(topic: str) -> list[dict[str, Any]]:
    return run_coroutine_in_lit_worker(_list_sections_for_topic_async(topic))


def _search_sections(
    query: str,
    mode: str = "hybrid",
    topic_filter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return run_coroutine_in_lit_worker(_search_sections_async(query, mode, topic_filter, limit))


# ── Formatting helpers ────────────────────────────────────────────────────


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_time(dt: datetime | str | None) -> str:
    if not dt:
        return "\u2014"
    if isinstance(dt, str):
        return dt[:16].replace("T", " ")
    return dt.strftime("%Y-%m-%d %H:%M")


# ── Compact button styling ───────────────────────────────────────────────

st.markdown(
    """
    <style>
    /* Left-panel topic buttons: compact, left-aligned, link-like */
    div[data-testid="stVerticalBlockBorderWrapper"] div.stButton > button {
        text-align: left;
        justify-content: flex-start;
        padding: 4px 8px;
        font-size: 13px;
        border: none;
        background: transparent;
        width: 100%;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div.stButton > button:hover {
        background: #e8e8e8;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _render_topic_detail(selected_file: MemoryFileInfo, topic_stats: dict[str, dict[str, Any]]) -> None:
    """Render the right-panel detail view for a selected topic."""
    st.subheader(selected_file.topic)
    st.caption(
        f"{_format_size(selected_file.size_bytes)} · "
        f"created {_format_time(selected_file.created_at)} · "
        f"updated {_format_time(selected_file.updated_at)}"
    )

    # Index status bar
    stats = topic_stats.get(selected_file.topic)
    if stats:
        cols = st.columns(4)
        cols[0].metric("Sections", stats["section_count"])
        cols[1].metric("With Embeddings", f"{stats['with_embeddings']}/{stats['section_count']}")
        cols[2].metric("Last Indexed", _format_time(stats.get("last_indexed")))
        gen = stats.get("source_generation")
        cols[3].metric("Generation", gen if gen is not None else "\u2014")
    else:
        st.info("Not indexed in DB yet.")

    # Load GCS content
    try:
        content = _read_memory_file_cached(selected_file.blob_name)
    except Exception as exc:
        st.error(f"Failed to read file: {exc}")
        return

    # Sub-tabs: Rendered / Sections / Raw
    sub_rendered, sub_sections, sub_raw = st.tabs(["Rendered", "Sections", "Raw"])

    with sub_rendered:
        rendered = re.sub(r"\A---\n.*?\n---\n*", "", content, count=1, flags=re.DOTALL)
        st.markdown(rendered)

    with sub_sections:
        try:
            sections = _list_sections_for_topic(selected_file.topic)
        except Exception:
            sections = []
            st.error("Failed to load sections from DB.")
        if not sections:
            st.caption("No indexed sections found for this topic.")
        else:
            st.caption(f"{len(sections)} sections")
            for sec in sections:
                title = sec["section_title"] or sec["section_key"]
                emb_badge = " [emb]" if sec["has_embedding"] else ""
                with st.expander(f"{title}{emb_badge}"):
                    c1, c2, c3 = st.columns(3)
                    c1.caption(f"Key: `{sec['section_key']}`")
                    c2.caption(f"Length: {sec['content_length']} chars")
                    c3.caption(f"Indexed: {_format_time(sec.get('last_indexed_at'))}")
                    if sec.get("source_generation") is not None:
                        st.caption(f"Generation: {sec['source_generation']}")

    with sub_raw:
        st.code(content, language="markdown")


# ── Load data ────────────────────────────────────────────────────────────

with st.spinner("Loading memory files from GCS..."):
    try:
        raw_files = _list_memory_files_cached()
    except Exception as exc:
        st.error(f"Failed to list memory files: {exc}")
        st.stop()

files = _to_models(raw_files)
try:
    topic_stats = _get_topic_stats()
except Exception:
    topic_stats = {}
    st.warning("Failed to load index stats from DB.")

# ── Top-level tabs ───────────────────────────────────────────────────────

tab_browse, tab_search = st.tabs(["Browse", "Search"])

# ── Browse tab ───────────────────────────────────────────────────────────

with tab_browse:
    if not files:
        st.info("No memory files found in gs://yupp-agents/memory/")
    else:
        left_col, right_col = st.columns([1, 3], gap="large")

        # ── Left panel: filter + topic list ───────────────────────────────

        with left_col:
            search = st.text_input(
                "Filter", placeholder="Filter topics...", label_visibility="collapsed", key="browse_filter"
            )

            if search:
                filtered = [f for f in files if search.lower() in f.topic.lower()]
            else:
                filtered = files

            if not filtered:
                st.caption("No topics matched.")
            else:
                st.caption(f"{len(filtered)} topics")

                for f in filtered:
                    is_selected = st.session_state.get("selected_topic") == f.topic
                    prefix = ">> " if is_selected else ""
                    stats = topic_stats.get(f.topic)
                    section_info = f" | {stats['section_count']}s" if stats else ""
                    if st.button(
                        f"{prefix}**{f.topic}**\n\n"
                        f"{_format_time(f.updated_at)} · {_format_size(f.size_bytes)}{section_info}",
                        key=f"topic_{f.topic}",
                        use_container_width=True,
                    ):
                        st.session_state["selected_topic"] = f.topic
                        st.rerun()

            if st.button("🔄 Refresh", help="Refresh", key="browse_refresh"):
                _list_memory_files_cached.clear()
                _read_memory_file_cached.clear()
                _get_topic_stats.clear()
                _list_sections_for_topic.clear()
                st.rerun()

        # ── Right panel: content view ─────────────────────────────────────

        with right_col:
            selected_topic: str | None = st.session_state.get("selected_topic")
            if not selected_topic:
                st.markdown(
                    "<div style='color:#999; padding-top:80px; text-align:center;'>Select a topic from the list</div>",
                    unsafe_allow_html=True,
                )
            else:
                selected_file = next((f for f in files if f.topic == selected_topic), None)
                if not selected_file:
                    st.warning(f"Topic '{selected_topic}' no longer exists.")
                else:
                    _render_topic_detail(selected_file, topic_stats)

# ── Search tab ───────────────────────────────────────────────────────────

with tab_search:
    col_query, col_mode, col_topic, col_btn = st.columns([3, 1, 1, 0.5])
    with col_query:
        search_query = st.text_input("Search query", placeholder="Search across all agent memories...", key="search_q")
    with col_mode:
        search_mode = st.selectbox("Mode", ["hybrid", "keyword", "semantic"], key="search_mode")
    with col_topic:
        topic_names = ["(all topics)"] + [f.topic for f in files]
        search_topic = st.selectbox("Topic filter", topic_names, key="search_topic_filter")
    with col_btn:
        st.markdown("<div style='padding-top:28px'></div>", unsafe_allow_html=True)
        do_search = st.button("Search", key="search_btn")

    if do_search and search_query:
        topic_f = None if search_topic == "(all topics)" else search_topic
        try:
            st.session_state["search_results"] = _search_sections(search_query, mode=search_mode, topic_filter=topic_f)
            st.session_state["search_error"] = None
        except Exception as exc:
            st.session_state["search_results"] = []
            st.session_state["search_error"] = str(exc)

    if st.session_state.get("search_error"):
        st.error(f"Search failed: {st.session_state['search_error']}")

    results: list[dict[str, Any]] = st.session_state.get("search_results", [])
    if results:
        st.caption(f"{len(results)} results")
        for r in results:
            title = r["section_title"] or r["section_key"]
            score_str = f"{r['score']:.4f}"
            snippet = r["content"]

            with st.expander(f"**{r['topic']}** / {title}  (score: {score_str})"):
                st.caption(f"Modified: {_format_time(r.get('modified_at'))}")
                st.markdown(snippet)
                if st.button("Select topic", key=f"goto_{r['topic']}_{r['section_key']}"):
                    st.session_state["selected_topic"] = r["topic"]
                    st.rerun()
                st.caption("Switch to the Browse tab to view in context.")
    elif do_search:
        st.caption("No results found.")
    else:
        st.caption("Enter a search query to search across indexed agent memories.")
