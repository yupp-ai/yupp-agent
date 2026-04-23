# Single-Box Agent Sandbox Hardening

**Status:** Draft
**Date:** 2026-04-22
**Author:** Tian + Claude
**Context:** Single-box AHS deploy (`ahs-mono-prod`) where Postgres, Redis, AHS, and agent subprocesses all share the same VM. An agent followed the `/db-schema-change` skill and wrote an orphan revision (`1acae4917762`) into prod's `alembic_version`, breaking the next deploy. Investigation found the bwrap sandbox was configured on but effectively off in runtime.

## What we're protecting

The blast radius of a rogue/mistaken agent subprocess on `ahs-mono-prod` today:

| Surface | Exposed because |
|---|---|
| Prod Postgres (`127.0.0.1:5432`) | Colocated; `POSTGRES_CONNECTION_*` env vars in parent; no egress firewall |
| Prod Redis (`127.0.0.1:6379`) | Colocated; env vars; no egress firewall |
| `/opt/yupp-agent/.env` (prod secrets) | Full filesystem access as `ahs` user |
| GCE metadata server (`169.254.169.254`) | Any network-enabled process can fetch instance creds |
| Other agents' workspaces under `/data/ahs/sessions/…` | Shared user, no per-session uid |
| AHS itself (`127.0.0.1:8090`) | Agent could call its own admin endpoints |

This is not "the DB leaked once" — it's "every running agent has a credential-spray surface."

## Non-goals

- Moving prod Postgres off the VM (intentionally single-box).
- Per-agent VMs or containers for isolation at the session level.
- Defending against sophisticated sandbox escapes (kernel exploits, etc.) — we're targeting accidents and prompt-injection-level misbehavior, not a determined attacker with root.

## Design

Three layers, each independently useful:

1. **Make bwrap actually work** (OS fix) — unblock unprivileged user namespaces on Ubuntu 24.
2. **Tighten the bwrap policy** — drop `/opt`, scrub env, narrow FS mounts.
3. **Selective egress firewall** — allow HTTPS to the world, deny the sensitive localhost ports + metadata server.

Together: an agent subprocess can run `gh`, `curl https://…`, `pip install`, `npm test` — but cannot `psql -h localhost`, cannot read `/opt/yupp-agent/.env`, and cannot reach `169.254.169.254`.

## Layer 1 — Unblock bwrap at the OS level

**Problem:** Ubuntu 24.04 ships with `kernel.apparmor_restrict_unprivileged_userns=1`. This blocks the `ahs` user from creating user namespaces, which bwrap requires. Result: `sudo -u ahs bwrap --ro-bind /usr /usr -- /bin/true` fails, and `use_bwrap` silently short-circuits to False in `runner.py`.

**Fix:** one sysctl drop-in, installed idempotently by `install.sh`.

```bash
# install.sh (Ubuntu 24 prerequisite for bubblewrap)
if [ ! -f /etc/sysctl.d/99-bwrap-userns.conf ]; then
    cat > /etc/sysctl.d/99-bwrap-userns.conf <<'EOF'
# Allow unprivileged user namespaces so bubblewrap can sandbox agent
# subprocesses. Required on Ubuntu 24.04+. See
# ypl/agent_harness_service/executors/sandbox.py.
kernel.apparmor_restrict_unprivileged_userns = 0
EOF
    sysctl -p /etc/sysctl.d/99-bwrap-userns.conf
fi
```

