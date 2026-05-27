"""voltcouch admin console — FastAPI app.

Run locally:

    poetry run uvicorn ypl.admin_console.server:app \\
        --host 127.0.0.1 --port 8099 --reload

In production (laptop one-box) it runs as a host-side launchd service
behind the existing cloudflared tunnel at admin.voltcouch.com.

Architecture:
  - read-only views      → status.py  (docker / git / du / psutil / sql)
  - mutating actions     → jobs.py    (restart / deploy / rollback + SSE)
  - background workers   → scheduler.py (host crons)
  - Google OAuth + email → auth.py
"""

from __future__ import annotations
import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv


# .env BEFORE importing anything that reads settings (backend.config, etc).
# Resolution order matches status._detect_deploy_repo():
#   1. AHS_ENV_FILE env var (explicit, set by launchd plist in production)
#   2. ~/deploy/voltcouch/config/.env if it exists (install.sh layout)
#   3. <code-root>/.env (dev fallback)
def _detect_env_file() -> Path:
    explicit = os.environ.get("AHS_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    deploy_env = Path.home() / "deploy" / "voltcouch" / "config" / ".env"
    if deploy_env.exists():
        return deploy_env
    return Path(__file__).resolve().parents[2] / ".env"


_ENV_PATH = _detect_env_file()
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from ypl.admin_console import auth, jobs, scheduler, status  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("admin_console")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Start the host-cron scheduler in the background.
    task = asyncio.create_task(scheduler.loop())
    logger.info("admin daemon up · oauth=%s · workspace=%s", auth.OAUTH_ENABLED, status.WORKSPACE_DIR)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="voltcouch admin",
    default_response_class=JSONResponse,
    lifespan=lifespan,
)

SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET") or secrets.token_urlsafe(32)
# Order matters: Starlette wraps middlewares such that the LAST added is the
# OUTERMOST. We want SessionMiddleware to be outer (populates scope.session)
# so AuthMiddleware sees it on the way in. Therefore add AuthMiddleware first.
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,  # cloudflared terminates TLS; tunnel itself is http
    max_age=14 * 86400,
)

app.include_router(auth.router)

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "admin_v0.html"


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "env_loaded": _ENV_PATH.exists(),
        "env_path": str(_ENV_PATH),
        "deploy_repo": str(status.REPO_ROOT),
        "workspace_dir": str(status.WORKSPACE_DIR),
        "oauth_enabled": auth.OAUTH_ENABLED,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not _TEMPLATE_PATH.exists():
        return HTMLResponse(
            f"<h1>template missing</h1><p>expected at {_TEMPLATE_PATH}</p>",
            status_code=500,
        )
    return HTMLResponse(_TEMPLATE_PATH.read_text())


# ---------------------------------------------------------------------------
# read-only data
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def api_status() -> dict:
    return await status.collect_all()


@app.get("/api/config")
async def api_config() -> dict:
    return status.config_view()


@app.post("/api/workspace/refresh")
async def api_workspace_refresh() -> dict:
    return await status.workspace_size(force=True)


# ---------------------------------------------------------------------------
# control plane
# ---------------------------------------------------------------------------


@app.post("/api/services/{service}/restart")
async def api_restart(service: str, request: Request) -> dict:
    try:
        job = jobs.start_restart(service, auth.current_actor(request))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"job_id": job.id, "action": "restart", "target": service}


@app.post("/api/deploy")
async def api_deploy(request: Request, branch: str | None = None) -> dict:
    job = jobs.start_deploy(branch, auth.current_actor(request))
    return {"job_id": job.id, "action": "deploy", "target": branch or "current"}


@app.post("/api/rollback")
async def api_rollback(request: Request, sha: str) -> dict:
    if not sha or not jobs._SAFE_SHA_RE.match(sha):
        raise HTTPException(400, "sha must be 4-40 hex characters")
    job = jobs.start_rollback(sha, auth.current_actor(request))
    return {"job_id": job.id, "action": "rollback", "target": sha}


@app.get("/api/services/{service}/logs")
async def api_service_logs(service: str, tail: int = 500) -> dict:
    return await jobs.container_logs(service, tail=tail)


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str) -> dict:
    job = jobs.JOBS.get(job_id)
    if not job:
        raise HTTPException(404)
    return {
        "id": job.id,
        "action": job.action,
        "target": job.target,
        "actor": job.actor,
        "status": job.status,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "exit_code": job.exit_code,
        "duration_s": job.duration_s,
        "log": job.log_lines,
    }


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str) -> StreamingResponse:
    return StreamingResponse(
        jobs.stream_job(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/deploys")
async def api_deploys(limit: int = 50) -> dict:
    return {"deploys": jobs.get_audit_tail(limit)}


# ---------------------------------------------------------------------------
# crons
# ---------------------------------------------------------------------------


@app.get("/api/crons")
async def api_crons() -> dict:
    return {"crons": scheduler.get_all()}


@app.post("/api/crons/{name}/run")
async def api_crons_run(name: str) -> dict:
    try:
        cron = await scheduler.run_now(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "name": cron.name,
        "last_status": cron.last_status,
        "last_result": cron.last_result,
        "last_duration_s": cron.last_duration_s,
    }


@app.post("/api/crons/{name}/toggle")
async def api_crons_toggle(name: str, enabled: bool = True) -> dict:
    try:
        cron = scheduler.toggle(name, enabled)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"name": cron.name, "enabled": cron.enabled}
