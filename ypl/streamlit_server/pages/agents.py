"""Agents — browse, view, edit, and add agent definitions in the ``agents`` table."""

from __future__ import annotations
import html
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from ypl.agent_harness_service.common.model_options import (
    canonical_to_display_label,
    display_label_to_canonical,
    enumerate_model_options,
    parse_chain_entry,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import (
    Agent,
    AgentExecutorType,
    AgentSession,
    AgentSessionMessage,
)
from ypl.streamlit_server.auth import require_auth
from ypl.structured_logger import get_logger

logger = get_logger()

st.set_page_config(page_title="Agents", page_icon="🔮", layout="wide")
require_auth()

st.title("🔮 Agents")

# ── Model dropdown helpers ────────────────────────────────────────────────────

# All selectable model options as "[HARNESSED]/[RAW] provider/model" labels.
_MODEL_OPTION_LABELS: list[str] = [o.display_label for o in enumerate_model_options()]

_KEEP_CURRENT_SENTINEL = "↩ keep current (unrecognized)"


def _current_model_label(
    executor_type: AgentExecutorType | None,
    executor_model: str | None,
    config: dict[str, Any] | None,
) -> str | None:
    """Compute the dropdown label matching an agent's current model, or None.

    Prefers ``config.executor_config`` (the runtime-authoritative source); falls
    back to the ``executor_type``/``executor_model`` columns for legacy rows.
    """
    cfg = config or {}
    _ec = cfg.get("executor_config")
    exec_cfg: dict[str, Any] = _ec if isinstance(_ec, dict) else {}
    exec_type = (exec_cfg.get("type") or (executor_type.value if executor_type else "")).lower()
    if exec_type == "raw":
        model = exec_cfg.get("model") or executor_model
        canonical = f"raw:{model}" if model else None
    elif exec_type == "harnessed":
        harness = exec_cfg.get("model") or exec_cfg.get("harness") or executor_model
        llm = cfg.get("llm_model") or exec_cfg.get("llm_model")
        if harness and llm:
            canonical = f"harnessed:{harness}:{llm}"
        elif harness:
            canonical = f"harnessed:{harness}"
        else:
            canonical = None
    else:
        canonical = None
    if not canonical:
        return None
    try:
        label = canonical_to_display_label(canonical)
    except ValueError:
        return None
    return label if label in _MODEL_OPTION_LABELS else None


def _model_selection_to_fields(
    label: str, base_config: dict[str, Any] | None
) -> tuple[AgentExecutorType, str, dict[str, Any]]:
    """Map a chosen dropdown label to (executor_type, executor_model, config).

    The returned config has its ``executor_config`` (type/model/harness) and
    top-level ``llm_model`` replaced to match the selection, while preserving any
    other ``executor_config`` keys (compaction/history/retry) and config keys.
    """
    opt = parse_chain_entry(display_label_to_canonical(label))
    config: dict[str, Any] = dict(base_config or {})
    existing_exec = config.get("executor_config")
    preserved = (
        {k: v for k, v in existing_exec.items() if k not in ("type", "model", "harness", "llm_model")}
        if isinstance(existing_exec, dict)
        else {}
    )
    if opt.executor_type == "raw":
        config["executor_config"] = {**preserved, "type": "raw", "model": opt.llm_model}
        executor_model = opt.llm_model or ""
        config.pop("llm_model", None)
    else:
        config["executor_config"] = {**preserved, "type": "harnessed", "model": opt.harness, "harness": opt.harness}
        executor_model = opt.harness or ""
        if opt.llm_model:
            config["llm_model"] = opt.llm_model
        else:
            config.pop("llm_model", None)
    return AgentExecutorType(opt.executor_type.upper()), executor_model, config


# ── Timezone helper ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _to_local(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_PACIFIC)
    return local.strftime(f"%Y-%m-%d %H:%M:%S {local.strftime('%Z')}")


def _time_ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return f"{months} mo ago"


# ── Repo agent config helpers ────────────────────────────────────────────────

_AGENT_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "agent_harness_service" / "deploy" / "agent_configs"


@st.cache_data(ttl=300, show_spinner=False)
def _read_repo_agent_config(agent_name: str) -> dict[str, Any] | None:
    """Read config.json from the repo for a given agent name. Returns None if not found."""
    config_path = _AGENT_CONFIGS_DIR / agent_name / "config.json"
    if not config_path.is_file():
        return None
    try:
        return json.loads(config_path.read_text())  # type: ignore[no-any-return]
    except Exception:
        logger.warning("Failed to read repo agent config", agent_name=agent_name, exc_info=True)
        return None


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_all_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).where(col(Agent.deleted_at).is_(None)).order_by(col(Agent.name)))
        return list(result.all())


