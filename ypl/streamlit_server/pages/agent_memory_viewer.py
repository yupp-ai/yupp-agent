"""Agent Memory Viewer — browse MEMORY artifacts grouped by scope.

This page is a thin Streamlit client over the AHS artifact REST API. It
**never** touches GCS or the database directly: every list / fetch / search
call goes through ``/ahs/artifacts`` with the service ``X-API-Key``. The
viewer presents itself as an admin caller (no ``X-User-ID`` /
``X-AHS-Agent-Name`` headers), so AHS skips the per-caller MEMORY read
filter and returns everything — that's the point of this page.

The sidebar groups by scope:

* **Topics**  — global MEMORY rows (``scope=topic``).
* **Users**   — per-user notebooks (``scope=user``); a subject picker
  selects which user's memories to show.
* **Agents**  — per-agent notebooks (``scope=agent``); a subject picker
  selects which agent's memories to show.

Display form (used everywhere — sidebar, headings, search results):

    u:{subject}:{slug}    a:{subject}:{slug}    t:{slug}
"""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import streamlit as st
from ypl.backend.config import settings
from ypl.streamlit_server.auth import require_auth

st.set_page_config(page_title="Agent Memory Viewer", layout="wide")
require_auth()

st.title("Agent Memory Viewer")


# ── REST client ──────────────────────────────────────────────────────────
#
# The viewer hits AHS as an admin (no caller-identity headers). The only
# header is ``X-API-Key`` for the service-to-service shared-secret gate;
# without an ``X-User-ID`` / ``X-AHS-Agent-Name`` pair, AHS treats us as
# admin and returns MEMORY rows across every scope/subject. That's the
# desired behaviour for a viewer.

_BASE_URL = (settings.AGENT_HARNESS_SERVICE_BASE_URL or "").rstrip("/")
_API_KEY = settings.AGENT_HARNESS_SERVICE_API_KEY


class _AHSError(Exception):
    """Raised when an AHS call fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"AHS {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _ahs_client() -> httpx.Client:
    if not _BASE_URL:
        raise _AHSError(503, "AGENT_HARNESS_SERVICE_BASE_URL is not configured.")
    if not _API_KEY:
        raise _AHSError(503, "AGENT_HARNESS_SERVICE_API_KEY is not configured.")
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"X-API-Key": _API_KEY},
        timeout=30.0,
    )


def _ahs_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    with _ahs_client() as http:
        resp = http.get(path, params=params)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise _AHSError(resp.status_code, str(detail))
    return resp.json()


def _ahs_get_bytes(path: str) -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for a raw artifact endpoint."""
    with _ahs_client() as http:
        resp = http.get(path)
    if resp.status_code >= 400:
        raise _AHSError(resp.status_code, resp.text or "request failed")
    return resp.content, resp.headers.get("content-type", "application/octet-stream")


# ── Cached fetchers ──────────────────────────────────────────────────────
#
# Streamlit's ``cache_data`` keys on argument values, so the cache version
# nonce (bumped by the Refresh button) isolates this page from the global
# cache. TTLs are short — memory writes happen at human cadence.


_LIST_PAGE_LIMIT = 200  # AHS hard ceiling; matches Query(le=200) on the route.


def _list_all_memory_artifacts() -> list[dict[str, Any]]:
    """Return every visible MEMORY artifact (paginated under the hood)."""
    artifacts: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _ahs_get_json(
            "/ahs/artifacts",
            params={"type": "MEMORY", "limit": _LIST_PAGE_LIMIT, "offset": offset},
        )
        chunk = cast(list[dict[str, Any]], page.get("artifacts", []))
        artifacts.extend(chunk)
        if len(chunk) < _LIST_PAGE_LIMIT:
            break
        offset += _LIST_PAGE_LIMIT
        # Defensive cap so a buggy upstream can't force unbounded paging.
        if offset >= 5000:
            break
    return artifacts


@st.cache_data(ttl=120, show_spinner=False)
def _load_memory_index(_cache_ver: int = 0) -> list[dict[str, Any]]:
    """Cached list of all MEMORY artifacts (latest visible version per slug)."""
    return _list_all_memory_artifacts()


