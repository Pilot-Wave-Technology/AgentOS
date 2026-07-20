"""Git webhook receiver — FastAPI router for POST /webhooks/git.

Validates signatures (GitHub HMAC-SHA256, GitLab token header), deduplicates
deliveries, parses the payload into a GitEvent, and routes to event_dispatcher.

Built to the channels inbound adapter shape so it can later move under
app/channels/github/inbound/ when the full GitHub channel is built.
All provider-format parsing stays in per-provider handler files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.database import get_pool
from .dedup import is_duplicate, mark_processed
from . import github_handler, gitlab_handler
from .event_dispatcher import dispatch

_log = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/git")
async def receive_git_webhook(request: Request):
    """Receive, validate, deduplicate, and dispatch a git provider webhook event."""
    pool = get_pool()
    body = await request.body()
    headers = request.headers

    # Detect provider from event-type headers.
    provider = _detect_provider(headers)
    if not provider:
        _log.warning("receive_git_webhook: could not detect provider from headers")
        raise HTTPException(status_code=400, detail="Unknown git provider")

    # Delivery-ID deduplication (at-least-once delivery from GitHub/GitLab).
    delivery_id = _get_delivery_id(provider, headers)
    if delivery_id and await is_duplicate(pool, "git", delivery_id):
        _log.info("receive_git_webhook: duplicate delivery_id=%s provider=%s — ack without reprocessing", delivery_id, provider)
        return {"ok": True, "duplicate": True}

    # Signature validation — reject unsigned or tampered payloads before parsing.
    secret = await _get_webhook_secret(request, provider, pool)
    if not _validate_signature(provider, body, headers, secret):
        _log.warning("receive_git_webhook: invalid signature from provider=%s", provider)
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    payload = json.loads(body)

    # Per-provider parsing → provider-agnostic GitEvent.
    if provider == "github":
        event = github_handler.handle(payload)
    elif provider == "gitlab":
        event = gitlab_handler.handle(payload)
    else:
        event = None

    if event:
        await dispatch(event)

    # Mark processed AFTER successful dispatch to avoid silently dropping on crash.
    if delivery_id:
        await mark_processed(pool, "git", delivery_id)

    _log.info("receive_git_webhook: processed provider=%s delivery=%s kind=%s", provider, delivery_id, event.kind if event else "none")
    return {"ok": True}


def _detect_provider(headers) -> str | None:
    if headers.get("X-GitHub-Event"):
        return "github"
    if headers.get("X-Gitlab-Event"):
        return "gitlab"
    return None


def _get_delivery_id(provider: str, headers) -> str | None:
    if provider == "github":
        return headers.get("X-GitHub-Delivery")
    if provider == "gitlab":
        # GitLab sends a unique ID in X-Gitlab-Event-UUID (17.x+)
        return headers.get("X-Gitlab-Event-UUID") or headers.get("X-Gitlab-Delivery")
    return None


def _validate_signature(provider: str, body: bytes, headers, secret: str | None) -> bool:
    if not secret:
        # No secret configured — only accept if in development mode.
        _log.warning("_validate_signature: no webhook secret configured — rejecting by default")
        return False

    if provider == "github":
        sig_header = headers.get("X-Hub-Signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_header, expected)

    if provider == "gitlab":
        # GitLab sends the secret as a plain token header
        return hmac.compare_digest(headers.get("X-Gitlab-Token", ""), secret)

    return False


async def _get_webhook_secret(request: Request, provider: str, pool) -> str | None:
    """Look up the per-repo webhook secret from goal_resources.metadata.webhook_secret.

    Looks up the repo by matching clone_url/repo_owner+repo_name from the request body.
    Returns None if no secret is configured for this repo.
    """
    try:
        body = await request.body()
        payload = json.loads(body)
        repo = payload.get("repository", {}) or payload.get("project", {})
        # GitHub: repository.full_name or html_url
        # GitLab: project.path_with_namespace or http_url
        full_name = repo.get("full_name") or repo.get("path_with_namespace", "")

        if not full_name:
            return None

        owner, _, repo_name = full_name.partition("/")
        row = await pool.fetchrow(
            """SELECT metadata FROM goal_resources
               WHERE repo_owner = $1 AND repo_name = $2 AND kind IN ('git_repo', 'repo')
               LIMIT 1""",
            owner, repo_name,
        )
        if not row:
            return None
        meta = row["metadata"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        return meta.get("webhook_secret")
    except Exception:
        _log.exception("_get_webhook_secret: failed to look up secret")
        return None
