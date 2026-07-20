"""In-process per-repo locks (plan §5.0).

The git engine runs as a single-instance worker (one leader), so per-repo
serialization is just an in-process `asyncio.Lock` per repo — NO distributed
advisory lock is needed (the global `gitgraph:engine-leader` lock guarantees a
single active engine; these locks serialize that one leader's own per-repo lanes
while letting different repos proceed in parallel).

A multi-repo merge group acquires its repos' locks in ASCENDING `git_repo_id`
order (a deterministic total order) so two groups can never hold-and-wait in a
cycle (plan §5.0 cross-repo ordering).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_locks: dict[str, asyncio.Lock] = {}


def repo_lock(git_repo_id: str) -> asyncio.Lock:
    """The (process-wide) lock for a repo, created on first use."""
    lk = _locks.get(git_repo_id)
    if lk is None:
        lk = asyncio.Lock()
        _locks[git_repo_id] = lk
    return lk


@asynccontextmanager
async def repo_locked(git_repo_id: str):
    """Hold one repo's lane lock for the duration of the block."""
    lk = repo_lock(git_repo_id)
    await lk.acquire()
    try:
        yield
    finally:
        lk.release()


@asynccontextmanager
async def repos_locked(git_repo_ids: list[str]):
    """Hold several repos' locks (a multi-repo merge group), acquired in ascending
    git_repo_id order to avoid cross-group deadlock. Releases in reverse order."""
    ordered = sorted(set(git_repo_ids))
    acquired: list[asyncio.Lock] = []
    try:
        for rid in ordered:
            lk = repo_lock(rid)
            await lk.acquire()
            acquired.append(lk)
        yield
    finally:
        for lk in reversed(acquired):
            lk.release()