**Acceptance test in install.sh** (mirrors `_bwrap_system_mounts()` — `/lib` and `/lib64` are symlinks on modern Ubuntu, so they must be recreated inside the sandbox or the dynamic linker can't resolve and execvp returns the misleading "No such file or directory"):
```bash
sudo -u ahs bwrap \
    --ro-bind /usr /usr \
    --symlink usr/lib /lib \
    --symlink usr/lib64 /lib64 \
    -- /usr/bin/true \
  || { echo "bwrap still unusable by ahs user — aborting install"; exit 1; }
```

Also add a runtime alarm: if `bwrap_available()` succeeds but the actual wrap call raises, log a loud error with `"sandbox not enforcing — refusing to launch agent"` and fail the session. No silent fallback to unsandboxed (current behavior).

## Layer 2 — Tighten the bwrap policy

Changes to `ypl/agent_harness_service/executors/sandbox.py`:

### 2a. Drop the `/opt` bulk mount

Replace with surgical binds that expose exactly what the Claude CLI / Codex / poetry-shipped tools need. Candidates — all **read-only**:
- The Python interpreter's real path (`realpath $(which python)` → something like `/opt/python3.12.12/`)
- The Claude CLI's install location (usually under `/usr` already)
- Nothing else from `/opt`

If a tool truly needs the AHS repo venv at runtime, bind only that venv directory, not `/opt/yupp-agent` wholesale.

**Explicitly forbid** in code review: no directory that contains `.env`, `.env.*`, or `secrets/`.

### 2b. Scrub env vars at sandbox exec time

Current code passes parent env through. Change the wrapper to start from an empty env and whitelist only what the agent needs:

```python
ALLOWED_ENV_KEYS = {
    "HOME", "USER", "PATH", "LANG", "LC_ALL", "TERM",
    "CLAUDE_CODE_OAUTH_TOKEN",         # Claude CLI auth
    "AHS_SESSION_ID", "AHS_AGENT_NAME", # harness-injected context
    # add per-agent needs explicitly
}
DENY_PATTERNS = ("POSTGRES_", "REDIS_", "_SECRET", "_TOKEN_ENCRYPTION_KEY", "_API_KEY")
```

If a var matches `DENY_PATTERNS`, it's dropped even if it somehow sneaks onto the allow list. Belt-and-suspenders.

### 2c. Per-session writable workspace, no cross-session visibility

Already mostly there — each session gets `/data/ahs/sessions/<sid>/`. Make sure bwrap binds **only that session's directory** RW, not the parent. That way agent A can't read agent B's workspace.

### 2d. Mount only `/data/ahs/repos/<repo>` the session actually needs

Today the workspace has symlinks pointing into `/data/ahs/repos/yupp-agent` and `/data/ahs/repos/yupp-mind`. Per-session, bind the single repo the agent is authorized for, not all of them.

## Layer 3 — Selective egress firewall

Implement with **iptables OWNER match on a dedicated sandbox uid**. This is the "keep gh working, kill psql" knob.

### 3a. Create a dedicated uid

```bash
# install.sh
if ! id ahs-sandbox >/dev/null 2>&1; then
    useradd -r -M -s /usr/sbin/nologin -U ahs-sandbox
fi
```

### 3b. Have bwrap drop privileges to ahs-sandbox

Inside the sandbox, we already get a new user namespace. Map the outer `ahs-sandbox` uid to an inner uid (or just run the wrapper as `ahs-sandbox` via `sudo -u`). The key point: the *effective uid visible to the kernel outside the user namespace* is `ahs-sandbox`, so `-m owner --uid-owner` rules match.

### 3c. Add iptables rules (install.sh, idempotent)

```bash
# Block ahs-sandbox uid from sensitive local services + metadata
iptables -C OUTPUT -m owner --uid-owner ahs-sandbox -d 127.0.0.1 \
    -p tcp -m multiport --dports 5432,6379,8090,8501 -j REJECT 2>/dev/null \
  || iptables -A OUTPUT -m owner --uid-owner ahs-sandbox -d 127.0.0.1 \
    -p tcp -m multiport --dports 5432,6379,8090,8501 -j REJECT

iptables -C OUTPUT -m owner --uid-owner ahs-sandbox -d 169.254.169.254 \
    -j REJECT 2>/dev/null \
  || iptables -A OUTPUT -m owner --uid-owner ahs-sandbox -d 169.254.169.254 \
    -j REJECT

# Persist across reboots (iptables-persistent or netfilter-persistent)
apt-get install -y iptables-persistent
netfilter-persistent save
```

Ports to block: **5432** (postgres), **6379** (redis), **8090** (AHS), **8501** (streamlit). Port **22** (SSH) stays open from localhost for the agent's own git push over SSH if it uses `localhost:22` — usually not, but no reason to block it.

Destination to block: `169.254.169.254` (GCE/AWS metadata — major creds-leak avenue via IMDS).

### 3d. Acceptance tests after install

```bash
# These must SUCCEED:
sudo -u ahs-sandbox curl -s -o /dev/null -w '%{http_code}\n' https://github.com
sudo -u ahs-sandbox curl -s -o /dev/null -w '%{http_code}\n' https://api.anthropic.com

# These must FAIL (connection refused / rejected):
sudo -u ahs-sandbox timeout 3 psql -h 127.0.0.1 -U postgres -c 'select 1' 2>&1 | grep -q 'refused\|unreachable'
sudo -u ahs-sandbox timeout 3 curl -s -o /dev/null http://169.254.169.254/computeMetadata/v1/ 2>&1 | grep -q 'refused\|unreachable'
```

## Migration + rollout

1. Land install.sh changes on a branch; run on a clean staging VM end-to-end first.
2. On `ahs-mono-prod`:
   - Apply Layer 1 (sysctl) first. Verify bwrap actually wraps next agent launch. Logs should now say `bwrap=True`.
   - Then Layer 2 (tighten bwrap config). Restart ahs-mono. Watch for session failures (things previously working via `/opt` that now aren't accessible).
   - Then Layer 3 (firewall). Restart ahs-mono. Watch for sessions that break when their scripts reach for `localhost:5432`.
3. After a week of clean runs, revisit bringing `/db-schema-change` back — now safe because agent sandbox can't reach prod DB regardless of env creds.

## Known limitations (documented, not fixed)

- An agent with a root exploit escapes everything above. We're not defending against that.
- Cross-agent snooping via filesystem is addressed by per-session binds, but if two agents share the same `/data/ahs/repos/yupp-agent` RW workspace for parallel work, one could still stomp on the other. Sessions should bind repos read-only and work in a per-session clone.
- Egress firewall is uid-scoped — any subprocess the agent spawns that somehow keeps the original uid stays blocked. If the agent manages to `setuid` to another local user, the OWNER match no longer applies. Hence the dedicated `ahs-sandbox` uid being non-login and unprivileged; the only way out is a kernel bug.

## Follow-ups (not in this plan)

- Stand up a per-session scratch Postgres on `:5433` so `/db-schema-change` has a legitimate target. Only useful after Layer 3 proves agents cannot reach `:5432`.
- Figure out a minimal AppArmor profile for the Claude CLI process itself as a belt-and-suspenders layer on top of bwrap.
- Move to cgroup v2 + nftables eventually — iptables OWNER match is fine for now but not the future direction.
