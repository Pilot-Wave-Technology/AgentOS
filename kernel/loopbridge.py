"""One asyncio loop on a background thread, shared by every request.

`http.server` is threaded and asyncpg is not, so a loop per request would
open a connection per request and exhaust the pool under polling. Both
stdlib servers in this build — the ops console and the assistant API —
need exactly this, and the second one is why it moved out of the console
rather than being copied into place.

Extracted, not rewritten. The comments below record things that were
learned the hard way and would have been lost in a re-implementation.

TWO PROCESSES NOW SHARE THIS POOL AND THEY ARE NOT THE SAME SHAPE
(D2-prep, plan §10a and §11.5a prerequisites 1 and 2). The conductor
connects as the superuser `agentos` and runs every loop in the build; the
execution pod connects through its own Cloud SQL proxy as `agentos_leg`
and runs ONE leg. Both questions below — which role to adopt, and how
many sockets to hold — used to have one answer because there was one
caller, and the pod makes each of them wrong in a different direction.

WHY EACH QUESTION IS ANSWERED BY A DIFFERENT MECHANISM, deliberately:

  * The ROLE is answered by the DATABASE, because the wrong answer is a
    privilege hole and configuration is not evidence. `pg_has_role`
    against the live connection cannot drift from the truth; a name
    comparison can, and already has — `migrations/kernel_execution_pod_
    leg_role.sql` creates `agentos_leg` while `scripts/infra/deploy_
    execution_plane.sh:226` puts `agentos_leg_${ENV_NAME}` in the Secret,
    so any mechanism that matched the DSN's username would have been
    born broken in every environment but one.

  * The SIZE is answered by the ENVIRONMENT, because it has to be decided
    BEFORE a connection exists — `max_size` is fixed at `create_pool`
    time and there is nothing to ask. The signal is the presence of the
    variable the pod builds its own DSN from, so "am I the pod" cannot
    drift from "can I reach a database at all": if that variable is
    absent the pod has no connection to size.

  * Neither is a `make_pool(dsn, size=...)` argument, though that reads
    tidier. `worker/service_pool.py::wait_for_pool` and
    `harness/leg_runner.py::run` both call this with a bare DSN, and a
    caller-declared size is a second declaration of a fact the process
    already carries — the same defect class as the invisible default
    `ci/check_no_environ_default.py` bans. A future reader adding a
    third caller must not have to know which one it is.

    RE-CHECKED AT D2, NOT JUST REPOINTED. The old citation was
    `worker/service.py:438`, and wave 4b split that file 299 -> 110, so
    the line is in `service_pool.py` now — but a moved pointer is the
    cheap half. The claim it supports is that BOTH callers still hand
    this a bare DSN, and D2 is exactly the change that could have broken
    it: `leg_runner` is now the pod's own entry point, running under
    `LEG_POOL_MAX = 2` instead of 16, and the tempting shape was to let
    it say so. It does not, and must not. The pod already declares
    itself by holding `AGENTOS_LEG_DB_USER` — that is the fact the size
    is derived from — so a `size=` argument would be a SECOND way to say
    "I am the pod", and the two could disagree. Named by SYMBOL rather
    than by line number for the reason 11.5f records: an anchor pinned
    to a path its subject has left matches nothing and proves nothing.

Do not "simplify" either back to an unconditional constant. The pod's
first failure would then be `permission denied to set role`, which is a
confusing error about the wrong thing, instead of `permission denied for
table`, which is the boundary working.
"""

from __future__ import annotations

import asyncio
import os
import threading

import asyncpg

# Bigger than the number of things that can hold one at once: the sweep
# holds a connection for a whole pass, each drive holds one for a whole
# run, and every HTTP request needs one. At 4 the worker could starve
# the page it is meant to be visible in. Unchanged, and every comment
# elsewhere that cites "POOL_MAX is 16" is talking about this process.
POOL_MIN, POOL_MAX = 2, 16

# THE EXECUTION POD, which runs one leg and holds its connection for the
# whole of it. `agentos_leg` carries `CONNECTION LIMIT 80` — §10a's
# arithmetic is 20 pods x 4 — so at 16 apiece four pods exhaust the role
# and the fifth gets a connection error that reads as a database outage.
# 1 rather than 2 because a one-leg process has nothing to overlap with;
# 2 as the ceiling only so that a leg needing a second connection for a
# moment waits on nothing. Raising this is raising a shared ceiling: 20
# pods x this number must stay under 80.
LEG_POOL_MIN, LEG_POOL_MAX = 1, 2

