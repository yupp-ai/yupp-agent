#!/bin/bash
# Streamlit entrypoint for the selfhosted (one-box) deployment.
#
# Reads OAuth settings from env vars (loaded from /data/ahs/.env by the
# systemd unit) and generates ${HOME}/.streamlit/secrets.toml, then execs
# streamlit. When ENVIRONMENT=selfhosted the Streamlit app refuses to serve
# anything until these secrets are present, so this script fails fast with a
# loud error instead of silently starting in dev mode.
set -euo pipefail

SECRETS_DIR="${HOME}/.streamlit"
SECRETS_FILE="${SECRETS_DIR}/secrets.toml"

if [[ "${ENVIRONMENT:-}" == "selfhosted" ]]; then
    missing=()
    [[ -z "${GOOGLE_AUTH_CLIENT_ID:-}" ]] && missing+=("GOOGLE_AUTH_CLIENT_ID")
    [[ -z "${GOOGLE_AUTH_CLIENT_SECRET:-}" ]] && missing+=("GOOGLE_AUTH_CLIENT_SECRET")
    [[ -z "${GOOGLE_AUTH_REDIRECT_URI:-}" ]] && missing+=("GOOGLE_AUTH_REDIRECT_URI")
    [[ -z "${GOOGLE_AUTH_COOKIE_SECRET:-}" ]] && missing+=("GOOGLE_AUTH_COOKIE_SECRET")
    if (( ${#missing[@]} > 0 )); then
        echo "ERROR: selfhosted Streamlit requires these env vars: ${missing[*]}" >&2
        echo "Add them to /data/ahs/.env and restart ahs-streamlit." >&2
        exit 1
    fi
fi

if [[ -n "${GOOGLE_AUTH_CLIENT_ID:-}" && -n "${GOOGLE_AUTH_CLIENT_SECRET:-}" ]]; then
    mkdir -p "${SECRETS_DIR}"
    umask 077
    cat > "${SECRETS_FILE}" <<EOF
[auth]
client_id = "${GOOGLE_AUTH_CLIENT_ID}"
client_secret = "${GOOGLE_AUTH_CLIENT_SECRET}"
redirect_uri = "${GOOGLE_AUTH_REDIRECT_URI}"
cookie_secret = "${GOOGLE_AUTH_COOKIE_SECRET}"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
EOF
    chmod 600 "${SECRETS_FILE}"
    echo "Wrote ${SECRETS_FILE}"
fi

STREAMLIT_BIN="${VENV_BIN:-/opt/yupp-agent/.venv/bin}/streamlit"

# --server.fileWatcherType=none disables Streamlit's LocalSourcesWatcher.
# That watcher reloads modules via `del sys.modules[...]` whenever a watched
# source file's mtime changes (e.g. mid-deploy `git pull` before the systemd
# restart, or any post-start file touch). On a SQLModel codebase that
# behaviour redefines table-mapped classes against an unchanged
# `SQLModel.metadata`, raising:
#     sqlalchemy.exc.InvalidRequestError: Table 'agents' is already defined
#     for this MetaData instance.
# Production never benefits from hot-reload (we restart the service on deploy),
# so it's strictly a footgun here. See ypl/streamlit_server/AGENTS.md.
exec "${STREAMLIT_BIN}" run \
    ypl/streamlit_server/app.py \
    --server.port "${PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.fileWatcherType none \
    --browser.gatherUsageStats false
