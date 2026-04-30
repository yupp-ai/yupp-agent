"""Streamlit Hub for yupp-agent — Agent pages only."""

from pathlib import Path

import streamlit as st

from ypl.db.all_models import *  # noqa
from ypl.streamlit_server.auth import auth_required, is_allowed_email, is_auth_configured

st.set_page_config(
    page_title="Agentic Couch Hub",
    page_icon="🛋️",
    layout="wide",
)


def login_screen() -> None:
    css_file = Path(__file__).parent / "styles" / "login.css"
    with open(css_file) as f:
        css_content = f.read()
    st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-size: 16rem; text-align: center; line-height: 1.3; "
        "padding: 0.4em 0 0.1em 0; overflow: visible'>🛋️</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='font-size: 2rem; font-weight: 700; text-align: center; "
        "margin: 1rem 0 2rem 0'>Agentic Couch Hub</div>",
        unsafe_allow_html=True,
    )
    st.button("Sign in with Google", on_click=st.login, type="primary")


def access_denied_screen() -> None:
    st.title("🛋️ Agentic Couch Hub")
    st.error("Access Denied")
    email = st.user.email if st.user is not None else "Unknown"
    st.warning(
        f"Your account ({email}) is not a registered user. Ask an admin to create a user record for this email address."
    )
    st.button("Log out", on_click=st.logout)


def auth_misconfigured_screen() -> None:
    css_file = Path(__file__).parent / "styles" / "login.css"
    if css_file.exists():
        with open(css_file) as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    st.title("🔒 Authentication Required")
    st.error("This deployment requires Google OAuth, but it is not configured.")
    st.markdown(
        "Set the following in `/data/ahs/.env` and restart `ahs-streamlit`:\n\n"
        "- `GOOGLE_AUTH_CLIENT_ID`\n"
        "- `GOOGLE_AUTH_CLIENT_SECRET`\n"
        "- `GOOGLE_AUTH_REDIRECT_URI` (e.g. `https://lit.agcouch.com/oauth2callback`)\n"
        "- `GOOGLE_AUTH_COOKIE_SECRET`\n\n"
        "Only emails that already exist in the `users` table can log in — "
        "create a user record for yourself before signing in."
    )


AUTH_ENABLED = is_auth_configured()
AUTH_REQUIRED = auth_required()

if AUTH_REQUIRED and not AUTH_ENABLED:
    auth_misconfigured_screen()
    st.stop()

if AUTH_ENABLED:
    if st.user is None or not st.user.is_logged_in:
        login_screen()
        st.stop()

    if st.user is None or not is_allowed_email(st.user.email):
        access_denied_screen()
        st.stop()

    st.title("🛋️ Agentic Couch Hub")
    st.markdown(f"Welcome, {st.user.name}! 👋")
    st.markdown("<div style='margin-bottom: 2rem;'></div>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown(f"**Logged in as:** {st.user.email}")
        st.button("Log out", on_click=st.logout)
else:
    st.title("🛋️ Agentic Couch Hub")
    st.warning(
        "⚠️ **Authentication Not Configured** - Running in development mode. "
        "See AUTHENTICATION.md for setup instructions."
    )
    st.markdown("Welcome to the Agentic Couch Hub. Select a page below to get started.")

pages = {
    "Agents": [
        {
            "name": "Agent Harness Console",
            "link": "agent_harness_console",
            "emoji": "💬",
            "description": (
                "Browse Agent Harness sessions and messages. "
                "View conversation threads grouped by session and search by agent or session ID."
            ),
        },
        {
            "name": "Agents",
            "link": "agents",
            "emoji": "🔮",
            "description": (
                "Browse and edit registered agents in the database. "
                "View and modify all DB fields, including additional system prompts, "
                "executor type, executor model, and config JSON. Add brand-new agents."
            ),
        },
        {
            "name": "Feedbacks",
            "link": "feedbacks",
            "emoji": "⭐",
            "description": (
                "Browse user feedback on agent sessions and messages. "
                "Filter by rating, agent, or session trigger; click through to the "
                "originating session in the Agent Harness Console."
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
    ],
    "Admin": [
        {
            "name": "Slack Agents",
            "link": "admin_slack_agents",
            "emoji": "💬",
            "description": (
                "Manage registered Slack bots: view, add, edit, and disable entries in the "
                "slack_agents table. Admin-only."
            ),
        },
        {
            "name": "Roles & Permissions",
            "link": "admin_roles_permissions",
            "emoji": "🔐",
            "description": (
                "View roles and their permissions, edit role permissions, "
                "and manage which users belong to each role. Admin-only."
            ),
        },
        {
            "name": "Users",
            "link": "admin_users",
            "emoji": "👥",
            "description": (
                "Browse, add, and edit user records. Assign roles to existing users "
                "or create new users for agent onboarding. Admin-only."
            ),
        },
        {
            "name": "MCP Tokens",
            "link": "admin_mcp_tokens",
            "emoji": "🔑",
            "description": (
                "Issue, view, and revoke MCP developer tokens. "
                "Generate new yupp_dev_* tokens for authorized users. Admin-only."
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