@retry_db
async def fetch_agent_by_id(agent_id: uuid.UUID) -> Agent | None:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).where(col(Agent.agent_id) == agent_id))
        return result.one_or_none()


@retry_db
async def fetch_agent_stats() -> dict[uuid.UUID, dict[str, Any]]:
    """Per-agent aggregates: total sessions, total messages, last active."""
    async with get_async_session_read_replica() as session:
        sess_q = select(
            col(AgentSession.agent_id),
            func.count(col(AgentSession.agent_session_id)).label("total_sessions"),
        ).group_by(col(AgentSession.agent_id))
        sess_rows = list((await session.exec(sess_q)).all())

        msg_q = (
            select(
                col(AgentSession.agent_id),
                func.count(col(AgentSessionMessage.agent_session_message_id)).label("total_messages"),
                func.max(col(AgentSessionMessage.created_at)).label("last_active"),
            )
            .join(AgentSession, col(AgentSessionMessage.agent_session_id) == col(AgentSession.agent_session_id))
            .group_by(col(AgentSession.agent_id))
        )
        msg_rows = list((await session.exec(msg_q)).all())

    stats: dict[uuid.UUID, dict[str, Any]] = {}
    for agent_id, total_sessions in sess_rows:
        stats.setdefault(agent_id, {})["total_sessions"] = total_sessions
    for agent_id, total_messages, last_active in msg_rows:
        stats.setdefault(agent_id, {})["total_messages"] = total_messages
        stats.setdefault(agent_id, {})["last_active"] = last_active
    return stats


# ── Mutations ────────────────────────────────────────────────────────────────


@retry_db
async def update_agent_fields(
    agent_id: uuid.UUID,
    *,
    display_name: str,
    description: str | None,
    executor_type: AgentExecutorType,
    executor_model: str | None,
    additional_system_prompt: str | None,
    config: dict[str, Any] | None,
    creator_user_id: str | None,
    agent_user_id: str | None,
) -> tuple[bool, str]:
    async with get_async_session() as session:
        agent = (await session.exec(select(Agent).where(col(Agent.agent_id) == agent_id))).one_or_none()
        if agent is None:
            return False, f"Agent {agent_id} not found."

        agent.display_name = display_name
        agent.description = description
        agent.executor_type = executor_type
        agent.executor_model = executor_model
        agent.additional_system_prompt = additional_system_prompt
        agent.config = config
        agent.creator_user_id = creator_user_id or None
        agent.agent_user_id = agent_user_id or None
        session.add(agent)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            return False, f"Update failed: {exc.orig}"
    return True, "Saved."