@st.cache_data(ttl=300, show_spinner=False)
def _load_artifact_content(artifact_id: str, _cache_ver: int = 0) -> str:
    """Fetch and decode the raw content of a single artifact (5 min cache)."""
    body, _ = _ahs_get_bytes(f"/ahs/artifacts/{artifact_id}")
    return body.decode("utf-8", errors="replace")


@st.cache_data(ttl=120, show_spinner=False)
def _load_versions(slug: str, scope: str, subject: str | None, _cache_ver: int = 0) -> list[dict[str, Any]]:
    """List every version of a (scope, subject?, slug) MEMORY tuple."""
    params: dict[str, Any] = {"type": "MEMORY", "scope": scope}
    if subject is not None:
        params["subject"] = subject
    payload = _ahs_get_json(f"/ahs/artifacts/by-slug/{slug}/versions", params=params)
    return cast(list[dict[str, Any]], payload.get("versions", []))


def _search_memory(q: str, scope: str | None) -> list[dict[str, Any]]:
    """Substring search across MEMORY title, description, slug, inline content."""
    params: dict[str, Any] = {"q": q, "type": "MEMORY", "limit": 100}
    if scope:
        params["scope"] = scope
    payload = _ahs_get_json("/ahs/artifacts/search", params=params)
    return cast(list[dict[str, Any]], payload.get("artifacts", []))


# ── Display form helpers ─────────────────────────────────────────────────


def _display_form(scope: str | None, subject: str | None, slug: str | None) -> str:
    """Return the canonical ``u:/a:/t:`` display string for a MEMORY row."""
    slug_str = slug or "?"
    if scope == "topic":
        return f"t:{slug_str}"
    if scope == "user":
        return f"u:{subject or '?'}:{slug_str}"
    if scope == "agent":
        return f"a:{subject or '?'}:{slug_str}"
    return slug_str


def _row_key(art: dict[str, Any]) -> str:
    """Stable widget key for an artifact row in session_state."""
    scope = art.get("memory_scope") or "?"
    subject = art.get("memory_scope_subject") or ""
    slug = art.get("named_slug") or art.get("artifact_id") or ""
    return f"{scope}|{subject}|{slug}"


def _format_time(dt: str | datetime | None) -> str:
    if not dt:
        return "—"
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
        except ValueError:
            return dt[:16].replace("T", " ")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.strftime("%Y-%m-%d %H:%M")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%M")


# ── Index assembly ───────────────────────────────────────────────────────


