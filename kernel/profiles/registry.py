"""Resolve (kind, domain) to a profile.

The profiles themselves are data on disk — see loader.py. This module is
only the lookup, plus a cache so a dispatch loop does not re-read the tree
on every execution.

Fails closed on an unknown pair. There is deliberately no default profile:
falling back to the coding profile would hand an unrecognised execution the
one surface that has `write` and `bash` in it.
"""

from __future__ import annotations

import threading

from kernel.profiles.loader import load_all
from kernel.profiles.record import Profile, UnknownProfileError

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], Profile] | None = None


def _profiles() -> dict[tuple[str, str, str], Profile]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = load_all()
    return _cache


def reload() -> None:
    """Drop the cache so the next resolve() re-reads from disk.

    Editing a prompt should not need a redeploy in development. Nothing
    calls this on a schedule: a profile changing under a running leg would
    break the stable-prefix property E6e depends on.
    """
    global _cache
    with _lock:
        _cache = None


def resolve(kind: str, domain: str, phase: str = "") -> Profile:
    """Look up one profile, or raise UnknownProfileError naming what exists.

    `phase` is the third key component and defaults to the work itself,
    so every existing caller resolves exactly what it resolved before.
    A phase with no profile FAILS CLOSED like an unknown kind — it does
    not fall back to the work profile, because the work profile is the
    one surface that holds `write` and `bash`, and a review falling back
    to it would be handed the tools it exists not to have.
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
            f"no profile for {want}; known: {known}"
        ) from None


def all_profiles() -> list[Profile]:
    """Every loaded profile, for CI checks and operator listings."""
    return [p for _, p in sorted(_profiles().items())]
