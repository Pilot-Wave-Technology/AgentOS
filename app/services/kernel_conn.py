"""One way for control-plane code to reach the kernel's schema.

WHY THIS MODULE EXISTS. The kernel's own SQL speaks UNQUALIFIED names
against the `kernel` schema -- `executions`, `questions`,
`step_journal` -- because the conductor and the pods run on connections
whose `search_path` is `kernel,public`. The control panel's pooled
connections do not carry that path, so the same statement that works
inside a pod raises `relation "executions" does not exist` here.

The fix is a connection that carries the path as a SERVER SETTING,
never a `SET search_path` on a shared pooled connection -- a pooled
connection is handed to the next caller with whatever you left on it.

FOUR COPIES OF THIS, before this module. `kernel_park`, `kernel_decide`,
`kernel_run_control` and `story_landing` each built the same connection
the same way, and three of them carried a comment pointing at one of the
others as the reason. Four places to remember is three too many: the
failure mode is not a wrong connection, it is a NEW caller that does not
know a special one is needed and uses the pool, which works in every
test that does not touch the kernel schema.

WHAT IS DELIBERATELY NOT HERE. `kernel/` has its own connection helpers
(`assistant/asgi.py`, `loopbridge.py`, `null_executor.py`) and they stay
separate: they resolve a different DSN and `kernel/` does not import
`app.config`, which is a boundary worth more than the duplication.
"""
from __future__ import annotations

import os

# The path the kernel's unqualified names need. `public` second so the
# control panel's own tables stay reachable on the same connection --
# several callers touch both in one unit of work.
KERNEL_SEARCH_PATH = "kernel,public"


def kernel_dsn() -> str:
    """The DSN for the kernel schema.

    `KERNEL_DATABASE_URL` wins when set: the kernel lives in the same
    database today, and the override is what lets it move without a
    code change.
    """
    from app.config import get_settings

    return (os.environ.get("KERNEL_DATABASE_URL", "").strip()
            or get_settings().database_url)


async def kernel_conn():
    """A fresh connection whose `search_path` reaches the kernel.

    The caller owns it and must close it. NOT pooled, and not to be
    wrapped in an outer transaction by callers that change role --
    `kernel_decide` documents at length why that combination once
    leaked a role across a savepoint.
    """
    import asyncpg

    return await asyncpg.connect(
        kernel_dsn(), server_settings={"search_path": KERNEL_SEARCH_PATH})
