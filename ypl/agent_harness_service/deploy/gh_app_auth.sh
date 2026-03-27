#!/bin/bash
#
# Authenticate gh CLI as a GitHub App installation.
# Usage: sudo -u ahs bash gh_app_auth.sh
#
set -euo pipefail

APP_ID="${GITHUB_APP_ID:-2880548}"
INSTALLATION_ID="${GITHUB_APP_INSTALLATION_ID:-110573475}"
KEY_FILE="${GITHUB_APP_PRIVATE_KEY_PATH:-/data/ahs/github-app-key.pem}"

if [ ! -f "$KEY_FILE" ]; then
    echo "ERROR: Private key not found at $KEY_FILE"
    exit 1
fi

# Build JWT
NOW=$(date +%s)
IAT=$((NOW - 60))
EXP=$((NOW + 600))

HEADER=$(echo -n '{"alg":"RS256","typ":"JWT"}' | openssl base64 -e -A | tr "+/" "-_" | tr -d "=")
PAYLOAD=$(echo -n "{\"iat\":${IAT},\"exp\":${EXP},\"iss\":\"${APP_ID}\"}" | openssl base64 -e -A | tr "+/" "-_" | tr -d "=")
SIGNATURE=$(echo -n "${HEADER}.${PAYLOAD}" | openssl dgst -sha256 -sign "$KEY_FILE" | openssl base64 -e -A | tr "+/" "-_" | tr -d "=")
JWT="${HEADER}.${PAYLOAD}.${SIGNATURE}"

# Exchange JWT for installation token
echo ">>> Requesting installation token..."
RESPONSE=$(curl -s -X POST \
    -H "Authorization: Bearer ${JWT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/${INSTALLATION_ID}/access_tokens")

TOKEN=$(echo "$RESPONSE" | jq -r .token)

if [ "$TOKEN" = "null" ] || [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to get token. Response:"
    echo "$RESPONSE" | jq .
    exit 1
fi

# Auth gh CLI
echo "$TOKEN" | gh auth login --with-token
# Configure git to use gh as the credential helper for HTTPS pushes.
# Without this, git push fails with "Invalid username or token".
gh auth setup-git
echo ">>> Done. Verifying..."
gh auth status
