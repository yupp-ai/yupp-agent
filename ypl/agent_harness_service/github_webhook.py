"""GitHub App webhook receiver for Agent Harness Service.

Handles ``pull_request`` events from the yupp-agent-harness GitHub App and
triggers the master-reviewer agent on qualifying PRs.

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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.service import create_session
from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

webhook_router = APIRouter(prefix="/webhook", tags=["github-webhook"])

_REVIEWER_AGENT = "master-reviewer"
_WEBHOOK_TRIGGER = "webhook"
_WEBHOOK_SOURCE = "github_webhook"

# Redis key prefix for PR → session mapping.
# Value is a JSON blob: {"session_id": "...", "review_round": N}
_REDIS_PR_KEY_PREFIX = "ahs:webhook:pr-review:"
_REDIS_PR_KEY_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# ---------------------------------------------------------------------------
# Skill content (loaded once at import time from .agents/skills/ markdown files)
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / ".agents" / "skills"


def _load_skill(name: str) -> str:
    """Read a skill SKILL.md, stripping the YAML frontmatter."""
    path = _SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        return ""
    text = path.read_text()
    # Strip YAML frontmatter (--- ... ---)
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :].lstrip("\n")
    return text.strip()


_SKILL_GENERAL = _load_skill("review-pr-general")

_REPO_SKILLS: dict[str, str] = {
    "yupp-agent": _load_skill("review-pr-yupp-agent"),
    "yupp-mind": _load_skill("review-pr-yupp-mind"),
    "yupp-head": _load_skill("review-pr-yupp-head"),
    "yupp-soul": _load_skill("review-pr-yupp-soul"),
}


def _build_review_prompt(
    repo_short: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    author: str,
    head_ref: str,
    base_ref: str,
    action: str,
    repo_full_name: str,
    review_round: int,
) -> str:
    """Compose the full review prompt from general + repo-specific skills."""
    repo_skill = _REPO_SKILLS.get(repo_short, "")

    prompt_parts = [
        f"Review PR #{pr_number} in {repo_full_name}.\n",
        f"PR URL: {pr_url}",
        f"Title: {pr_title}",
        f"Author: @{author}",
        f"Branch: `{head_ref}` → `{base_ref}`",
        f"Trigger: {action}",
        f"Review round: {review_round}",
        "",
        "--- REVIEW GUIDELINES ---",
        "",
        _SKILL_GENERAL,
    ]

    if repo_skill:
        prompt_parts += [
            "",
            "--- REPO-SPECIFIC GUIDELINES ---",
            "",
            repo_skill,
        ]

    prompt_parts += [
        "",
        "--- END GUIDELINES ---",
        "",
        "Fetch the diff, review the PR, and post the result as a GitHub comment on the PR.",
    ]

    return "\n".join(prompt_parts)


# ---------------------------------------------------------------------------
# Redis-backed PR ↔ session cache
# ---------------------------------------------------------------------------


def _pr_redis_key(repo_full_name: str, pr_number: int) -> str:
    return f"{_REDIS_PR_KEY_PREFIX}{repo_full_name}#{pr_number}"


async def _get_pr_review_state(repo_full_name: str, pr_number: int) -> dict[str, Any] | None:
    """Load cached PR review state from Redis.

    Returns dict with ``session_id`` (str) and ``review_round`` (int), or None.
    """
    try:
        from ypl.db.redis import get_redis_client

        client = await get_redis_client()
        raw = await client.get(_pr_redis_key(repo_full_name, pr_number))
        if raw is None:
            return None
        return json.loads(raw)  # type: ignore[no-any-return]
    except Exception:
        logger.exception("github_webhook_redis_get_failed", repo=repo_full_name, pr_number=pr_number)
        return None


async def _set_pr_review_state(repo_full_name: str, pr_number: int, session_id: str, review_round: int) -> None:
    """Store PR review state in Redis with TTL."""
    try:
        from ypl.db.redis import get_redis_client

        client = await get_redis_client()
        payload = json.dumps({"session_id": session_id, "review_round": review_round})
        await client.set(_pr_redis_key(repo_full_name, pr_number), payload, ex=_REDIS_PR_KEY_TTL_SECONDS)
    except Exception:
        logger.exception("github_webhook_redis_set_failed", repo=repo_full_name, pr_number=pr_number)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Return True iff the GitHub HMAC-SHA256 signature is valid."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


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

    # Determine review round from Redis cache.
    cached = await _get_pr_review_state(repo_full_name, pr_number)
    review_round = (cached["review_round"] + 1) if cached else 1

    message = _build_review_prompt(
        repo_short=repo_short,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
        author=author,
        head_ref=head_ref,
        base_ref=base_ref,
        action=action,
        repo_full_name=repo_full_name,
        review_round=review_round,
    )

    context: dict[str, Any] = {
        "repo": repo_short,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_title": pr_title,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "author": author,
        "event_action": action,
        "repo_full_name": repo_full_name,
        "review_round": review_round,
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
        session_id = str(session_resp.session_id)

        # Cache the PR → session mapping in Redis.
        await _set_pr_review_state(repo_full_name, pr_number, session_id, review_round)

        logger.info(
            "github_webhook_session_created",
            pr_number=pr_number,
            repo=repo_full_name,
            action=action,
            session_id=session_id,
            review_round=review_round,
        )
        return Response(
            content=json.dumps({"ok": True, "session_id": session_id, "review_round": review_round}),
            media_type="application/json",
        )
    except Exception as exc:
        logger.exception(
            "github_webhook_session_failed",
            pr_number=pr_number,
            repo=repo_full_name,
        )
        raise HTTPException(status_code=500, detail=f"Failed to start review session: {exc}") from exc