# Named once. `kernel/drivers/k8s_sqlproxy.py::DB_KEYS` is what puts this
# into the pod's WORKER container and nowhere else — not the sandbox, not
# the conductor — and a test asserts the two spellings still agree rather
# than trusting this copy. Spelled out here rather than imported from
# that module, because it pulls in `kernel.spec` and `kernel.workspace`
# and this file sits underneath the console, the assistant API and the
# credentials sidecar — a pool must not need a pod manifest to import.
LEG_DB_ENV = "AGENTOS_LEG_DB_USER"

WORKER_ROLE = "agentos_worker"

# EXISTS-shaped on purpose. A bare `pg_has_role(current_user,
# 'agentos_worker', 'MEMBER')` RAISES when the role is absent, which is
# the state of any database the role seed has not reached — and a pool
# that cannot be built is a worse failure than a grant that is not
# enforced yet. No rows means false means "do not adopt", and what
# follows is `permission denied for table` if anything is actually
# protected, which is the error a reader can act on.
HOLDS_ROLE_SQL = ("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1"
                  " AND pg_has_role(current_user, oid, 'MEMBER'))")

# Bounded. Without a timeout a request that cannot get a connection waits
# for ever and takes its thread with it.
CALL_TIMEOUT_S = 45


def in_the_execution_pod() -> bool:
    """Does this process have a database of its own, as the narrow role?

    Membership and not `os.environ.get(x, default)`: the ban exists
    because a default nobody can see is a policy nobody reviewed, and
    `k8s_sqlproxy.from_env` makes the same call for the same reason.
    Read per call rather than at import so a test can set it.
    """
    return LEG_DB_ENV in os.environ


def pool_sizes() -> tuple[int, int]:
    """`(min, max)` for THIS process. See the module docstring."""
    if in_the_execution_pod():
        return LEG_POOL_MIN, LEG_POOL_MAX
    return POOL_MIN, POOL_MAX


async def _as_worker(conn):
    """F1 — every pooled connection that CAN be `agentos_worker` is.

    That role cannot write `criterion_results` and cannot edit or delete
    a criterion. The grant is only a boundary if something actually
    adopts the role: a rule present but ungated is the defect class this
    build order exists to avoid.

    `agentos` is a superuser and a superuser bypasses every grant, so
    this SET ROLE is what makes the refusal real for the running system
    rather than only for the test. Migrations are unaffected —
    `apply.py` opens its own connection and never comes through here.
    A superuser is a member of every role, so the conductor still adopts
    it and nothing about the conductor changed.

    CONDITIONAL SINCE D2-prep, and NOT because the pod is trusted.
    `agentos_leg` was deliberately never granted membership in
    `agentos_worker` (§10a): granting it would have silenced the error
    and the boundary with it. So the pod must not ask, and asking the
    database whether it may is the only test that stays true when a
    deploy renames something.

    One extra round trip per NEW connection — at most 16 in the
    conductor's lifetime, 1 in the pod's — and none per query, because
    asyncpg runs `init` once per connection and not once per acquire.

    The role is interpolated because `SET ROLE` takes no parameter. It is
    a module constant beside the query that checks it, never a value off
    the wire; keep it that way.
    """
    if await conn.fetchval(HOLDS_ROLE_SQL, WORKER_ROLE):
        await conn.execute(f"SET ROLE {WORKER_ROLE}")


async def make_pool(dsn: str):
    """The kernel's pool, configured in ONE place.

    Both callers need identical settings and for different reasons: the
    role gate above is a security boundary, and `search_path` is what
    makes every unqualified table name resolve to the kernel schema. A
    second copy of this call is a second thing to forget to change --
    which is the defect class this build spends most of its time on.

    `LoopRuntime` wraps it for the threaded stdlib servers; the headless
    worker service awaits it directly on its own loop.

    The two settings that now differ by process are the two this module
    computes for itself, so that stays true: there is still exactly one
    `create_pool` call under `kernel/`, and no caller passes it a number.
    """
    min_size, max_size = pool_sizes()
    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=_as_worker,
        server_settings={"search_path": "kernel,public"})


class LoopRuntime:
    """A loop, a pool, and a way to call into them from a request thread."""

    def __init__(self, dsn: str):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        # Wait for the loop to actually be running. Scheduling onto a
        # loop that has not started yet fails intermittently, which is
        # the worst kind of start-up bug: it works on a warm machine.
        self._ready.wait(10)

        # `make_pool` is a coroutine function, which is what
        # run_coroutine_threadsafe needs -- create_pool returns an
        # awaitable Pool rather than a coroutine, so it cannot be passed
        # directly. That is the reason for the wrapper, not style.
        self.pool = self.call(make_pool(dsn))

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(
            coro, self.loop).result(CALL_TIMEOUT_S)
