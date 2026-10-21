"""Who is watching which GO thread, in this process.

WHAT CHANGED AND WHY. This registry was keyed by `goal_id` alone, and
that was exactly right while a goal had one GO thread: every subscriber
of the goal was a subscriber of the only conversation there was.

Autonomous errands break that. An approved errand opens a thread of its
own on the GO, so a goal now has a main thread plus one per live errand,
and a goal-keyed fan-out delivers every errand's replies into every
open tab. Worse than noise: the GO's answer to a question asked in
errand A renders inside errand B's conversation, where it is wrong and
looks authoritative.

So the key is `(goal_id, thread_id)`.

THE GOAL-WIDE FAN-OUT IS KEPT, deliberately, as `thread_id=None`. Every
caller in this tree passes a thread id today and none needs it -- but a
broadcast that silently reached nobody would be an invisible
regression, and a caller added later without a thread id should degrade
to the old behaviour rather than to silence. `None` is a widening, never
a narrowing.

NOT A MESSAGE BUS. Everything here is per-process, in-memory, and dies
with the worker; the durable record is `messages` and the durable
fan-out is the client's next fetch. A dropped subscriber is a tab that
refreshes, not a lost message -- which is why `broadcast` discards a
send function that raises instead of retrying it.
"""

from __future__ import annotations

# (goal_id, thread_id) -> set of websocket send functions.
_subscribers: dict[tuple[str, str], set] = {}


def subscribe(goal_id: str, thread_id: str, send_fn):
    """Register a send function and hand back its unsubscribe."""
    key = (goal_id, thread_id)
    _subscribers.setdefault(key, set()).add(send_fn)

    def unsubscribe():
        live = _subscribers.get(key)
        if live is None:
            return
        live.discard(send_fn)
        if not live:
            _subscribers.pop(key, None)

    return unsubscribe


def _targets(goal_id: str, thread_id: str | None) -> list:
    if thread_id is not None:
        return list(_subscribers.get((goal_id, thread_id), ()))
    # See the module docstring: no thread named means every thread of
    # this goal, which is what the goal-keyed registry always did.
    out: list = []
    for (gid, _tid), fns in _subscribers.items():
        if gid == goal_id:
            out.extend(fns)
    return out


async def broadcast(goal_id: str, message: str, thread_id: str | None = None):
    """Push one frame to everyone watching, and forget whoever is gone."""
    for send_fn in _targets(goal_id, thread_id):
        try:
            await send_fn(message)
        except Exception:                                   # noqa: BLE001
            for key, fns in list(_subscribers.items()):
                if key[0] == goal_id:
                    fns.discard(send_fn)
                    if not fns:
                        _subscribers.pop(key, None)


def watcher_count(goal_id: str, thread_id: str | None = None) -> int:
    """How many tabs are open on this, for logging only."""
    return len(_targets(goal_id, thread_id))