@retry_db
async def insert_agent(
    *,
    name: str,
    display_name: str,
    description: str | None,
    executor_type: AgentExecutorType,
    executor_model: str | None,
    additional_system_prompt: str | None,
    config: dict[str, Any] | None,
    creator_user_id: str | None,
    agent_user_id: str | None,
) -> tuple[bool, str, str | None]:
    async with get_async_session() as session:
        existing = (await session.exec(select(Agent.agent_id).where(col(Agent.name) == name))).one_or_none()
        if existing is not None:
            return False, f"An agent with name '{name}' already exists.", None

        new_id = uuid.uuid4()
        row = Agent(
            agent_id=new_id,
            name=name,
            display_name=display_name,
            description=description,
            executor_type=executor_type,
            executor_model=executor_model,
            additional_system_prompt=additional_system_prompt,
            config=config,
            creator_user_id=creator_user_id or None,
            agent_user_id=agent_user_id or None,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            return False, f"Insert failed: {exc.orig}", None
    return True, f"Agent '{name}' created.", str(new_id)


# ── Cached wrappers ──────────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _cached_agents() -> list[dict[str, Any]]:
    """Return all agents as plain dicts (cache-safe)."""
    agents = run_coroutine_in_lit_worker(fetch_all_agents(), timeout=30)
    return [
        {
            "agent_id": str(a.agent_id),
            "name": a.name,
            "display_name": a.display_name,
            "description": a.description,
            "executor_type": a.executor_type.value if a.executor_type else None,
            "executor_model": a.executor_model,
            "config": a.config,
            "additional_system_prompt": a.additional_system_prompt,
            "creator_user_id": a.creator_user_id,
            "agent_user_id": a.agent_user_id,
            "created_at": a.created_at,
            "modified_at": a.modified_at,
        }
        for a in agents
    ]


@st.cache_data(ttl=30, show_spinner=False)
def _cached_agent_stats() -> dict[str, dict[str, Any]]:
    raw = run_coroutine_in_lit_worker(fetch_agent_stats(), timeout=30)
    return {str(k): v for k, v in raw.items()}


def _refresh_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── Browse / Edit ────────────────────────────────────────────────────────────


def _render_browse() -> None:
    agents = _cached_agents()
    stats = _cached_agent_stats()

    if not agents:
        st.info("No agents found.")
        return

    # Filters
    filter_cols = st.columns([1.5, 2, 3])
    with filter_cols[0]:
        exec_type_options = ["(all)"] + [t.value for t in AgentExecutorType]
        selected_exec_type = st.selectbox("Executor type", exec_type_options, key="agents_exec_type")
    with filter_cols[1]:
        agent_name_options = ["(all)"] + [a["name"] for a in agents]
        selected_agent_name = st.selectbox("Agent", agent_name_options, key="agents_name_filter")
    with filter_cols[2]:
        keyword_input = st.text_input(
            "Keyword", key="agents_keyword", placeholder="Search name, display name, description…"
        )

    keyword_lower = keyword_input.strip().lower() if keyword_input else ""

    filtered = agents
    if selected_exec_type != "(all)":
        filtered = [a for a in filtered if a["executor_type"] == selected_exec_type]
    if selected_agent_name != "(all)":
        filtered = [a for a in filtered if a["name"] == selected_agent_name]
    if keyword_lower:
        filtered = [
            a
            for a in filtered
            if keyword_lower in (a["display_name"] or "").lower()
            or keyword_lower in (a["name"] or "").lower()
            or keyword_lower in (a["description"] or "").lower()
        ]

    st.caption(f"{len(filtered)} agent(s)")

    # Header row
    _BLUE_BOLD = 'style="color:#1976d2;font-weight:bold;font-size:0.85em;"'
    h_exec, h_name, h_desc, h_tools, h_subagents, h_action = st.columns([1.4, 1.5, 2, 2.4, 2.4, 0.8])
    with h_exec:
        st.markdown(f"<span {_BLUE_BOLD}>Executor</span>", unsafe_allow_html=True)
    with h_name:
        st.markdown(f"<span {_BLUE_BOLD}>Agent</span>", unsafe_allow_html=True)
    with h_desc:
        st.markdown(f"<span {_BLUE_BOLD}>Description</span>", unsafe_allow_html=True)
    with h_tools:
        st.markdown(f"<span {_BLUE_BOLD}>Tool Permissions</span>", unsafe_allow_html=True)
    with h_subagents:
        st.markdown(f"<span {_BLUE_BOLD}>Allowed Subagents</span>", unsafe_allow_html=True)
    with h_action:
        st.markdown(f"<span {_BLUE_BOLD}>Action</span>", unsafe_allow_html=True)
    st.divider()

    for a in filtered:
        agent_id_str = a["agent_id"]
        s = stats.get(agent_id_str, {})
        total_sess = s.get("total_sessions", 0)
        total_msgs = s.get("total_messages", 0)
        last_active = s.get("last_active")
        last_active_str = f"{_to_local(last_active)} ({_time_ago(last_active)})" if last_active else "—"

        db_config = a["config"]
        repo_config = _read_repo_agent_config(a["name"])
        config = db_config if db_config is not None else repo_config
        config_source = "DB" if db_config is not None else "repo"

        exec_config = (config or {}).get("executor_config", {})
        executor_type = exec_config.get("type") or (a["executor_type"] or "—")
        executor_model = exec_config.get("harness") or exec_config.get("model") or a["executor_model"] or "—"

        tool_permissions: dict[str, str] = (config or {}).get("tool_permissions", {}) or {}
        allowed_subagents: list[str] = (config or {}).get("allowed_subagents", []) or []
        rest_config = (
            {k: v for k, v in (config or {}).items() if k not in ("tool_permissions", "allowed_subagents")}
            if config
            else {}
        )

        # Layout
        r1_exec, r1_name, r1_desc, r1_tools, r1_subagents, r1_action = st.columns([1.4, 1.5, 2, 2.4, 2.4, 0.8])
        with r1_exec:
            st.markdown(f"`{executor_type}`<br>`{executor_model}`", unsafe_allow_html=True)
        with r1_name:
            st.markdown(
                f'<span style="font-size:1.15em;font-weight:bold;">{html.escape(a["display_name"])}</span>'
                f"<br>`{html.escape(a['name'])}`",
                unsafe_allow_html=True,
            )
        with r1_desc:
            st.caption(a["description"] or "—")
        with r1_tools:
            if tool_permissions:
                st.markdown(", ".join(f"`{k}`:`{v}`" for k, v in tool_permissions.items()))
            else:
                st.caption("—")
        with r1_subagents:
            if allowed_subagents:
                st.markdown(", ".join(f"`{sub}`" for sub in allowed_subagents))
            else:
                st.caption("—")
        with r1_action:
            if st.button("Edit", key=f"agent_edit_btn_{agent_id_str}"):
                st.query_params["agent_id"] = agent_id_str
                st.rerun()

        st.caption(f"{total_sess} sessions · {total_msgs} messages · last active {last_active_str}")

        if a["additional_system_prompt"]:
            with st.expander("Additional system prompt", expanded=False):
                st.markdown(a["additional_system_prompt"])

        if rest_config:
            with st.expander(f"Full config ({config_source})", expanded=False):
                st.json(rest_config)
        st.divider()


def _render_edit(agent_id: uuid.UUID) -> None:
    if st.button("← Back to all agents", key="agent_edit_back"):
        st.query_params.clear()
        st.rerun()

    with st.spinner("Loading agent…"):
        agent = run_coroutine_in_lit_worker(fetch_agent_by_id(agent_id), timeout=30)
    if agent is None:
        st.error(f"Agent not found: `{agent_id}`")
        return

    st.subheader(f"Edit agent: {agent.display_name}")
    st.caption(f"`{agent.name}` · `{agent.agent_id}`")
    st.markdown(f"Created: `{_to_local(agent.created_at)}` · Modified: `{_to_local(agent.modified_at)}`")

    repo_config = _read_repo_agent_config(agent.name)
    if repo_config is not None:
        st.caption("ℹ️ A repo `config.json` exists for this agent — DB config takes precedence at runtime.")

    config_str_initial = json.dumps(agent.config, indent=2) if agent.config else ""

    with st.form(f"agent_edit_form_{agent.agent_id}"):
        # Read-only name; renaming an agent is risky — handled separately if needed
        st.text_input("Name (immutable)", value=agent.name, disabled=True, key=f"name_ro_{agent.agent_id}")

        display_name = st.text_input("Display name", value=agent.display_name)
        description = st.text_area("Description", value=agent.description or "", height=80)

        # Model dropdown: a single "[HARNESSED]/[RAW] provider/model" picker that
        # sets executor_type + executor_model + config.executor_config together.
        _current_label = _current_model_label(agent.executor_type, agent.executor_model, agent.config)
        if _current_label is None:
            _model_options = [_KEEP_CURRENT_SENTINEL, *_MODEL_OPTION_LABELS]
            _model_index = 0
            st.caption(
                f"Current model `{agent.executor_type.value if agent.executor_type else '—'}` / "
                f"`{agent.executor_model or '—'}` isn't in the known list — pick one to change it, "
                "or keep current."
            )
        else:
            _model_options = _MODEL_OPTION_LABELS
            _model_index = _MODEL_OPTION_LABELS.index(_current_label)
        selected_model_label = st.selectbox(
            "Model",
            options=_model_options,
            index=_model_index,
            help="Executor type + provider/model. Harnessed (default) uses the CLI's own default model.",
        )

        st.markdown("**Additional system prompt** — appended to the system prompt at runtime.")
        additional_system_prompt = st.text_area(
            "Additional system prompt",
            value=agent.additional_system_prompt or "",
            height=240,
            label_visibility="collapsed",
            help="Markdown is supported. Leave blank to clear.",
        )

        st.markdown("**Config (JSON)** — stored in the `agents.config` JSONB column.")
        config_str = st.text_area(
            "Config JSON",
            value=config_str_initial,
            height=240,
            label_visibility="collapsed",
            help="Must be valid JSON or empty.",
        )

        creator_user_id = st.text_input("Creator user ID", value=agent.creator_user_id or "")
        agent_user_id = st.text_input(
            "Agent user ID (FK to users.user_id)",
            value=agent.agent_user_id or "",
            help="The agent's own user identity, if it has one. Leave blank if not applicable.",
        )

        submitted = st.form_submit_button("Save changes", type="primary")

    if not submitted:
        return

    # Parse config JSON
    parsed_config: dict[str, Any] | None
    config_str_clean = config_str.strip()
    if not config_str_clean:
        parsed_config = None
    else:
        try:
            parsed_config = json.loads(config_str_clean)
        except json.JSONDecodeError as exc:
            st.error(f"Config JSON is invalid: {exc}")
            return
        if not isinstance(parsed_config, dict):
            st.error("Config JSON must be an object (dict) at the top level.")
            return

    if not display_name.strip():
        st.error("Display name is required.")
        return

    # Apply the model dropdown selection over the (possibly edited) config JSON.
    # The "keep current" sentinel leaves the existing executor fields untouched.
    if selected_model_label == _KEEP_CURRENT_SENTINEL:
        executor_type = agent.executor_type
        executor_model = agent.executor_model
        final_config = parsed_config
    else:
        executor_type, executor_model, final_config = _model_selection_to_fields(selected_model_label, parsed_config)

    success, message = run_coroutine_in_lit_worker(
        update_agent_fields(
            agent_id=agent.agent_id,
            display_name=display_name.strip(),
            description=description.strip() or None,
            executor_type=executor_type,
            executor_model=executor_model,
            additional_system_prompt=additional_system_prompt.strip() or None,
            config=final_config,
            creator_user_id=creator_user_id.strip() or None,
            agent_user_id=agent_user_id.strip() or None,
        ),
        timeout=15,
    )
    if success:
        st.toast(message, icon="✅")
        _refresh_and_rerun()
    else:
        st.error(message)


def _render_add() -> None:
    st.subheader("Add new agent")
    st.caption(
        "Creates a row in the ``agents`` table. To wire the agent into a runtime executor, "
        "you may also need a matching directory under "
        "``ypl/agent_harness_service/deploy/agent_configs/<name>/``."
    )

    with st.form("agent_add_form", clear_on_submit=False):
        name = st.text_input("Name", help="URL-safe slug, unique. Cannot be changed once created.").strip()
        display_name = st.text_input("Display name").strip()
        description = st.text_area("Description", height=80).strip()

        _default_model_label = canonical_to_display_label("harnessed:claude-code-cli")
        selected_model_label = st.selectbox(
            "Model",
            options=_MODEL_OPTION_LABELS,
            index=_MODEL_OPTION_LABELS.index(_default_model_label),
            help="Executor type + provider/model. Harnessed (default) uses the CLI's own default model.",
        )

        additional_system_prompt = st.text_area(
            "Additional system prompt (optional)",
            height=200,
            help="Markdown is appended to the agent's system prompt at runtime.",
        ).strip()

        config_str = st.text_area(
            "Config JSON (optional)",
            value="",
            height=200,
            help="Stored in agents.config JSONB. Must be a valid JSON object, or leave blank.",
        ).strip()

        creator_user_id = st.text_input("Creator user ID (optional)").strip()
        agent_user_id = st.text_input(
            "Agent user ID (optional, FK to users.user_id)",
            help="If the agent has its own user record, paste the user_id here.",
        ).strip()

        submitted = st.form_submit_button("Create agent", type="primary")

    if not submitted:
        return

    if not name:
        st.error("Name is required.")
        return
    if not display_name:
        st.error("Display name is required.")
        return

    parsed_config: dict[str, Any] | None
    if not config_str:
        parsed_config = None
    else:
        try:
            parsed_config = json.loads(config_str)
        except json.JSONDecodeError as exc:
            st.error(f"Config JSON is invalid: {exc}")
            return
        if not isinstance(parsed_config, dict):
            st.error("Config JSON must be an object (dict) at the top level.")
            return

    # Apply the model dropdown selection over the (optional) config JSON.
    executor_type, executor_model, final_config = _model_selection_to_fields(selected_model_label, parsed_config)

    success, message, new_id = run_coroutine_in_lit_worker(
        insert_agent(
            name=name,
            display_name=display_name,
            description=description or None,
            executor_type=executor_type,
            executor_model=executor_model or None,
            additional_system_prompt=additional_system_prompt or None,
            config=final_config,
            creator_user_id=creator_user_id or None,
            agent_user_id=agent_user_id or None,
        ),
        timeout=15,
    )
    if success:
        st.toast(message, icon="✅")
        if new_id:
            st.success(f"Created agent_id: `{new_id}`")
        _refresh_and_rerun()
    else:
        st.error(message)


# ── Main ─────────────────────────────────────────────────────────────────────

qp = st.query_params
url_agent_id = qp.get("agent_id", None)

if url_agent_id:
    try:
        aid = uuid.UUID(url_agent_id.strip())
    except ValueError:
        st.error(f"Invalid agent ID: `{url_agent_id}`")
        st.stop()
    _render_edit(aid)
    st.stop()

tab_browse, tab_add = st.tabs(["Browse", "Add Agent"])

with tab_browse:
    _render_browse()

with tab_add:
    _render_add()
