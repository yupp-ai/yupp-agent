#!/bin/bash
set -e

# Legacy GCP helper — present on the prod / staging Cloud Run images but
# pruned from the open-source tree (de-yupp phase 5).  Source it when it
# exists so production behaviour is unchanged; fall back to a deterministic
# value otherwise so the entrypoint still boots inside the one-box / Mac /
# self-hosted image.
if [ -f "$(pwd)/scripts/gcp_instance_id.sh" ]; then
    # shellcheck disable=SC1091
    source "$(pwd)/scripts/gcp_instance_id.sh"
    export CONTAINER_INSTANCE_ID=$(get_container_instance_id)
else
    export CONTAINER_INSTANCE_ID="${CONTAINER_INSTANCE_ID:-local}"
fi

# Create .streamlit directory if it doesn't exist
mkdir -p /app/.streamlit

# Determine redirect URI.
#
# Resolution order:
#   1. STREAMLIT_GOOGLE_AUTH_REDIRECT_URI — explicit override.  Set this to
#      your public callback URL, e.g.
#      ``https://agent-ui.example.com/oauth2callback`` so OAuth keeps working
#      behind a public hostname (Cloudflare tunnel, reverse proxy, etc.).
#   2. Empty — combined with empty GOOGLE_AUTH_CLIENT_ID/SECRET below, this
#      skips secrets.toml generation and leaves Streamlit unauthenticated.
#      Only safe for localhost-only deploys.
if [ -n "${STREAMLIT_GOOGLE_AUTH_REDIRECT_URI:-}" ]; then
    GOOGLE_AUTH_REDIRECT_URI="${STREAMLIT_GOOGLE_AUTH_REDIRECT_URI}"
else
    GOOGLE_AUTH_REDIRECT_URI=""
fi

# Generate secrets.toml from environment variables if auth credentials are
# provided.  All three (client id, secret, redirect URI) must be set —
# Streamlit's auth helper refuses to bind without a redirect URI, so a
# half-configured deployment is worse than disabling auth outright.
if [ -n "${GOOGLE_AUTH_CLIENT_ID:-}" ] && [ -n "${GOOGLE_AUTH_CLIENT_SECRET:-}" ] && [ -n "${GOOGLE_AUTH_REDIRECT_URI:-}" ]; then
    echo "Generating .streamlit/secrets.toml from environment variables..."
    cat > /app/.streamlit/secrets.toml <<EOF
[auth]
client_id = "${GOOGLE_AUTH_CLIENT_ID}"
client_secret = "${GOOGLE_AUTH_CLIENT_SECRET}"
redirect_uri = "${GOOGLE_AUTH_REDIRECT_URI}"
cookie_secret = "${GOOGLE_AUTH_COOKIE_SECRET}"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
EOF
    echo "Authentication configured (redirect_uri=${GOOGLE_AUTH_REDIRECT_URI})."
else
    echo "Warning: Google OAuth environment variables not fully set."
    echo "  GOOGLE_AUTH_CLIENT_ID:       ${GOOGLE_AUTH_CLIENT_ID:+set}"
    echo "  GOOGLE_AUTH_CLIENT_SECRET:   ${GOOGLE_AUTH_CLIENT_SECRET:+set}"
    echo "  GOOGLE_AUTH_REDIRECT_URI:    ${GOOGLE_AUTH_REDIRECT_URI:+set}"
    echo "  Streamlit authentication will be disabled."
fi

# Run streamlit server.
#
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
exec streamlit run /app/ypl/streamlit_server/app.py \
    --server.port=${PORT:-8080} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.fileWatcherType=none \
    --browser.serverAddress=0.0.0.0 \
    --browser.gatherUsageStats=false
