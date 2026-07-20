"""Stable protocol contract for all git providers.

Any code outside this package should import from here, not from implementation
files, so adding a new provider (or upgrading an existing one) is a one-file
change with no ripple through the orchestrator or webhook layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class MRResult:
    mr_id: str          # provider-specific identifier (PR number, MR iid)
    mr_url: str         # human-readable URL for UI display and ledger events
    source_branch: str
    target_branch: str


@dataclass
class MergeResult:
    success: bool
    conflict_files: list[str] = field(default_factory=list)  # populated on success=False
    new_trunk_sha: str = ""                                   # populated on success=True


@dataclass
class MRStatus:
    mr_id: str
    state: str      # 'open' | 'merged' | 'closed' | 'conflict'
    mergeable: bool
    mr_url: str


@dataclass
class BranchProtection:
    branch: str
    pr_review_required: bool    # True → Direct Push is not allowed; only MR path
    approvals_required: int     # 0 if pr_review_required=False
    raw: dict = field(default_factory=dict)  # provider response for debugging


class GitProvider(Protocol):
    """Provider-agnostic contract for all git operations used by the integration flow.

    Implementations must be versioned: each implementation file carries a comment
    at the top documenting the API version it was written against. If the provider
    releases a breaking update, only the implementation file changes; this protocol
    and the orchestrator remain untouched.
    """

    async def create_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MRResult: ...

    async def merge_mr(self, mr_id: str) -> MergeResult: ...

    async def update_mr(self, mr_id: str, description: str) -> None: ...

    async def get_mr_status(self, mr_id: str) -> MRStatus: ...

    async def close_mr(self, mr_id: str, reason: str) -> None:
        """Close the MR with a comment explaining why (used on story cancellation)."""
        ...

    async def direct_push_merge(
        self, source_branch: str, target_branch: str
    ) -> MergeResult:
        """Fast-forward merge source into target and push target — no MR created."""
        ...

    async def get_branch_protection(self, branch: str) -> BranchProtection:
        """Query branch protection to determine which landing modes are allowed."""
        ...

    async def get_default_branch(self) -> str:
        """Return the repo's default branch name — fallback if no explicit trunk is set."""
        ...

    async def register_webhook(
        self,
        repo_url: str,
        events: list[str],
        target_url: str,
        secret: str,
    ) -> str:
        """Register a webhook on the repo. Returns the provider's webhook ID."""
        ...

    async def delete_webhook(self, webhook_id: str) -> None: ...
