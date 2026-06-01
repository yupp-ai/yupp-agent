"""External MCP registry + per-user OAuth/M2M grants.

DB models live in :mod:`ypl.db.external_mcp`.  This package carries the
runtime side:

- :mod:`crypto`       — Fernet helpers (``MCP_USER_GRANT_ENCRYPTION_KEY``).
- :mod:`oauth_client` — generic OAuth 2.0 code-exchange + refresh.
- :mod:`grants`       — grant lookup / token-refresh service.
- :mod:`routes`       — FastAPI router for OAuth start/callback + M2M setup.
- :mod:`resolver`     — builds ``.mcp.json`` entries for a session.

The Streamlit admin + user pages live under
``ypl/streamlit_server/pages/admin_external_mcps.py`` and ``my_mcps.py``.
"""