def _group_by_scope(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket rows by ``memory_scope`` (topic/user/agent), dropping non-MEMORY."""
    buckets: dict[str, list[dict[str, Any]]] = {"topic": [], "user": [], "agent": []}
    for r in rows:
        scope = r.get("memory_scope")
        if scope in buckets:
            buckets[scope].append(r)
    # Stable ordering: most recently created first within each bucket.
    for bucket in buckets.values():
        bucket.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return buckets


def _distinct_subjects(rows: list[dict[str, Any]]) -> list[str]:
    """Distinct, alphabetically sorted ``memory_scope_subject`` values."""
    subjects = {r.get("memory_scope_subject") for r in rows if r.get("memory_scope_subject")}
    return sorted(s for s in subjects if isinstance(s, str))


# ── Compact button styling (matches the legacy viewer's left-pane look) ─


st.markdown(
    """
    <style>
    /* Sidebar memory list buttons: compact, left-aligned, link-like */
    section[data-testid="stSidebar"] div.stButton > button {
        text-align: left;
        justify-content: flex-start;
        padding: 4px 8px;
        font-size: 13px;
        border: none;
        background: transparent;
        width: 100%;
    }
    section[data-testid="stSidebar"] div.stButton > button:hover {
        background: #e8e8e8;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Cache version nonce ──────────────────────────────────────────────────
#
# Bumped by the sidebar Refresh button. Threaded through every cached
# helper so we can bust just this page's caches without nuking the global
# Streamlit cache.

_cache_ver: int = st.session_state.get("memory_cache_ver", 0)


# ── Detail / content rendering ───────────────────────────────────────────


def _render_memory_detail(art: dict[str, Any]) -> None:
    """Right-panel: render a single MEMORY artifact with rendered/raw/versions tabs."""
    scope = art.get("memory_scope")
    subject = art.get("memory_scope_subject")
    slug = art.get("named_slug")
    artifact_id = art.get("artifact_id")
    title = _display_form(scope, subject, slug)

    st.subheader(title)
    meta_parts: list[str] = []
    if art.get("version") is not None:
        meta_parts.append(f"version {art['version']}")
    meta_parts.append(f"created {_format_time(art.get('created_at'))}")
    if art.get("creator_user_name"):
        meta_parts.append(f"user {art['creator_user_name']}")
    elif art.get("creator_user_id"):
        meta_parts.append(f"user {art['creator_user_id']}")
    if art.get("creator_agent_name"):
        meta_parts.append(f"agent {art['creator_agent_name']}")
    st.caption(" · ".join(p for p in meta_parts if p))

    if not artifact_id:
        st.error("Artifact has no id — cannot fetch content.")
        return

    try:
        content = _load_artifact_content(artifact_id, _cache_ver=_cache_ver)
    except _AHSError as exc:
        st.error(f"Failed to fetch content: {exc}")
        return

    tab_rendered, tab_raw, tab_versions = st.tabs(["Rendered", "Raw", "Versions"])

    with tab_rendered:
        st.markdown(content)

    with tab_raw:
        st.code(content, language="markdown")

    with tab_versions:
        if slug is None:
            st.caption("This artifact has no slug, so there is no version history.")
            return
        try:
            versions = _load_versions(slug, scope or "", subject, _cache_ver=_cache_ver)
        except _AHSError as exc:
            st.error(f"Failed to fetch versions: {exc}")
            return
        if not versions:
            st.caption("No version history.")
            return
        st.caption(f"{len(versions)} version(s)")
        # Newest first.
        for v in sorted(versions, key=lambda r: r.get("version") or 0, reverse=True):
            v_num = v.get("version")
            label = f"v{v_num} · {_format_time(v.get('created_at'))}"
            with st.expander(label, expanded=False):
                cols = st.columns(3)
                cols[0].caption(f"id: `{v.get('artifact_id')}`")
                if v.get("creator_user_name") or v.get("creator_user_id"):
                    cols[1].caption(f"user: {v.get('creator_user_name') or v.get('creator_user_id')}")
                if v.get("creator_agent_name"):
                    cols[2].caption(f"agent: {v['creator_agent_name']}")
                if st.button(
                    "Load this version",
                    key=f"load_version_{v.get('artifact_id')}",
                ):
                    st.session_state["selected_artifact_id"] = v.get("artifact_id")
                    st.rerun()


# ── Sidebar: scope-grouped picker ────────────────────────────────────────


def _sidebar_topic_section(rows: list[dict[str, Any]]) -> None:
    """Render the Topics section."""
    st.sidebar.markdown(f"### Topics ({len(rows)})")
    if not rows:
        st.sidebar.caption("No topic memories yet.")
        return
    selected_id = st.session_state.get("selected_artifact_id")
    for r in rows:
        is_selected = r.get("artifact_id") == selected_id
        prefix = "» " if is_selected else ""
        if st.sidebar.button(
            f"{prefix}t:{r.get('named_slug') or '?'}",
            key=f"sb_topic_{_row_key(r)}",
            use_container_width=True,
        ):
            st.session_state["selected_artifact_id"] = r.get("artifact_id")
            st.rerun()


def _sidebar_subject_section(
    label: str,
    scope: str,
    rows: list[dict[str, Any]],
    state_key: str,
) -> None:
    """Shared rendering for the Users / Agents sidebar sections."""
    subjects = _distinct_subjects(rows)
    st.sidebar.markdown(f"### {label} ({len(subjects)})")
    if not subjects:
        st.sidebar.caption(f"No {scope} memories yet.")
        return

    options = ["(select)"] + subjects
    current = st.session_state.get(state_key, "(select)")
    if current not in options:
        current = "(select)"
    chosen = st.sidebar.selectbox(
        f"{label[:-1]} subject",
        options=options,
        index=options.index(current),
        key=f"{state_key}_picker",
        label_visibility="collapsed",
    )
    st.session_state[state_key] = chosen
    if chosen == "(select)":
        return

    subject_rows = [r for r in rows if r.get("memory_scope_subject") == chosen]
    if not subject_rows:
        st.sidebar.caption(f"No memories under {chosen!r}.")
        return

    prefix_letter = "u" if scope == "user" else "a"
    selected_id = st.session_state.get("selected_artifact_id")
    for r in subject_rows:
        is_selected = r.get("artifact_id") == selected_id
        marker = "» " if is_selected else ""
        if st.sidebar.button(
            f"{marker}{prefix_letter}:{chosen}:{r.get('named_slug') or '?'}",
            key=f"sb_{scope}_{_row_key(r)}",
            use_container_width=True,
        ):
            st.session_state["selected_artifact_id"] = r.get("artifact_id")
            st.rerun()


# ── Page entry ───────────────────────────────────────────────────────────


with st.spinner("Loading agent memories…"):
    try:
        all_rows = _load_memory_index(_cache_ver=_cache_ver)
    except _AHSError as exc:
        st.error(f"Failed to list MEMORY artifacts: {exc}")
        st.stop()

buckets = _group_by_scope(all_rows)

if st.sidebar.button("🔄 Refresh", key="memory_refresh"):
    st.session_state["memory_cache_ver"] = _cache_ver + 1
    st.rerun()

st.sidebar.caption(
    f"{len(all_rows)} memories total · "
    f"{len(buckets['topic'])} topic / {len(buckets['user'])} user / {len(buckets['agent'])} agent"
)

_sidebar_topic_section(buckets["topic"])
st.sidebar.divider()
_sidebar_subject_section("Users", "user", buckets["user"], "memory_user_subject")
st.sidebar.divider()
_sidebar_subject_section("Agents", "agent", buckets["agent"], "memory_agent_subject")


# ── Main pane: Browse vs Search tabs ─────────────────────────────────────


tab_browse, tab_search = st.tabs(["Browse", "Search"])

with tab_browse:
    selected_id = st.session_state.get("selected_artifact_id")
    selected_row: dict[str, Any] | None = None
    if selected_id:
        selected_row = next((r for r in all_rows if r.get("artifact_id") == selected_id), None)
    if not selected_row:
        st.markdown(
            "<div style='color:#999; padding-top:80px; text-align:center;'>"
            "Pick a memory from the sidebar — Topics, Users, or Agents."
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        _render_memory_detail(selected_row)


with tab_search:
    col_query, col_scope, col_btn = st.columns([4, 1, 1])
    with col_query:
        search_query = st.text_input(
            "Search query",
            placeholder="Substring search across MEMORY artifacts…",
            key="memory_search_q",
        )
    with col_scope:
        scope_choice = st.selectbox(
            "Scope filter",
            options=["(any)", "topic", "user", "agent"],
            key="memory_search_scope",
        )
    with col_btn:
        st.markdown("<div style='padding-top:28px'></div>", unsafe_allow_html=True)
        do_search = st.button("Search", key="memory_search_btn")

    if do_search and search_query.strip():
        scope_param = None if scope_choice == "(any)" else scope_choice
        try:
            st.session_state["memory_search_results"] = _search_memory(search_query.strip(), scope_param)
            st.session_state["memory_search_error"] = None
        except _AHSError as exc:
            st.session_state["memory_search_results"] = []
            st.session_state["memory_search_error"] = str(exc)

    err = st.session_state.get("memory_search_error")
    if err:
        st.error(f"Search failed: {err}")

    results: list[dict[str, Any]] = st.session_state.get("memory_search_results", [])
    if results:
        st.caption(f"{len(results)} result(s)")
        for r in results:
            scope = r.get("memory_scope")
            subject = r.get("memory_scope_subject")
            slug = r.get("named_slug")
            display = _display_form(scope, subject, slug)
            v = r.get("version")
            heading = f"**{display}**" + (f"  (v{v})" if v is not None else "")
            with st.expander(heading):
                st.caption(f"created {_format_time(r.get('created_at'))}")
                if r.get("description"):
                    st.write(r["description"])
                if st.button(
                    "Open in Browse tab",
                    key=f"memory_search_open_{r.get('artifact_id')}",
                ):
                    st.session_state["selected_artifact_id"] = r.get("artifact_id")
                    st.rerun()
    elif do_search and search_query.strip():
        st.caption("No results.")
    else:
        st.caption("Enter a search query to look across MEMORY artifacts.")
