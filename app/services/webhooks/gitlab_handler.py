"""Parse GitLab webhook payloads into provider-agnostic GitEvent objects."""
from __future__ import annotations

import logging

from .github_handler import GitEvent  # shared dataclass — provider-agnostic

_log = logging.getLogger(__name__)


def handle(payload: dict) -> GitEvent | None:
    """Parse a GitLab merge_request event.

    Returns a GitEvent for events we care about, None for everything else.
    The dispatcher only sees GitEvent objects — it does not know which provider
    produced them.
    """
    kind = payload.get("object_kind")
    if kind != "merge_request":
        _log.debug("gitlab_handler: object_kind=%s — skipping", kind)
        return None

    attrs = payload.get("object_attributes", {})
    repo = payload.get("project", {})

    # GitLab uses iid (project-scoped integer) for API calls — always store iid,
    # never the global id.
    mr_id = str(attrs.get("iid", ""))
    mr_url = attrs.get("url", "")
    source_branch = attrs.get("source_branch", "")
    target_branch = attrs.get("target_branch", "")
    repo_full_name = repo.get("path_with_namespace", "")
    state = attrs.get("state", "")
    action = attrs.get("action", "")

    if state == "merged":
        _log.info("gitlab_handler: MR !%s merged (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_merged", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if action == "approved":
        _log.info("gitlab_handler: MR !%s approved (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_approved", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if state == "closed":
        _log.info("gitlab_handler: MR !%s closed (%s)", mr_id, repo_full_name)
        return GitEvent(
            kind="mr_closed", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    if action == "open":
        _log.debug("gitlab_handler: MR !%s opened — informational only", mr_id)
        return GitEvent(
            kind="mr_opened", mr_id=mr_id, mr_url=mr_url,
            source_branch=source_branch, target_branch=target_branch,
            repo_full_name=repo_full_name,
        )

    _log.debug("gitlab_handler: uninteresting event state=%s action=%s mr=%s", state, action, mr_id)
    return None
