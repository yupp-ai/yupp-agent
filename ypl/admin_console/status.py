"""Read-only data collection for the admin console.

All collection is async; subprocess calls are non-blocking. Results are cached
in-memory for a short TTL to avoid hammering docker/git/postgres on refresh.
"""

from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast

import psutil
from sqlalchemy import text

logger = logging.getLogger(__name__)

# The admin daemon controls a *deploy* checkout — a dedicated clone whose
# only job is to track origin/main and back the running docker stack. NEVER
# the dev workspace where the code is edited (which would point `git pull`
# at a dirty feature branch).
#
# Resolution order:
#   1. VOLTCOUCH_DEPLOY_REPO env var (explicit)
#   2. ~/deploy/voltcouch/yupp-agent if it exists (install.sh layout)
#   3. fall back to wherever this file lives (dev mode, single-tree setups)
_CODE_ROOT = Path(__file__).resolve().parents[2]


def _detect_deploy_repo() -> Path:
    env = os.environ.get("VOLTCOUCH_DEPLOY_REPO")
    if env:
        return Path(env).expanduser().resolve()
    candidate = Path.home() / "deploy" / "voltcouch" / "yupp-agent"
    if (candidate / ".git").exists():
        return candidate.resolve()
    return _CODE_ROOT


REPO_ROOT = _detect_deploy_repo()
COMPOSE_FILE = REPO_ROOT / "docker-compose.one-box.yml"


def _detect_workspace() -> Path:
    """Pick the workspace dir that's actually being used.

    Honors AHS_HOST_DATA_DIR if set. Otherwise prefers the externalized
    ~/deploy/voltcouch/data layout if it exists (this is what
    deploy/mac/install.sh creates), falling back to ./ahs-data inside the
    repo (the docker-compose default).
    """
    env = os.environ.get("AHS_HOST_DATA_DIR") or os.environ.get("AHS_DATA_DIR_HOST")
    if env:
        return Path(env).expanduser()
    external = Path.home() / "deploy" / "voltcouch" / "data"
    if external.exists():
        return external
    return REPO_ROOT / "ahs-data"


WORKSPACE_DIR = _detect_workspace()

_CACHE: dict[str, tuple[float, Any]] = {}

R = TypeVar("R")


def _cache_get(key: str, ttl: float) -> Any | None:
    if key not in _CACHE:
        return None
    ts, val = _CACHE[key]
    if time.time() - ts > ttl:
        return None
    return val


def _cache_set[R](key: str, val: R) -> R:
    _CACHE[key] = (time.time(), val)
    return val


async def _run(*args: str, cwd: Path | None = None, timeout: float = 30.0) -> str:
    """Run a subprocess and return stdout (empty string on error/timeout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("command not found: %s", args[0])
        return ""
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        logger.warning("command timed out: %s", " ".join(args))
        return ""
    return stdout.decode(errors="replace").strip()


# ---------------------------------------------------------------------------
# System resources (host)
# ---------------------------------------------------------------------------


def system_resources() -> dict[str, Any]:
    """Synchronous snapshot of host CPU / mem / disk / uptime."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(REPO_ROOT))
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (0.0, 0.0, 0.0)
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "load_avg": [round(x, 2) for x in load],
        "mem_total_gb": round(mem.total / 1e9, 1),
        "mem_used_gb": round(mem.used / 1e9, 1),
        "mem_percent": mem.percent,
        "disk_total_gb": round(disk.total / 1e9, 0),
        "disk_used_gb": round(disk.used / 1e9, 0),
        "disk_free_gb": round(disk.free / 1e9, 0),
        "disk_percent": disk.percent,
        "boot_time": psutil.boot_time(),
        "uptime_seconds": time.time() - psutil.boot_time(),
    }


# ---------------------------------------------------------------------------
# Docker compose
# ---------------------------------------------------------------------------


async def docker_compose_ps() -> list[dict[str, Any]]:
    """List services from docker compose. Returns [] on docker failure."""
    out = await _run(
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "ps",
        "--format",
        "json",
        timeout=10,
    )
    services: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        services.append(
            {
                "service": d.get("Service"),
                "name": d.get("Name"),
                "state": d.get("State"),
                "health": d.get("Health") or "",
                "image": d.get("Image"),
                "status": d.get("Status"),
                "running_for": d.get("RunningFor"),
                "ports": d.get("Ports"),
            }
        )
    services.sort(key=lambda s: s["service"] or "")
    return services


