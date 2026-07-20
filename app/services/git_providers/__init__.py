"""Git provider abstraction layer.

Provider-agnostic interface for MR lifecycle, branch operations, webhooks,
and branch-protection queries. Implementations: github.py, gitlab.py.

Deliberately standalone (not buried in merge_orchestrator) so the eventual
app/channels/github/ channel integration can reuse the same providers rather
than reimplementing the API. See documentation/story-integration/GIT_PROVIDERS.md.
"""
from .base import BranchProtection, GitProvider, MergeResult, MRResult, MRStatus
from .factory import get_provider

__all__ = [
    "GitProvider",
    "MRResult",
    "MergeResult",
    "MRStatus",
    "BranchProtection",
    "get_provider",
]
