"""GitHub App webhook receiver for Agent Harness Service.

Handles ``pull_request`` events from the yupp-agent-harness GitHub App and
triggers the dual-reviewer agent on qualifying PRs.

Qualifying events
-----------------
- ``ready_for_review``  — PR just moved from draft to ready for review.
- ``synchronize``       — new commits pushed to a PR that is already ready
                          (draft=False).

All other events and actions are acknowledged (HTTP 200) and silently ignored.

Authentication
--------------
Every incoming request is verified against the HMAC-SHA256 signature that
GitHub sends in the ``X-Hub-Signature-256`` header. The shared secret must be
configured via ``AHS_GITHUB_WEBHOOK_SECRET`` in the environment / GCP secrets.
No API key is required — the webhook secret is the sole auth mechanism.
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.service import create_session
from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

webhook_router = APIRouter(prefix="/webhook", tags=["github-webhook"])

_REVIEWER_AGENT = "dual-reviewer"
_WEBHOOK_TRIGGER = "webhook"
_WEBHOOK_SOURCE = "github_webhook"


def _verify_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Return True iff the GitHub HMAC-SHA256 signature is valid."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


@webhook_router.post("/github")
async def github_webhook(request: Request) -> Response:
    """Receive and dispatch GitHub App webhook events.

    Responds with HTTP 200 + ``{"ok": true}`` for all valid, processed events.
    Skipped events also return 200 with ``{"skipped": true}``.
    """
    secret = settings.AHS_GITHUB_WEBHOOK_SECRET
    if not secret:
        logger.error("AHS_GITHUB_WEBHOOK_SECRET not configured — rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    raw_body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")

    if not _verify_signature(raw_body, sig, secret):
        logger.warning("github_webhook_signature_invalid")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")

    # Acknowledge ping events GitHub sends on webhook creation.
    if event_type == "ping":
        logger.info("github_webhook_ping_received")
        return Response(content='{"ok":true,"pong":true}', media_type="application/json")

    if event_type != "pull_request":
        return Response(content='{"ok":true,"skipped":true}', media_type="application/json")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    is_draft = pr.get("draft", False)

    # Only trigger on ready_for_review, or synchronize when PR is already ready.
    if action == "ready_for_review":
        pass  # always trigger
    elif action == "synchronize" and not is_draft:
        pass  # new commits to an open, ready PR
    else:
        return Response(content='{"ok":true,"skipped":true}', media_type="application/json")

    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")  # e.g. "yupp-ai/yupp-agent"
    repo_short = repo_full_name.split("/")[-1] if "/" in repo_full_name else repo_full_name
    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    head_ref = pr.get("head", {}).get("ref", "")
    base_ref = pr.get("base", {}).get("ref", "")
    author = pr.get("user", {}).get("login", "")

    message = (
        f"Review PR #{pr_number} in {repo_full_name}.\n\n"
        f"PR URL: {pr_url}\n"
        f"Title: {pr_title}\n"
        f"Author: @{author}\n"
        f"Branch: `{head_ref}` → `{base_ref}`\n"
        f"Trigger: {action}\n\n"
        f"Fetch the diff, run dual reviews, synthesize findings, and post the "
        f"result as a GitHub comment on the PR."
    )

    context: dict = {
        "repo": repo_short,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_title": pr_title,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "author": author,
        "event_action": action,
        "repo_full_name": repo_full_name,
    }

    create_req = SessionCreateRequest(
        agent_id=_REVIEWER_AGENT,
        trigger=_WEBHOOK_TRIGGER,
        message=message,
        context=context,
        source=_WEBHOOK_SOURCE,
    )

    try:
        session_resp = await create_session(create_req)
        logger.info(
            "github_webhook_session_created",
            pr_number=pr_number,
            repo=repo_full_name,
            action=action,
            session_id=str(session_resp.session_id),
        )
        return Response(
            content=json.dumps({"ok": True, "session_id": str(session_resp.session_id)}),
            media_type="application/json",
        )
    except Exception as exc:
        logger.exception(
            "github_webhook_session_failed",
            pr_number=pr_number,
            repo=repo_full_name,
        )
        raise HTTPException(status_code=500, detail=f"Failed to start review session: {exc}") from exc
