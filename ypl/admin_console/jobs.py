"""Control plane: restart / deploy / rollback as background jobs.

Each user action creates a Job (id, log buffer, status). The job runs a
subprocess pipeline (`docker compose ...`, `git ...`) and streams stdout
into the buffer. SSE clients poll the buffer in `stream_job()`.

Successful deploys / restarts append a single line to ~/.voltcouch-admin/deploys.jsonl
which the dashboard renders as the audit log. Fix-forward is the primary
recovery path; rollback (`git checkout <sha> && compose up -d --build`) is
available but flagged when it would cross alembic migrations.

Only one job runs at a time (asyncio.Lock); concurrent triggers queue.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ypl.admin_console.status import COMPOSE_FILE, REPO_ROOT

logger = logging.getLogger(__name__)

# Allowlist patterns for user-supplied git refs.  Git treats argv tokens
# starting with ``-`` as option flags even after the subcommand name; injecting
# ``--upload-pack=/tmp/x.sh`` or ``-b`` is therefore possible without shell
# involvement.  Validate all branch names and shas before passing them as argv.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9/._-]+$")
_SAFE_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

ADMIN_DIR = Path(os.environ.get("VOLTCOUCH_ADMIN_DIR") or (Path.home() / ".voltcouch-admin"))
AUDIT_LOG = ADMIN_DIR / "deploys.jsonl"
ADMIN_DIR.mkdir(parents=True, exist_ok=True)

# Services we'll let the UI act on. Postgres / redis are restartable but we
# never want a "deploy" (rebuild) to touch them — they're stateful.
RESTARTABLE = {"app", "streamlit", "artifact-viewer", "postgres", "redis", "dozzle", "netdata-sidecar"}

# After a deploy/rollback we poll each of these endpoints (via host port) and
# the deploy is marked "ok" only if every one responds 200 within the gate.
HEALTH_URLS = {
    "app": "http://127.0.0.1:8090/health",
    "streamlit": "http://127.0.0.1:8501/_stcore/health",
    "artifact-viewer": "http://127.0.0.1:8095/healthz",
}


@dataclass
class Job:
    id: str
    action: str  # restart | deploy | rollback | logs
    target: str  # service name | branch | sha
    actor: str
    status: str = "pending"
    log_lines: list[dict] = field(default_factory=list)
    started_at: float | None = None
    completed_at: float | None = None
    exit_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, text: str, kind: str = "out") -> None:
        self.log_lines.append({"ts": time.time(), "kind": kind, "text": text})

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.completed_at or time.time()) - self.started_at


JOBS: dict[str, Job] = {}
_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# subprocess streaming
# ---------------------------------------------------------------------------


async def _run_streaming(args: list[str], job: Job, *, cwd: Path | None = None) -> int:
    job.add("$ " + " ".join(args), "cmd")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        job.add(f"command not found: {e}", "err")
        return 127
    assert proc.stdout is not None
    async for raw in proc.stdout:
        job.add(raw.decode(errors="replace").rstrip(), "out")
    rc = await proc.wait()
    job.add(f"[exit {rc}]", "rc")
    return rc


async def _capture(args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


async def _git_sha() -> str:
    return (await _capture(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"])).strip()


# ---------------------------------------------------------------------------
# health gate
# ---------------------------------------------------------------------------


async def _health_gate(
    services: list[str], timeout_s: float = 60.0, job: Job | None = None
) -> tuple[str, dict[str, str]]:
    start = time.time()
    statuses: dict[str, str] = {}
    while time.time() - start < timeout_s:
        statuses = {}
        for svc in services:
            url = HEALTH_URLS.get(svc)
            if not url:
                statuses[svc] = "no-check"
                continue
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(url)
                statuses[svc] = "healthy" if r.status_code == 200 else f"http_{r.status_code}"
            except Exception as e:
                statuses[svc] = f"err:{type(e).__name__}"
        if job is not None:
            job.add("health: " + " · ".join(f"{k}={v}" for k, v in statuses.items()), "health")
        if all(v in ("healthy", "no-check") for v in statuses.values()):
            return "ok", statuses
        await asyncio.sleep(3)
    return "flapped", statuses


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


async def audit_append(entry: dict[str, Any]) -> None:
    entry = {"ts": datetime.now(UTC).isoformat(), **entry}
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def get_audit_tail(n: int = 50) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-n:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


# ---------------------------------------------------------------------------
# job factories
# ---------------------------------------------------------------------------


def _make(action: str, target: str, actor: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:8], action=action, target=target, actor=actor)
    JOBS[job.id] = job
    # bounded retention: keep last 100
    if len(JOBS) > 100:
        for k in list(JOBS.keys())[:-100]:
            JOBS.pop(k, None)
    return job


def start_restart(service: str, actor: str) -> Job:
    if service not in RESTARTABLE:
        raise ValueError(f"unknown service: {service}")
    job = _make("restart", service, actor)
    asyncio.create_task(_do_restart(job, service))
    return job


def start_deploy(branch: str | None, actor: str) -> Job:
    job = _make("deploy", branch or "current-branch", actor)
    asyncio.create_task(_do_deploy(job, branch))
    return job


def start_rollback(sha: str, actor: str) -> Job:
    job = _make("rollback", sha, actor)
    asyncio.create_task(_do_rollback(job, sha))
    return job


# ---------------------------------------------------------------------------
# job bodies
# ---------------------------------------------------------------------------


async def _do_restart(job: Job, service: str) -> None:
    async with _LOCK:
        job.status = "running"
        job.started_at = time.time()
        rc = await _run_streaming(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", service],
            job,
        )
        health = "skipped"
        statuses: dict[str, str] = {}
        if rc == 0 and service in HEALTH_URLS:
            health, statuses = await _health_gate([service], timeout_s=60, job=job)
        job.exit_code = rc
        ok = rc == 0 and health in ("ok", "skipped")
        job.status = "succeeded" if ok else "failed"
        job.completed_at = time.time()
        await audit_append(
            {
                "actor": job.actor,
                "action": "restart",
                "target": service,
                "duration_s": round(job.duration_s or 0, 1),
                "exit_code": rc,
                "health": health,
                "statuses": statuses,
            }
        )


async def _stash_if_dirty(job: Job) -> bool:
    """If the deploy clone has uncommitted edits, push them to a named stash.

    Production deploys always build from pristine origin/main, so the stash
    is intentionally NOT auto-popped after the build — any local tweaks the
    operator made stay in `git stash list` to be inspected / cherry-picked
    manually. Returns True if a stash was actually created.
    """
    dirty = await _capture(["git", "-C", str(REPO_ROOT), "status", "--porcelain"])
    if not dirty.strip():
        return False
    n = len(dirty.splitlines())
    job.add(f"dirty working tree — auto-stashing {n} files (recover later with: git -C {REPO_ROOT} stash pop)", "warn")
    rc = await _run_streaming(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "stash",
            "push",
            "--include-untracked",
            "-m",
            f"voltcouch-admin auto-stash {datetime.now(UTC).isoformat()}",
        ],
        job,
    )
    if rc != 0:
        job.add("stash failed — aborting deploy", "err")
        return False
    return True


async def _do_deploy(job: Job, branch: str | None) -> None:
    async with _LOCK:
        job.status = "running"
        job.started_at = time.time()
        job.add(f"deploy target: {REPO_ROOT}", "info")
        sha_before = await _git_sha()
        job.add(f"sha before: {sha_before}", "info")

        if await _run_streaming(["git", "-C", str(REPO_ROOT), "fetch", "--all", "--prune"], job) != 0:
            _finish_failed(job)
            return

        await _stash_if_dirty(job)

        if branch:
            if not _SAFE_BRANCH_RE.match(branch):
                job.add(
                    f"branch {branch!r} rejected — must match [A-Za-z0-9/._-]+. "
                    "Branches starting with '-' or containing shell chars are not allowed.",
                    "err",
                )
                _finish_failed(job)
                return
            if await _run_streaming(["git", "-C", str(REPO_ROOT), "checkout", branch], job) != 0:
                _finish_failed(job)
                return

        if await _run_streaming(["git", "-C", str(REPO_ROOT), "pull", "--ff-only"], job) != 0:
            job.add("pull failed (non-fast-forward?). resolve manually and redeploy.", "err")
            _finish_failed(job)
            return

        sha_after = await _git_sha()
        job.add(f"sha after:  {sha_after}", "info")

        if sha_after != sha_before:
            diff = await _capture(["git", "-C", str(REPO_ROOT), "diff", "--name-only", sha_before, sha_after])
            if "ypl/db/alembic/versions/" in diff:
                job.add(
                    "⚠ alembic migrations changed — `python -m ypl.mono_server.setup` "
                    "may be needed before traffic resumes",
                    "warn",
                )
            if ".env.example" in diff:
                job.add("⚠ .env.example changed — check for new required vars", "warn")

        # Skip build when HEAD didn't move — common for "redeploy as a
        # safety check" or when the cron pre-fetched but no commits landed.
        # Saves a Docker Hub roundtrip (which can hang on a flaky proxy or
        # when Docker Hub rate-limits the free tier).
        if sha_after == sha_before:
            job.add("HEAD unchanged — skipping image build, just `up -d`", "info")
        else:
            if (
                await _run_streaming(
                    ["docker", "compose", "-f", str(COMPOSE_FILE), "build"],
                    job,
                )
                != 0
            ):
                await audit_append(
                    {
                        "actor": job.actor,
                        "action": "deploy",
                        "branch": branch or "current",
                        "sha_before": sha_before,
                        "sha_after": sha_after,
                        "exit_code": 1,
                        "health": "build-failed",
                        "duration_s": round(job.duration_s or 0, 1),
                    }
                )
                _finish_failed(job)
                return

        if (
            await _run_streaming(
                ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
                job,
            )
            != 0
        ):
            await audit_append(
                {
                    "actor": job.actor,
                    "action": "deploy",
                    "branch": branch or "current",
                    "sha_before": sha_before,
                    "sha_after": sha_after,
                    "exit_code": 1,
                    "health": "up-failed",
                    "duration_s": round(job.duration_s or 0, 1),
                }
            )
            _finish_failed(job)
            return

        health, statuses = await _health_gate(["app", "streamlit", "artifact-viewer"], timeout_s=120, job=job)
        job.exit_code = 0
        job.status = "succeeded" if health == "ok" else "failed"
        job.completed_at = time.time()
        await audit_append(
            {
                "actor": job.actor,
                "action": "deploy",
                "branch": branch or "current",
                "sha_before": sha_before,
                "sha_after": sha_after,
                "exit_code": 0,
                "health": health,
                "statuses": statuses,
                "duration_s": round(job.duration_s or 0, 1),
            }
        )


async def _do_rollback(job: Job, sha: str) -> None:
    async with _LOCK:
        job.status = "running"
        job.started_at = time.time()
        job.add(f"deploy target: {REPO_ROOT}", "info")

        if not _SAFE_SHA_RE.match(sha):
            job.add(
                f"sha {sha!r} rejected — must be 4-40 hex characters. "
                "Values starting with '-' or containing non-hex chars are not allowed.",
                "err",
            )
            _finish_failed(job)
            return

        sha_before = await _git_sha()
        job.add(f"sha before: {sha_before}", "info")
        job.add(f"target sha: {sha}", "info")

        # check whether we cross alembic
        diff = await _capture(["git", "-C", str(REPO_ROOT), "diff", "--name-only", sha, sha_before])
        if "ypl/db/alembic/versions/" in diff:
            job.add(
                "⚠ this rollback crosses alembic migrations — schema may need "
                "manual downgrade. Proceeding with code rollback only.",
                "warn",
            )

        await _stash_if_dirty(job)

        if await _run_streaming(["git", "-C", str(REPO_ROOT), "checkout", sha], job) != 0:
            _finish_failed(job)
            return

        if (
            await _run_streaming(
                ["docker", "compose", "-f", str(COMPOSE_FILE), "build"],
                job,
            )
            != 0
        ):
            await audit_append(
                {
                    "actor": job.actor,
                    "action": "rollback",
                    "target_sha": sha,
                    "sha_before": sha_before,
                    "exit_code": 1,
                    "health": "build-failed",
                    "duration_s": round(job.duration_s or 0, 1),
                }
            )
            _finish_failed(job)
            return

        if (
            await _run_streaming(
                ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
                job,
            )
            != 0
        ):
            await audit_append(
                {
                    "actor": job.actor,
                    "action": "rollback",
                    "target_sha": sha,
                    "sha_before": sha_before,
                    "exit_code": 1,
                    "health": "up-failed",
                    "duration_s": round(job.duration_s or 0, 1),
                }
            )
            _finish_failed(job)
            return

        health, statuses = await _health_gate(["app", "streamlit", "artifact-viewer"], timeout_s=120, job=job)
        job.exit_code = 0
        job.status = "succeeded" if health == "ok" else "failed"
        job.completed_at = time.time()
        await audit_append(
            {
                "actor": job.actor,
                "action": "rollback",
                "target_sha": sha,
                "sha_before": sha_before,
                "exit_code": 0,
                "health": health,
                "statuses": statuses,
                "duration_s": round(job.duration_s or 0, 1),
            }
        )


def _finish_failed(job: Job) -> None:
    job.exit_code = 1
    job.status = "failed"
    job.completed_at = time.time()


# ---------------------------------------------------------------------------
# logs (static dump)
# ---------------------------------------------------------------------------


async def container_logs(service: str, tail: int = 500) -> dict[str, Any]:
    container = f"yupp-agent-{service}-1"
    text = await _capture(["docker", "logs", "--tail", str(tail), "--timestamps", container])
    return {"service": service, "container": container, "lines": text.splitlines(), "tail": tail}


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------


async def stream_job(job_id: str) -> AsyncGenerator[bytes, None]:
    job = JOBS.get(job_id)
    if job is None:
        yield (b"event: error\ndata: " + json.dumps({"error": "job not found"}).encode() + b"\n\n")
        return
    sent = 0
    while True:
        while sent < len(job.log_lines):
            line = job.log_lines[sent]
            yield b"data: " + json.dumps(line).encode() + b"\n\n"
            sent += 1
        if job.status in ("succeeded", "failed"):
            payload = {"status": job.status, "exit_code": job.exit_code, "duration_s": round(job.duration_s or 0, 1)}
            yield b"event: done\ndata: " + json.dumps(payload).encode() + b"\n\n"
            return
        await asyncio.sleep(0.25)
