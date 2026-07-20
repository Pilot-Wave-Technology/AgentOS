"""Parse GitHub webhook payloads into provider-agnostic GitEvent objects."""
from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass
class GitEvent:
    """Provider-agnostic event produced by a handler and consumed by event_dispatcher."""
    kind: str       # 'mr_merged' | 'mr_approved' | 'mr_closed' | 'mr_opened'
    mr_id: str      # provider's own identifier (PR number as string)
    mr_url: str
    source_branch: str
    target_branch: str
    repo_full_name: str   # '{owner}/{repo}' — used to look up the goal_resource


def handle(payload: dict) -> GitEvent | None:
    """Parse a GitHub pull_request or pull_request_review event.

    Returns a GitEvent for events we care about, None for everything else.
    The dispatcher only sees GitEvent objects — it does not know which provider
    produced them.
    """
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    review = payload.get("review", {})
    repo = payload.get("repository", {})

    if not pr:
        _log.debug("github_handler: no pull_request in payload (action=%s) — skipping", action)
        return None

    mr_id = str(pr.get("number", ""))
    mr_url = pr.get("html_url", "")
    source_branch = pr.get("head", {}).get("ref", "")
    target_branch = pr.get("base", {}).get("ref", "")
    repo_full_name = repo.get("full_name", "")

    if action == "closed" and pr.get("merged"):
        _log.info("github_handler: PR #%s merged (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_merged", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if action == "submitted" and review.get("state") == "approved":
        _log.info("github_handler: PR #%s approved (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_approved", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if action == "closed" and not pr.get("merged"):
        _log.info("github_handler: PR #%s closed without merge (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_closed", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if action == "opened":
        _log.debug("github_handler: PR #%s opened — informational only", mr_id)
        return GitEvent(
            kind="mr_opened", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    _log.debug("github_handler: uninteresting event action=%s pr=%s", action, mr_id)
    return None
