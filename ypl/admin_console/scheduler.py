"""Host-side maintenance crons run by the admin daemon.

These keep the laptop in shape — git fetch so the dashboard's "N behind"
badge is accurate, du to keep workspace size fresh, periodic prune of
dangling docker images / build cache, and a daily pg_dump backup.

Definitions are pure-interval (next_run = last_run + interval_seconds).
Cron-style expressions aren't worth the parser dependency here. Enable
state is persisted to ~/.voltcouch-admin/crons.json so a daemon restart
preserves your toggles.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ypl.admin_console import jobs as jobs_mod
from ypl.admin_console import status

logger = logging.getLogger(__name__)

STATE_FILE = jobs_mod.ADMIN_DIR / "crons.json"


@dataclass
class Cron:
    name: str
    description: str
    interval_seconds: float
    fn: Callable[[], Awaitable[str]]
    enabled: bool = True
    last_run: float | None = None
    last_status: str | None = None
    last_result: str | None = None
    last_duration_s: float | None = None
    next_run: float | None = None
    running: bool = False


CRONS: dict[str, Cron] = {}


# ---------------------------------------------------------------------------
# job bodies
# ---------------------------------------------------------------------------


async def _git_fetch() -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(status.REPO_ROOT),
        "fetch",
        "--all",
        "--prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(out.decode(errors="replace").strip()[:200])
    return "fetched"


async def _du_refresh() -> str:
    info = await status.workspace_size(force=True)
    return f"{info['total_bytes'] / 1e9:.2f} GB · {info['session_dir_count']} dirs"


async def _docker_prune() -> str:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "system",
        "prune",
        "-af",
        "--filter",
        "until=168h",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(out.decode(errors="replace").strip()[:200])
    text = out.decode(errors="replace")
    for line in text.splitlines():
        if "Total reclaimed" in line:
            return line.strip()
    return "prune complete"


async def _pg_backup() -> str:
    out_dir = jobs_mod.ADMIN_DIR / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"yadb-{ts}.sql.gz"
    cmd = f'docker compose -f {status.COMPOSE_FILE} exec -T postgres pg_dump -U postgres yadb | gzip > "{out_file}"'
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[:200])
    # prune old backups (keep last 14)
    backups = sorted(out_dir.glob("yadb-*.sql.gz"))
    for old in backups[:-14]:
        old.unlink(missing_ok=True)
    size = out_file.stat().st_size if out_file.exists() else 0
    return f"{out_file.name} · {size / 1e6:.1f} MB · kept {min(len(backups), 14)}"


_DEFINITIONS: list[tuple[str, str, float, Callable[[], Awaitable[str]]]] = [
    ("git_fetch", "git fetch --all --prune so dashboard 'commits behind' stays fresh", 30 * 60, _git_fetch),
    ("workspace_du", "refresh workspace-size cache (used by the dashboard KPI)", 5 * 60, _du_refresh),
    ("docker_prune", "prune dangling images + build cache older than 7 days", 7 * 86400, _docker_prune),
    ("pg_backup", "pg_dump yadb → ~/.voltcouch-admin/backups/ (keep last 14)", 24 * 3600, _pg_backup),
]


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return cast(dict[str, dict[str, Any]], json.loads(STATE_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state() -> None:
    data = {
        c.name: {
            "enabled": c.enabled,
            "last_run": c.last_run,
            "last_status": c.last_status,
            "last_result": c.last_result,
            "last_duration_s": c.last_duration_s,
        }
        for c in CRONS.values()
    }
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2))
    except OSError as e:
        logger.warning("failed to persist cron state: %s", e)


def register_defaults() -> None:
    saved = _load_state()
    for name, desc, interval, fn in _DEFINITIONS:
        if name in CRONS:
            continue
        s = saved.get(name, {})
        CRONS[name] = Cron(
            name=name,
            description=desc,
            interval_seconds=interval,
            fn=fn,
            enabled=s.get("enabled", True),
            last_run=s.get("last_run"),
            last_status=s.get("last_status"),
            last_result=s.get("last_result"),
            last_duration_s=s.get("last_duration_s"),
            # stagger so they don't all fire at once on startup
            next_run=time.time() + 30 + len(CRONS) * 15,
        )


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


async def _run_one(cron: Cron) -> None:
    if cron.running:
        return
    cron.running = True
    start = time.time()
    try:
        cron.last_result = await cron.fn()
        cron.last_status = "ok"
    except Exception as e:
        logger.exception("cron %s failed", cron.name)
        cron.last_status = "fail"
        cron.last_result = str(e)
    finally:
        cron.last_run = start
        cron.last_duration_s = time.time() - start
        cron.next_run = time.time() + cron.interval_seconds
        cron.running = False
        _save_state()


async def loop() -> None:
    register_defaults()
    logger.info("scheduler: %d crons registered", len(CRONS))
    while True:
        now = time.time()
        for cron in list(CRONS.values()):
            if not cron.enabled or cron.next_run is None or cron.next_run > now:
                continue
            asyncio.create_task(_run_one(cron))
        await asyncio.sleep(5)


async def run_now(name: str) -> Cron:
    cron = CRONS.get(name)
    if not cron:
        raise ValueError(f"unknown cron: {name}")
    await _run_one(cron)
    return cron


def toggle(name: str, enabled: bool) -> Cron:
    cron = CRONS.get(name)
    if not cron:
        raise ValueError(f"unknown cron: {name}")
    cron.enabled = enabled
    _save_state()
    return cron


def get_all() -> list[dict]:
    return [
        {
            "name": c.name,
            "description": c.description,
            "interval_seconds": c.interval_seconds,
            "enabled": c.enabled,
            "last_run": c.last_run,
            "last_status": c.last_status,
            "last_result": c.last_result,
            "last_duration_s": c.last_duration_s,
            "next_run": c.next_run,
            "running": c.running,
        }
        for c in CRONS.values()
    ]
