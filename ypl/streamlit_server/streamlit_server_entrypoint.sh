#!/bin/bash
set -e

source "$(pwd)/scripts/gcp_instance_id.sh"

export CONTAINER_INSTANCE_ID=$(get_container_instance_id)

# Create .streamlit directory if it doesn't exist
mkdir -p /app/.streamlit

# Determine redirect URI based on environment
if [ "$ENVIRONMENT" = "production" ]; then
    GOOGLE_AUTH_REDIRECT_URI="https://agent-streamlit-server-production-451082535721.us-east4.run.app/oauth2callback"
else
    GOOGLE_AUTH_REDIRECT_URI="https://agent-streamlit-server-staging-451082535721.us-east4.run.app/oauth2callback"
fi

# Generate secrets.toml from environment variables if auth credentials are provided
if [ -n "$GOOGLE_AUTH_CLIENT_ID" ] && [ -n "$GOOGLE_AUTH_CLIENT_SECRET" ]; then
    echo "Generating .streamlit/secrets.toml from environment variables..."
    cat > /app/.streamlit/secrets.toml <<EOF
[auth]
client_id = "${GOOGLE_AUTH_CLIENT_ID}"
client_secret = "${GOOGLE_AUTH_CLIENT_SECRET}"
redirect_uri = "${GOOGLE_AUTH_REDIRECT_URI}"
cookie_secret = "${GOOGLE_AUTH_COOKIE_SECRET}"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
EOF
    echo "Authentication configured successfully."
else
    echo "Warning: Google OAuth environment variables not set. Authentication will be disabled."
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
