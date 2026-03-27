"""Streamlit Hub for yupp-agent — Agent pages only."""

from pathlib import Path

import streamlit as st

from ypl.db.all_models import *  # noqa
from ypl.streamlit_server.auth import is_allowed_email, is_auth_configured

st.set_page_config(
    page_title="Yupp Agent Hub",
    page_icon="🤖",
    layout="wide",
)


def login_screen() -> None:
    css_file = Path(__file__).parent / "styles" / "login.css"
    with open(css_file) as f:
        css_content = f.read()
    st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)

    logo_path = Path(__file__).parent / "lit_logo.png"
    if logo_path.exists():
        st.image(str(logo_path))
    st.markdown("<div style='margin-bottom: 40px;'></div>", unsafe_allow_html=True)
    st.button("Log in with Google", on_click=st.login, type="primary")


def access_denied_screen() -> None:
    st.title("🤖 Yupp Agent Hub")
    st.error("Access Denied")
    email = st.user.email if st.user is not None else "Unknown"
    st.warning(f"Your account ({email}) does not have access. Only yupp.ai emails are authorized.")
    st.button("Log out", on_click=st.logout)


AUTH_ENABLED = is_auth_configured()

if AUTH_ENABLED:
    if st.user is None or not st.user.is_logged_in:
        login_screen()
        st.stop()

    if st.user is None or not is_allowed_email(st.user.email):
        access_denied_screen()
        st.stop()

    st.title("🤖 Yupp Agent Hub")
    st.markdown(f"Welcome, {st.user.name}! 👋")
    st.markdown("<div style='margin-bottom: 2rem;'></div>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown(f"**Logged in as:** {st.user.email}")
        st.button("Log out", on_click=st.logout)
else:
    st.title("🤖 Yupp Agent Hub")
    st.warning(
        "⚠️ **Authentication Not Configured** - Running in development mode. "
        "See AUTHENTICATION.md for setup instructions."
    )
    st.markdown("Welcome to the Yupp Agent Hub. Select a page below to get started.")

pages = {
    "Agents": [
        {
            "name": "Agent Harness Console",
            "link": "agent_harness_console",
            "emoji": "🔮",
            "description": (
                "Browse Agent Harness sessions, messages, and feedbacks. "
                "View conversation threads grouped by session, search by agent or session ID, "
                "and review feedback signals."
            ),
        },
        {
            "name": "Agent Projects",
            "link": "agent_projects",
            "emoji": "📁",
            "description": (
                "View and manage agent projects and tasks. "
                "Browse project hierarchies, track task dependencies and status, "
                "monitor budget spending, and view shared state."
            ),
        },
        {
            "name": "Agent Schedules",
            "link": "agent_schedule",
            "emoji": "📅",
            "description": "View and manage scheduled agent calls, recurring schedules, and run history.",
        },
        {
            "name": "Agent Memory Viewer",
            "link": "agent_memory_viewer",
            "emoji": "🗃️",
            "description": (
                "Browse shared agent memory files stored in GCS. "
                "View topic files with metadata, timestamps, sizes, and content."
            ),
        },
        {
            "name": "Agent Harness Dashboard",
            "link": "agent_harness_dashboard",
            "emoji": "📊",
            "description": (
                "Operational analytics for agent sessions, costs, latency, errors, "
                "feedback, and schedule health. Track usage trends and performance metrics."
            ),
        },
        {
            "name": "Agent Anomaly Detection",
            "link": "agent_anomaly_dashboard",
            "emoji": "🔍",
            "description": (
                "Identify agents with high cost and latency variance. "
                "View coefficient of variation, jitter scores, and variance bands "
                "over configurable time windows to catch regressions early."
            ),
        },
    ],
}

# Sort pages within each group
for group_name, group_pages in pages.items():
    pages[group_name] = sorted(group_pages, key=lambda p: p["name"].lower())

st.markdown(
    """
    <style>
    .page-item { display: flex; align-items: center; margin-bottom: 15px; }
    .page-emoji-container { flex: 0 0 80px; display: flex; align-items: center; justify-content: center; }
    .page-emoji { font-size: 4em; }
    .page-emoji-link {
        text-decoration: none !important; cursor: pointer;
        display: block; transition: transform 0.2s ease;
    }
    .page-emoji-link:hover { transform: scale(1.1); }
    .page-content { flex: 1; padding-left: 15px; }
    .page-name-link {
        color: #262730 !important; text-decoration: none !important;
        font-size: 1.25em; font-weight: 600; line-height: 1.2;
        display: block; margin-bottom: 8px;
    }
    .page-name-link:hover { color: #FF4B4B !important; text-decoration: none !important; }
    .page-description { font-size: 0.9em; color: #666; line-height: 1.4; margin: 0; }
    </style>
    """,
    unsafe_allow_html=True,
)

for group_name, group_pages in pages.items():
    with st.expander(group_name, expanded=True):
        cols = st.columns(3)
        for idx, page in enumerate(group_pages):
            with cols[idx % 3]:
                st.markdown(
                    f"""
                    <div class="page-item">
                        <div class="page-emoji-container">
                            <a href="{page["link"]}" class="page-emoji-link" target="_self">
                                <div class="page-emoji">{page["emoji"]}</div>
                            </a>
                        </div>
                        <div class="page-content">
                            <a href="{page["link"]}" class="page-name-link" target="_self">{page["name"]}</a>
                            <div class="page-description">{page["description"]}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
