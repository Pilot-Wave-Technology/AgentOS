"""Who is this log line about? One contextvar, set at boundaries.

WHY A CONTEXTVAR AND NOT AN ARGUMENT. There are 482 `logger.*` calls
across app/ and kernel/. Threading ids through them is a change to every
one of those call sites and to every function between the boundary and
the log line -- which is the kind of change that gets half-done and then
looks done. A contextvar is set once where the work is identified and
read by a logging filter; nothing in between has to know.

WHY IT WORKS UNDER ASYNCIO. A `contextvars.Context` is copied into a
task at creation, so a leg spawned onto the LegSupervisor carries its
own execution's ids and cannot see or clobber a sibling's. That is
exactly the shape the worker needs, and it is why this is not a global
dict with a lock.

WHAT GOES IN. Only identifiers, and only ones that are cheap to carry:
this dict is read on every log record. Never a payload, never a secret,
never anything that has to be computed. `kernel/obs/fields.py` is the
list, and it is the file to edit when the structure changes.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("agentos_log_ctx")


def current() -> Mapping[str, Any]:
    """The ids in force right now. Empty outside any bound scope."""
    return _CTX.get({})


def bind(**fields: Any) -> contextvars.Token:
    """Add ids to the current context. Returns a token for `unbind`.

    Merges rather than replaces, so a leg can add `leg=4` without
    knowing that the tick above it already set `execution_id`. Values
    that are None are dropped rather than stored -- a field that is
    absent should be absent from the log line, not present and null.
    """
    merged = dict(_CTX.get({}))
    merged.update({k: v for k, v in fields.items() if v is not None})
    return _CTX.set(merged)


def unbind(token: contextvars.Token) -> None:
    _CTX.reset(token)


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Scope some ids to a block.

        with bound(execution_id=13616, leg=4):
            ...                      # every log line in here carries them

    Use this at the six boundaries in `fields.BOUNDARIES`. Everywhere
    else, just log -- the context is already right.
    """
    token = bind(**fields)
    try:
        yield
    finally:
        unbind(token)


def clear() -> None:
    """Drop everything. For tests, and for a worker tick that is about to
    move to a different execution on the same task."""
    _CTX.set({})