async def docker_stats() -> dict[str, dict[str, Any]]:
    """Per-container CPU% and mem (current snapshot via docker stats --no-stream).

    Returns map: container_name -> {cpu_percent, mem_used_bytes, mem_percent}.
    Cached for 15 s; the underlying command takes ~1-2s.
    """
    cached = _cache_get("docker_stats", ttl=15)
    if cached is not None:
        return cast(dict[str, dict[str, Any]], cached)

    out = await _run(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","memp":"{{.MemPerc}}"}',
        timeout=15,
    )
    result: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = d.get("name", "")
        cpu_pct = float(d.get("cpu", "0%").rstrip("%") or 0)
        mem_pct = float(d.get("memp", "0%").rstrip("%") or 0)
        mem_used = _parse_mem(d.get("mem", "0B"))
        result[name] = {
            "cpu_percent": cpu_pct,
            "mem_used_bytes": mem_used,
            "mem_percent": mem_pct,
        }
    return _cache_set("docker_stats", result)


_MEM_UNITS = {
    "B": 1,
    "KB": 1e3,
    "MB": 1e6,
    "GB": 1e9,
    "TB": 1e12,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def _parse_mem(s: str) -> int:
    """Parse '612MiB / 7.7GiB' or '23.4MB' → bytes (first value)."""
    s = s.split("/")[0].strip()
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s)
    if not m:
        return 0
    return int(float(m.group(1)) * _MEM_UNITS.get(m.group(2).upper(), 1))


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


async def git_info() -> dict[str, Any]:
    """Branch, sha, commit subject, commits behind origin/<branch>."""
    branch = await _run("git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD")
    sha = await _run("git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD")
    subject = await _run("git", "-C", str(REPO_ROOT), "log", "-1", "--format=%s")
    dirty = bool(await _run("git", "-C", str(REPO_ROOT), "status", "--porcelain"))
    behind = 0
    if branch and branch != "HEAD":
        behind_str = await _run(
            "git",
            "-C",
            str(REPO_ROOT),
            "rev-list",
            "--count",
            f"HEAD..origin/{branch}",
        )
        if behind_str.isdigit():
            behind = int(behind_str)
    return {
        "branch": branch or "?",
        "sha": sha or "?",
        "subject": subject,
        "dirty": dirty,
        "commits_behind": behind,
    }


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


async def workspace_size(force: bool = False) -> dict[str, Any]:
    """du -sk on workspace subdirs. Cached 5 min — du is slow on large trees."""
    if not force:
        cached = _cache_get("workspace", ttl=300)
        if cached is not None:
            return cast(dict[str, Any], cached)
    breakdown: dict[str, int] = {}
    total = 0
    sessions_dir_count = 0
    if WORKSPACE_DIR.exists():
        for sub in ("sessions", "repos", "agent_memories", "artifacts"):
            p = WORKSPACE_DIR / sub
            if not p.exists():
                continue
            out = await _run("du", "-sk", str(p), timeout=120)
            first = out.split()[0] if out else ""
            kb = int(first) if first.isdigit() else 0
            breakdown[sub] = kb * 1024
            total += kb * 1024
            if sub == "sessions":
                try:
                    sessions_dir_count = sum(1 for _ in p.iterdir())
                except OSError:
                    sessions_dir_count = 0
    return _cache_set(
        "workspace",
        {
            "path": str(WORKSPACE_DIR),
            "total_bytes": total,
            "breakdown_bytes": breakdown,
            "session_dir_count": sessions_dir_count,
            "measured_at": time.time(),
        },
    )


# ---------------------------------------------------------------------------
# Sessions (postgres)
# ---------------------------------------------------------------------------


# Bucket boundaries for the turn-size distribution (inclusive lo, inclusive hi).
TURN_BUCKETS: list[tuple[str, int, int]] = [
    ("1", 1, 1),
    ("2-3", 2, 3),
    ("4-7", 4, 7),
    ("8-15", 8, 15),
    ("16-31", 16, 31),
    ("32-63", 32, 63),
    ("64+", 64, 10**9),
]


