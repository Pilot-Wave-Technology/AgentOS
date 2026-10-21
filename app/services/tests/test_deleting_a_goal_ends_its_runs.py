"""Deleting a goal releases the compute it was holding.

OBSERVED ON NCAL1, a deleted goal: six resources, two git_nodes and
thirteen executions still there, and the executions are the ones that
cost something.

WHY THE CASCADE MISSED THEM. `delete_goal` cancels the goal's TASKS and
STORIES — "the root fix; the per-loop goals.status='active' guards are
the complement" — and the reclaim sweep frees a workspace on exactly
three signals:

    s.state IN ('done', 'reviewed')
 OR e.state = 'CANCELLED'
 OR (e.state = 'FAILED' AND idle for STALE_HOURS)

`cancelled` is none of them. So the story went terminal, the execution
stayed RUNNING or PARKED, and `reclaim` — which asks the SUBSTRATE what
workspaces exist and the database which are finished with — correctly
answered "not this one" for every claim the dead goal held.

WHAT THAT ADDS UP TO. The namespace quota is
`persistentvolumeclaims: "40"` and a claim survives its pod on purpose
(preemption). It is not forty concurrent executions; it is forty ever.
A few deleted goals is the difference between a story that starts and
one that cannot be placed at all — and the symptom arrives much later,
on a different goal, as "nothing runs".

THROUGH THE MAILBOX. `cancel` is a LOOP_KIND with an edge from every
non-terminal state; a running leg takes it at its next step boundary, a
parked one ends on the next sweep. Writing `state = 'CANCELLED'`
directly would skip the lease release, the admission slot and the
journal entry — the machine disagreeing with its own log — so this uses
the same path the story-thread Cancel button uses.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import kernel_goal_teardown as teardown


class _Conn:
    def __init__(self, ids): self.ids, self.closed = ids, False

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return [{"id": i} for i in self.ids]

    async def close(self): self.closed = True


@pytest.fixture()
def wired(monkeypatch):
    sent: list = []

    def _use(ids, result=1):
        conn = _Conn(ids)

        async def _conn():
            return conn

        async def _submit(c, actor, kind, payload, **kw):
            sent.append((actor, kind, payload, kw.get("execution_id")))
            return result

        monkeypatch.setattr(
            "app.services.kernel_run_control._kernel_conn", _conn)
        monkeypatch.setattr("kernel.mailbox.api.submit_proposal", _submit)
        return conn, sent
    return _use


@pytest.mark.asyncio
async def test_every_live_run_of_the_goal_is_cancelled(wired):
    conn, sent = wired([11, 12, 13])
    assert await teardown.cancel_goal_runs("NCAL1", "u1") == {
        "cancelled": 3, "refused": 0}
    assert [s[3] for s in sent] == [11, 12, 13]
    assert {s[1] for s in sent} == {"cancel"}


@pytest.mark.asyncio
async def test_it_asks_only_for_the_runs_that_have_not_finished(wired):
    conn, _ = wired([])
    await teardown.cancel_goal_runs("NCAL1")
    assert "goal_id = $1" in conn.sql
    assert "state <> ALL($2::text[])" in conn.sql
    assert conn.args[0] == "NCAL1"
    assert set(conn.args[1]) == {"DONE", "FAILED", "CANCELLED"}


@pytest.mark.asyncio
async def test_one_refusal_does_not_strand_the_rest(wired):
    """A goal is being deleted. A partially torn-down goal is the state
    this exists to prevent, so a run that will not cancel is counted and
    stepped over."""
    class _Refusal:
        message = "no"

    conn, sent = wired([1, 2], result=_Refusal())
    assert await teardown.cancel_goal_runs("NCAL1") == {
        "cancelled": 0, "refused": 2}
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_the_connection_is_closed_even_when_every_run_refuses(wired):
    conn, _ = wired([1], result=None)
    await teardown.cancel_goal_runs("NCAL1")
    assert conn.closed


# ── and it is actually wired into the delete ─────────────────────────

def test_delete_goal_calls_it():
    from app.routers import goals

    src = inspect.getsource(goals.delete_goal)
    assert "cancel_goal_runs" in src


def test_it_does_not_take_the_delete_down_with_it():
    """The goal is already marked deleted by this point. A kernel that
    will not answer must not leave a half-deleted goal behind."""
    from app.routers import goals

    src = inspect.getsource(goals.delete_goal)
    block = src[src.index("cancel_goal_runs") - 400:
                src.index("cancel_goal_runs") + 400]
    assert "except Exception" in block


def test_the_terminal_set_is_not_restated():
    """Two lists of terminal states drift. This one is imported from the
    module that already owns it."""
    src = inspect.getsource(teardown.cancel_goal_runs)
    assert "TERMINAL" in src
    assert '"DONE"' not in src and "'DONE'" not in src
