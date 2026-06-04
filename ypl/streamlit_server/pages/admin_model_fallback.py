"""Admin Model Fallback — view and edit the global rate-limit fallback chain.

When a harnessed (subscription) agent turn hits a rate / usage limit, the turn is
restarted with the next model in this ordered chain. Admin-only.
"""

from __future__ import annotations

import streamlit as st
from ypl.agent_harness_service.common.model_options import (
    DEFAULT_FALLBACK_CHAIN,
    canonical_to_display_label,
    display_label_to_canonical,
    enumerate_model_options,
    get_fallback_chain,
    set_fallback_chain,
)
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.streamlit_server.auth import require_admin_role, require_auth
from ypl.structured_logger import get_logger

logger = get_logger()

st.set_page_config(page_title="Model Fallback", page_icon="⚠️", layout="wide")
require_auth()
require_admin_role()

st.title("⚠️ Model Fallback Chain")
st.caption(
    "Global, ordered fallback used when a harnessed (subscription) agent turn hits a rate / usage limit. "
    "The turn restarts with the next entry in this list. Keep a `[RAW]` direct-API model last as a no-limit "
    "fallback. Changes take effect within ~30 seconds."
)

_DRAFT_KEY = "model_fallback_draft"
_OPTION_LABELS: list[str] = [o.display_label for o in enumerate_model_options()]


def _load_draft() -> list[str]:
    """Initialize the editable draft from the persisted chain (once per session)."""
    if _DRAFT_KEY not in st.session_state:
        chain = run_coroutine_in_lit_worker(get_fallback_chain(), timeout=15)
        st.session_state[_DRAFT_KEY] = list(chain)
    current: list[str] = st.session_state[_DRAFT_KEY]
    return current


def _set_draft(entries: list[str]) -> None:
    st.session_state[_DRAFT_KEY] = entries


draft = _load_draft()

st.subheader("Current chain")
if not draft:
    st.info("The chain is empty — add at least one model below.")

for idx, entry in enumerate(draft):
    try:
        label = canonical_to_display_label(entry)
    except ValueError:
        label = f"⚠️ invalid: {entry}"
    cols = st.columns([0.6, 0.1, 0.1, 0.1])
    cols[0].markdown(f"**{idx + 1}.** `{label}`")
    if cols[1].button("⬆️", key=f"up_{idx}", disabled=idx == 0, help="Move up"):
        draft[idx - 1], draft[idx] = draft[idx], draft[idx - 1]
        _set_draft(draft)
        st.rerun()
    if cols[2].button("⬇️", key=f"down_{idx}", disabled=idx == len(draft) - 1, help="Move down"):
        draft[idx + 1], draft[idx] = draft[idx], draft[idx + 1]
        _set_draft(draft)
        st.rerun()
    if cols[3].button("🗑️", key=f"del_{idx}", help="Remove"):
        draft.pop(idx)
        _set_draft(draft)
        st.rerun()

st.divider()

st.subheader("Add a model")
add_cols = st.columns([0.7, 0.15])
new_label = add_cols[0].selectbox("Model to add", options=_OPTION_LABELS, label_visibility="collapsed")
if add_cols[1].button("➕ Add", use_container_width=True):
    canonical = display_label_to_canonical(new_label)
    if canonical in draft:
        st.warning(f"`{new_label}` is already in the chain.")
    else:
        draft.append(canonical)
        _set_draft(draft)
        st.rerun()

st.divider()

save_cols = st.columns([0.2, 0.2, 0.6])
if save_cols[0].button("💾 Save chain", type="primary", use_container_width=True):
    if not draft:
        st.error("Cannot save an empty chain.")
    else:
        try:
            run_coroutine_in_lit_worker(set_fallback_chain(list(draft)), timeout=15)
            st.toast("Fallback chain saved.", icon="✅")
            logger.info("Fallback chain updated via Streamlit", chain=list(draft))
        except ValueError as exc:
            st.error(f"Invalid chain: {exc}")

if save_cols[1].button("↩ Reset to default", use_container_width=True):
    _set_draft(list(DEFAULT_FALLBACK_CHAIN))
    st.rerun()

with st.expander("Default chain (hardcoded)"):
    for entry in DEFAULT_FALLBACK_CHAIN:
        st.markdown(f"- `{canonical_to_display_label(entry)}`")