async def session_stats() -> dict[str, Any]:
    """Status counts + per-day histogram + turn-size distribution.

    Imports `get_async_session` lazily so a broken backend config doesn't
    break import of this module.
    """
    try:
        from ypl.backend.db import get_async_session
    except Exception as e:
        return {"error": f"db import failed: {e}"}

    try:
        async with get_async_session() as session:
            # status counts
            rows = (await session.execute(text("SELECT status, count(*) FROM agent_sessions GROUP BY status"))).all()
            status_counts = {str(r[0]): int(r[1]) for r in rows}

            # per-day, last 30 days
            cutoff = datetime.now(UTC) - timedelta(days=30)
            rows = (
                await session.execute(
                    text(
                        "SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS d, "
                        "       count(*) "
                        "FROM agent_sessions "
                        "WHERE created_at >= :cutoff "
                        "GROUP BY d ORDER BY d"
                    ),
                    {"cutoff": cutoff},
                )
            ).all()
            per_day_map = {r[0].isoformat(): int(r[1]) for r in rows}
            per_day: list[dict[str, Any]] = []
            today = datetime.now(UTC).date()
            for i in range(30):
                d = (today - timedelta(days=29 - i)).isoformat()
                per_day.append({"date": d, "count": per_day_map.get(d, 0)})

            # turn-size distribution (messages per session)
            rows = (
                await session.execute(
                    text(
                        "SELECT msg_count, count(*) AS sessions FROM ("
                        "  SELECT agent_session_id, count(*) AS msg_count "
                        "  FROM agent_session_messages "
                        "  GROUP BY agent_session_id"
                        ") t GROUP BY msg_count ORDER BY msg_count"
                    )
                )
            ).all()
            distribution = [
                {"bucket": label, "sessions": sum(int(r[1]) for r in rows if lo <= int(r[0]) <= hi)}
                for label, lo, hi in TURN_BUCKETS
            ]
    except Exception as e:
        logger.exception("session_stats failed")
        return {"error": f"sql failed: {e}"}

    return {
        "status_counts": status_counts,
        "total": sum(status_counts.values()),
        "per_day_30d": per_day,
        "turn_distribution": distribution,
    }


# ---------------------------------------------------------------------------
# Config (annotated, redacted .env)
# ---------------------------------------------------------------------------


_SENSITIVE = re.compile(
    r"(password|secret|token|api[_-]?key|signing[_-]?key|"
    r"encryption[_-]?key|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret)",
    re.IGNORECASE,
)

# Functional groups: ordered list of (group label, ordered keys, annotations).
# Anything not in any group lands in "Other".
CONFIG_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Database & cache",
        [
            ("POSTGRES_CONNECTION_AGENTDB", "Primary AHS DB connection (JSON)."),
            ("POSTGRES_USER", "Postgres role used by app + admin daemon."),
            ("POSTGRES_PASSWORD", "Postgres role password."),
            ("POSTGRES_DB", "Database name."),
            ("REDIS_URL", "Redis connection string (app-side)."),
        ],
    ),
    (
        "Ports & paths",
        [
            ("AHS_PORT", "AHS+SAG+MCP monolith listen port (compose: 8090)."),
            ("VIEWER_PORT", "Artifact viewer listen port."),
            ("AHS_DATA_DIR", "In-container workspace mount target."),
            ("AHS_HOST_DATA_DIR", "Host workspace dir bind-mounted into containers."),
            ("AHS_ENV_FILE", "Path to .env consumed by docker compose."),
        ],
    ),
    (
        "Feature toggles",
        [
            ("SANDBOX_ENABLED", "bwrap sandbox; false in unprivileged docker."),
            ("USE_GOOGLE_CLOUD_LOGGING", "Off → logs go to docker json-file driver."),
            ("DISABLE_WRITE_GOOGLE_CLOUD_METRICS", "Off → metrics handled by netdata locally."),
            ("AHS_MONO_ENABLE_GATEWAY_SERVICE", "Mount /gw/<name>/* gateway plugins."),
            ("GATEWAY_SLACK_ENABLED", "Slack gateway plugin (needs SLACK_* keys)."),
            ("AHS_MONO_ENABLE_MCP", "Mount /mcp/agcouch developer MCP."),
            ("IS_SANDBOX", "Flag many subsystems use to relax safety."),
            ("VOLTCOUCH_ENV", "Logical env name (local | staging | prod)."),
        ],
    ),
    (
        "OAuth & hostnames",
        [
            ("GOOGLE_OAUTH_CLIENT_ID", "Public OAuth client id (Streamlit, Viewer, Admin)."),
            ("GOOGLE_OAUTH_CLIENT_SECRET", "OAuth client secret."),
            ("AGENT_UI_HOST", "Public host for Streamlit (lit.voltcouch.com)."),
            ("ARTIFACTS_HOST", "Public host for Artifact Viewer (a.voltcouch.com)."),
            ("AHS_HOST", "Public host for AHS API (ahs.voltcouch.com)."),
        ],
    ),
    (
        "Slack gateway",
        [
            ("SLACK_SIGNING_SECRET", "Slack request signature verification."),
            ("SLACK_BOT_TOKEN", "xoxb- token used to post messages."),
            ("SLACK_APP_TOKEN", "xapp- token (socket mode, if used)."),
            ("SLACK_AGENT_GW_ENCRYPTION_KEY", "Encrypts per-session Slack state at rest."),
        ],
    ),
    (
        "MCP secrets",
        [
            ("MCP_OAUTH_JWT_SIGNING_KEY", "Signs MCP-issued OAuth JWTs."),
            ("MCP_OAUTH_STORAGE_ENCRYPTION_KEY", "Fernet key for OAuth grant store."),
            ("EXTERNAL_MCP_GRANT_ENCRYPTION_KEY", "Fernet key for per-user external MCP grants."),
        ],
    ),
    (
        "LLM providers",
        [
            ("ANTHROPIC_API_KEY", "Anthropic Claude."),
            ("OPENAI_API_KEY", "OpenAI."),
            ("GEMINI_API_KEY", "Google Gemini."),
            ("GOOGLE_API_KEY", "Alternate Google API key."),
        ],
    ),
]


