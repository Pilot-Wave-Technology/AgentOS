"""Resolve (kind, domain, phase) to a LOCAL executor profile.

A SEPARATE TREE, not a variant of the pod one. The pod profiles describe a
sandbox: `story_commit`, `bash_wait`, `run_check`, `propose_completion`,
one-tool-per-step, the refusal envelope, legs that end and rehydrate. None of
that exists when Claude Code is the executor in somebody's editor -- there is
no leg, no step budget, no envelope, and the agent already owns a terminal.
Describing a local run with a pod profile means either teaching the session a
machine it is not running on, or quietly reinterpreting half the profile and
hoping the prose does not matter. It does: the prose is the role.

So `profiles-local/` is its own tree, loaded by the same loader, keyed the same
way, and edited the same way -- `profile.toml` for what a local executor may
do, `prompt.md` for who it is. Changing a pod profile does not change a local
run and vice versa.

WHAT STAYS SHARED, deliberately: the KEY. A story resolves to `(kind, domain,
phase)` by one derivation (`kernel.profiles.story_keys`) whichever lane it runs
in, so "which profile is this" has one answer and only "what does that profile
say" differs. A second derivation is the failure nobody looks for -- both runs
work and disagree about what the agent may do.

Fails closed, like its sibling: a pair with no local profile is a story that
cannot be imported, and saying so beats approximating it with the pod one.
"""

from __future__ import annotations

import os
import threading

from kernel.profiles.loader import load_all
from kernel.profiles.record import Profile, UnknownProfileError

DEFAULT_LOCAL_PROFILES_DIR = os.environ.get("AGENTOS_LOCAL_PROFILES_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "profiles-local",
)

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], Profile] | None = None


def _profiles() -> dict[tuple[str, str, str], Profile]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = load_all(DEFAULT_LOCAL_PROFILES_DIR)
    return _cache


def reload() -> None:
    """Drop the cache so the next resolve re-reads from disk."""
    global _cache
    with _lock:
        _cache = None


def resolve_local(kind: str, domain: str, phase: str = "") -> Profile:
    """One local profile, or UnknownProfileError naming what exists locally.

    The message says LOCAL explicitly. The same pair may well have a pod
    profile, and "no profile for kind='story' domain='business'" would send
    a reader to the wrong tree.
    """
    profiles = _profiles()
    try:
        return profiles[(kind, domain, phase or "")]
    except KeyError:
        known = ", ".join(
            f"{k}/{d}" + (f"/{ph}" if ph else "")
            for k, d, ph in sorted(profiles))
        want = f"kind={kind!r} domain={domain!r}"
        if phase:
            want += f" phase={phase!r}"
        raise UnknownProfileError(
            f"no LOCAL profile for {want} -- this story may run in a pod, but "
            f"nothing describes it running in an editor; local profiles: {known}"
        ) from None


def all_local_profiles() -> list[Profile]:
    """Every local profile, for CI checks and operator listings."""
    return [p for _, p in sorted(_profiles().items())]
