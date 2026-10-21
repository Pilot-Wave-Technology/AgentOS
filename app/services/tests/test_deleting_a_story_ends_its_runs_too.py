"""A cancelled story must give back its pod, its disk and its verifiers.

Phase X. Deleting a GOAL already worked: `app/routers/goals.py` calls
`cancel_goal_runs`, which submits a real `cancel` through the mailbox --
so the run takes the edge at its next step boundary and the lease, the
admission slot and the journal entry all follow. Writing
`state = 'CANCELLED'` straight into the table would skip all four.

A CANCELLED STORY HAD NO SUCH PATH. `stories.state = 'cancelled'` is
written in at least two places -- `git_graph/merge_lane.py` and
`routers/internal_assistant.py` -- and NEITHER touches the executions.
`reclaim.releasable` frees a workspace on `s.state IN ('done',
'reviewed')`, `e.state = 'CANCELLED'`, or FAILED-and-idle; `cancelled` is
none of them. So the story went terminal and its run kept its pod, its
PVC, its admission slot and its legs, with nothing saying why. Observed
on NCAL1: a deleted goal still holding thirteen executions.

AND THE VERIFIER'S JOBS were never reaped by anything but `run_job`'s own
`finally` -- which does not run if the conductor dies, and which nothing
triggers when an execution is cancelled while its criteria are still
going. `ttlSecondsAfterFinished` only starts counting once a Job
COMPLETES, so a wedged one is bounded by `activeDeadlineSeconds` alone,
holding a slot against `count/jobs.batch: 150`.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from kernel.conductor import abandoned, jobs_sweep

SWEEP = (pathlib.Path(abandoned.__file__).parent
         / "sweep.py").read_text(encoding="utf-8")
RECLAIM = (pathlib.Path(abandoned.__file__).parent
           / "reclaim.py").read_text(encoding="utf-8")


# ── X1 · the runs ──────────────────────────────────────────────────────

def test_a_cancelled_story_and_a_deleted_goal_both_count():
    sql = abandoned.ABANDONED_SQL
    assert "s.state IN ('cancelled', 'rejected')" in sql
    assert "g.id IS NULL" in sql


def test_terminal_is_read_from_the_executions_own_machine():
    """A run carried forward has different terminal states, and judging
    one by another machine's is how a live run gets reaped."""
    assert "run_state_machine" in abandoned.ABANDONED_SQL
    assert "terminal_states @> to_jsonb(e.state)" in abandoned.ABANDONED_SQL


def test_an_errand_without_a_story_is_not_swept():
    """LEFT JOIN, so a run with no story is not treated as one whose
    story vanished."""
    assert "LEFT JOIN stories" in abandoned.ABANDONED_SQL
    assert "LEFT JOIN goals" in abandoned.ABANDONED_SQL


def test_cancelling_goes_through_the_mailbox():
    """Writing CANCELLED directly would skip the lease release, the
    admission slot and the journal entry -- the machine disagreeing with
    its own log."""
    src = pathlib.Path(abandoned.__file__).read_text(encoding="utf-8")
    assert "submit_proposal" in src
    assert "UPDATE executions" not in src, (
        "it must not write the state itself")


def test_one_refusal_does_not_stop_the_sweep():
    fn = next(n for n in ast.walk(ast.parse(
        pathlib.Path(abandoned.__file__).read_text(encoding="utf-8")))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "cancel_abandoned")
    assert any(isinstance(h, ast.ExceptHandler) for h in ast.walk(fn))


@pytest.mark.asyncio
async def test_it_cancels_what_it_finds(monkeypatch):
    sent = []

    class _Conn:
        async def fetch(self, sql, *a):
            return [{"id": 7, "goal_id": "G", "story_id": "s",
                     "state": "RUNNING", "why": "its story was cancelled"},
                    {"id": 8, "goal_id": "G", "story_id": None,
                     "state": "PARKED_HOT", "why": "its goal was deleted"}]

    async def _submit(conn, actor, kind, payload, idempotency_key=None):
        sent.append((kind, payload["execution_id"]))

    import kernel.mailbox.api as api
    monkeypatch.setattr(api, "submit_proposal", _submit)
    actions = await abandoned.cancel_abandoned(_Conn())
    assert sent == [("cancel", 7), ("cancel", 8)]
    assert [a["action"] for a in actions] == ["abandoned", "abandoned"]


# ── X2 · the disk, as a backstop ───────────────────────────────────────

def test_reclaim_now_recognises_a_cancelled_story():
    """Belt and braces for a run that never took the cancel edge. A
    workspace whose story was abandoned is finished with either way."""
    assert "s.state IN ('cancelled', 'rejected')" in RECLAIM


# ── X3 · the verifier's Jobs ───────────────────────────────────────────

def test_jobs_are_found_by_the_labels_that_already_existed():
    assert jobs_sweep.SELECTOR == "managed-by=kernel"
    assert jobs_sweep.OWNER_LABEL == "execution-id"
    manifest = (pathlib.Path(abandoned.__file__).parents[1]
                / "drivers" / "k8s_job.py").read_text(encoding="utf-8")
    assert '"managed-by": "kernel"' in manifest
    assert '"execution-id"' in manifest


@pytest.mark.asyncio
async def test_a_job_whose_owner_cannot_be_identified_is_kept():
    """The same rule `list_workspaces` applies to claims whose name does
    not parse: a sweep that destroys what it cannot identify destroys
    somebody's hand-made debugging Job."""
    class _Conn:
        async def fetchval(self, sql, *a):
            raise AssertionError("should not have been asked")

    assert await jobs_sweep._wanted(_Conn(), "not-a-number") is True


@pytest.mark.asyncio
async def test_a_job_whose_execution_is_gone_is_not_wanted():
    class _Conn:
        async def fetchval(self, sql, *a):
            return None

    assert await jobs_sweep._wanted(_Conn(), "42") is False


@pytest.mark.asyncio
async def test_a_live_execution_keeps_its_jobs():
    class _Conn:
        async def fetchval(self, sql, *a):
            return True

    assert await jobs_sweep._wanted(_Conn(), "42") is True


@pytest.mark.asyncio
async def test_docker_is_left_alone():
    """Docker removes its own criterion containers."""
    class _Driver:
        name = "docker"

    assert await jobs_sweep.sweep_jobs(None, _Driver()) == []


# ── the wiring ─────────────────────────────────────────────────────────

def test_both_sweeps_run_in_reconcile():
    assert "cancel_abandoned" in SWEEP
    assert "sweep_jobs" in SWEEP


def test_the_runs_are_cancelled_before_the_pods_are_swept():
    """Cancelling first means the pod, claim and Job sweeps in this same
    pass see the terminal state, instead of waiting a whole cycle."""
    assert SWEEP.index("cancel_abandoned") < SWEEP.index("orphan_sweep(conn")


def test_a_dry_run_cancels_nothing():
    """`dry_run` counts; it must never submit a cancellation."""
    block = SWEEP[SWEEP.index("cancel_abandoned"):]
    assert "if not dry_run:" in block[:400]
