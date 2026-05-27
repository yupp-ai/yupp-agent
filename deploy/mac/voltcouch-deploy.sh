#!/usr/bin/env bash
#
# voltcouch-deploy — kick a deploy on the admin daemon and stream its log.
#
# Hits POST /api/deploy on the local admin daemon (which is the same code
# path as clicking "Deploy" in admin.voltcouch.com), then polls the job and
# tails its log lines until the build + health gate finish.
#
# The audit entry lands in ~/.voltcouch-admin/deploys.jsonl, and the running
# stack reflects the new HEAD if the deploy succeeds.
#
# Usage:
#   voltcouch-deploy                 # pull current branch (main) + rebuild + up
#   voltcouch-deploy <branch>        # checkout that branch first
#   voltcouch-deploy --rollback <sha>
#   voltcouch-deploy --tail          # don't trigger anything, just tail the
#                                    # last running job

set -euo pipefail

ADMIN_HOST="${ADMIN_HOST:-http://127.0.0.1:8099}"
ENV_FILE="${AHS_ENV_FILE:-$HOME/deploy/voltcouch/config/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "no .env at $ENV_FILE" >&2; exit 2
fi

# Use the poetry venv's python (has itsdangerous installed) rather than the
# system python. Falls back to plain python3 if the venv isn't there.
PYTHON_CANDIDATES=(
    "$HOME/workspace/yupp-agent/.venv/bin/python"
    "$HOME/deploy/voltcouch/yupp-agent/.venv/bin/python"
    "$(command -v python3 || true)"
)
PYTHON=""
for p in "${PYTHON_CANDIDATES[@]}"; do
    [[ -x "$p" ]] || continue
    if "$p" -c "import itsdangerous" 2>/dev/null; then
        PYTHON="$p"; break
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "no python with itsdangerous found; try: pip install itsdangerous" >&2
    exit 2
fi

SECRET="$(awk -F= '/^ADMIN_SESSION_SECRET=/{print $2; exit}' "$ENV_FILE")"
EMAIL="$(awk -F= '/^ADMIN_ALLOWED_EMAILS=/{print $2; exit}' "$ENV_FILE" | cut -d, -f1)"

if [[ -z "$SECRET" || -z "$EMAIL" ]]; then
    echo "ADMIN_SESSION_SECRET / ADMIN_ALLOWED_EMAILS missing in $ENV_FILE" >&2
    exit 2
fi

# Forge a signed session cookie exactly like Starlette's SessionMiddleware
# would emit after a real OAuth round-trip.
cookie() {
    "$PYTHON" - "$SECRET" "$EMAIL" <<'PY'
import base64, json, sys
from itsdangerous import TimestampSigner
secret, email = sys.argv[1], sys.argv[2]
signer = TimestampSigner(secret)
data = base64.b64encode(json.dumps({"email": email, "name": "cli"}).encode())
print(signer.sign(data).decode())
PY
}

# Trigger the deploy / rollback and capture the job_id.
trigger() {
    local mode="$1"; shift
    local url
    case "$mode" in
        deploy)
            url="$ADMIN_HOST/api/deploy"
            if [[ $# -gt 0 && -n "$1" ]]; then
                url+="?branch=$1"
            fi
            ;;
        rollback)
            [[ -n "${1:-}" ]] || { echo "rollback needs a sha"; exit 2; }
            url="$ADMIN_HOST/api/rollback?sha=$1"
            ;;
        *) echo "unknown mode: $mode"; exit 2 ;;
    esac
    curl -fsS -X POST -H "Cookie: session=$(cookie)" "$url" \
        | "$PYTHON" -c 'import sys, json; print(json.load(sys.stdin)["job_id"])'
}

# Poll /api/jobs/<id> and print new log lines until status != running.
follow() {
    local job_id="$1"
    local C; C="$(cookie)"
    "$PYTHON" - "$ADMIN_HOST" "$C" "$job_id" <<'PY'
import json, sys, time, urllib.request
host, cookie, job_id = sys.argv[1:4]

def fetch(path):
    req = urllib.request.Request(f"{host}{path}")
    req.add_header("Cookie", f"session={cookie}")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

sent = 0
KIND_COLOR = {
    "cmd":   "\033[33m", "out":   "",
    "err":   "\033[31m", "warn":  "\033[33m",
    "info":  "\033[36m", "rc":    "\033[90m",
    "health":"\033[32m", "done":  "\033[1;32m",
}
RESET = "\033[0m"
while True:
    job = fetch(f"/api/jobs/{job_id}")
    for ln in job["log"][sent:]:
        c = KIND_COLOR.get(ln["kind"], "")
        print(f"{c}[{ln['kind']:6}]{RESET} {ln['text']}")
    sent = len(job["log"])
    if job["status"] in ("succeeded", "failed"):
        dur = round(job.get("duration_s") or 0, 1)
        col = "\033[1;32m" if job["status"] == "succeeded" else "\033[1;31m"
        print(f"\n{col}── {job['status']}  exit={job['exit_code']}  duration={dur}s ──{RESET}")
        sys.exit(0 if job["status"] == "succeeded" else 1)
    time.sleep(2)
PY
}

# --- arg parsing ---
case "${1:-}" in
    --rollback)
        shift; JOB_ID="$(trigger rollback "${1:-}")"
        ;;
    --tail)
        JOB_ID="$(curl -fsS -H "Cookie: session=$(cookie)" \
            "$ADMIN_HOST/api/deploys?limit=1" \
            | "$PYTHON" -c 'import sys, json; d=json.load(sys.stdin)["deploys"]; print(d[0]["ts"] if d else "")')"
        if [[ -z "$JOB_ID" ]]; then echo "no recent deploy"; exit 0; fi
        echo "last deploy: $JOB_ID"; exit 0
        ;;
    -h|--help)
        sed -n '3,18p' "$0"; exit 0
        ;;
    *)
        JOB_ID="$(trigger deploy "${1:-}")"
        ;;
esac

echo "job: $JOB_ID  (daemon: $ADMIN_HOST)"
follow "$JOB_ID"