def _redact_value(key: str, value: str) -> tuple[str, bool]:
    """Return (display_value, was_redacted) for a single env var."""
    if not value:
        return ("(unset)", False)
    if _SENSITIVE.search(key):
        return (f"●●●●●● ({len(value)} chars)", True)
    # JSON-embedded secrets (e.g. POSTGRES_CONNECTION_AGENTDB)
    if value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return (value, False)
        if isinstance(parsed, dict):
            redacted_any = False
            cleaned = {}
            for k, v in parsed.items():
                if _SENSITIVE.search(k) and v:
                    cleaned[k] = "●●●●●●"
                    redacted_any = True
                else:
                    cleaned[k] = v
            return (json.dumps(cleaned), redacted_any)
    return (value, False)


def config_view() -> dict[str, Any]:
    """Build the grouped, redacted view of .env / process env for the UI."""
    seen_keys: set[str] = set()
    groups_out = []
    for group_label, keys in CONFIG_GROUPS:
        items = []
        for key, annotation in keys:
            raw = os.environ.get(key, "")
            display, redacted = _redact_value(key, raw)
            items.append(
                {
                    "key": key,
                    "value": display,
                    "set": bool(raw),
                    "redacted": redacted,
                    "annotation": annotation,
                }
            )
            seen_keys.add(key)
        groups_out.append({"label": group_label, "items": items})

    # "Other" — every env var that looks like an AHS/SAG/MCP/SLACK/etc setting
    # but isn't in our curated list above.
    interesting_prefixes = (
        "AHS_",
        "MCP_",
        "POSTGRES_",
        "REDIS_",
        "SLACK_",
        "GOOGLE_",
        "GATEWAY_",
        "VIEWER_",
        "ARTIFACTS_",
        "AGENT_",
        "VOLTCOUCH_",
        "SANDBOX_",
    )
    other_items = []
    for k in sorted(os.environ.keys()):
        if k in seen_keys:
            continue
        if not k.startswith(interesting_prefixes):
            continue
        display, redacted = _redact_value(k, os.environ[k])
        other_items.append(
            {
                "key": k,
                "value": display,
                "set": True,
                "redacted": redacted,
                "annotation": "",
            }
        )
    if other_items:
        groups_out.append({"label": "Other (uncategorized)", "items": other_items})

    return {
        "groups": groups_out,
        "total_env_keys": len(os.environ),
    }


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


async def collect_all() -> dict[str, Any]:
    """Single entry point used by /api/status. Runs independent calls in parallel."""
    docker, stats, git, sessions, ws = await asyncio.gather(
        docker_compose_ps(),
        docker_stats(),
        git_info(),
        session_stats(),
        workspace_size(),
        return_exceptions=True,
    )

    def _ok(val: Any, fallback: Any) -> Any:
        return val if not isinstance(val, BaseException) else {"error": str(val), "fallback": fallback}

    return {
        "now": time.time(),
        "system": system_resources(),
        "docker": _ok(docker, []),
        "docker_stats": _ok(stats, {}),
        "git": _ok(git, {}),
        "sessions": _ok(sessions, {}),
        "workspace": _ok(ws, {}),
    }
