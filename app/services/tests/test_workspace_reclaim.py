"""The disk is given back, and only when the work is done with it.

`_ensure_pvc` creates a claim per execution. `destroy()` deliberately
keeps it -- "deleting the claim here would destroy the workspace every
time a Spot pod was preempted, which is the exact scenario the claim
exists to survive". The orphan sweep destroys only pods. Nothing, ever,
deleted a claim.

The namespace quota is `persistentvolumeclaims: "40"`, so that is not a
cost leak: the fortieth execution is not the fortieth CONCURRENT one, it
is the fortieth EVER, and after it nothing in the environment can run a
story at all.
"""
import pytest

from kernel.conductor import reclaim
from kernel.driver import Driver


class _Driver:
    """Records what it was asked to do, in order."""

    def __init__(self, reclaimed=True, held=(1,)):
        self.calls = []
        self._reclaimed = reclaimed
        self._held = list(held)

    async def list_workspaces(self):
        self.calls.append(("list", None))
        return list(self._held)

    async def destroy(self, container_id, stop_timeout=0):
        self.calls.append(("destroy", container_id))

    async def release_workspace(self, execution_id):
        self.calls.append(("release", execution_id))
        return self._reclaimed


class _Conn:
    """Answers the two statements `releasable` makes, separately.

    ONE `self.sql` WOULD RECORD THE LAST CALL AND HIDE THE FIRST, which
    is how a test of the wrong statement passes.
    """

    def __init__(self, rows=(), present=None):
        self.rows = list(rows)
        self.present = present            # None: every held id exists
        self.executed = []
        self.sql = self.args = None

    async def fetch(self, sql, *args):
        if "FROM executions e" in sql:
            self.sql, self.args = sql, args
            return self.rows
        # the probe for rows that are simply gone
        held = args[0] if args else []
        ids = held if self.present is None else self.present
        return [{"id": i} for i in ids]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _executable(sql: str) -> str:
    """The statement with its `--` comments removed.

    A guard must read what RUNS. The first version of the assertion
    below matched the word `errand` inside a comment explaining why
    there is no errand clause -- so the test failed on its own
    documentation, which is the same mistake in miniature as reading a
    variable's name and not its value.
    """
    lines = [line.split("--")[0] for line in (sql or "").splitlines()]
    return chr(10).join(lines)


# ── the seam ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_driver_with_no_claims_reclaims_nothing():
    # Docker's workspace is a directory in a volume the next run reuses.
    # A no-op is the honest answer; a silent True would report disk
    # freed that never existed.
    assert await Driver.release_workspace(None, 1) is False


def test_releasing_is_not_folded_into_destroy():
    # If it were, a preempted Spot pod would take the workspace with it
    # -- the precise failure the claim's survival is designed to prevent.
    assert Driver.release_workspace is not Driver.destroy
    assert "preempt" in (Driver.release_workspace.__doc__ or "").lower()


# ── who is releasable ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_query_is_driven_by_the_story_not_the_execution():
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    # A story is attempted more than once and review sits after DONE, so
    # an execution reaching DONE is not the work being finished with.
    assert "stories s" in conn.sql
    assert "'done', 'reviewed'" in conn.sql


@pytest.mark.asyncio
async def test_a_run_still_verifying_is_not_releasable():
    # VERIFYING -> RUNNING on completion_rejected is a real edge: tearing
    # down there costs a rebuild on every rejection and blanks the panel
    # exactly when somebody is watching.
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    assert "VERIFYING" not in conn.sql


@pytest.mark.asyncio
async def test_failed_is_kept_for_a_window_and_the_window_is_named():
    conn = _Conn()
    await reclaim.releasable(conn, _Driver(), stale_hours=2)
    assert "FAILED" in conn.sql and "hours" in conn.sql
    assert conn.args[-1] == "2"
    assert reclaim.STALE_HOURS == 2


@pytest.mark.asyncio
async def test_an_execution_with_no_story_is_still_collected():
    # An errand cancelled before a story existed still holds a claim; an
    # inner join would leak exactly those.
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    assert "LEFT JOIN" in conn.sql


@pytest.mark.asyncio
async def test_nothing_is_offered_twice():
    # No `workspace_released_at` column: a CLAIM is listed only while it
    # exists, so the substrate's own state is what stops the second
    # attempt. Same property the column was avoided for, keyed on the
    # thing being reclaimed.
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    assert "e.id = ANY($1" in conn.sql


@pytest.mark.asyncio
async def test_a_finished_execution_whose_pod_is_already_gone_is_collected():
    """THE BUG, AND THE ONLY TEST HERE THAT FOUND A REAL LEAK.

    The first query required `container_id IS NOT NULL`, reasoning that
    a row stops offering itself once its pod is gone. True of the pod
    and false of the disk. `destroy()` nulls `container_id` and KEEPS
    the claim -- that is the preemption guarantee -- so every execution
    that had finished normally was excluded from the sweep that exists
    to collect it. Observed on `next`: five cancelled executions, every
    `container_id` NULL, four claims bound against a quota of forty,
    none of them releasable, ever.
    """
    conn = _Conn(rows=[{"id": 4, "container_id": None, "why": "cancelled"}])
    rows = await reclaim.releasable(conn, _Driver(held=[4]))
    assert [r["id"] for r in rows] == [4]
    assert "container_id IS NOT NULL" not in conn.sql


@pytest.mark.asyncio
async def test_the_substrate_is_asked_first_and_the_database_second():
    # Backwards, the database names claims that may not exist and the
    # sweep spends a delete on each. This way a claim absent from the
    # cluster is never considered at all.
    driver = _Driver(held=[])
    conn = _Conn(rows=[{"id": 1, "container_id": None, "why": "x"}])
    assert await reclaim.releasable(conn, driver) == []
    assert conn.sql is None                       # never asked
    assert driver.calls == [("list", None)]


@pytest.mark.asyncio
async def test_a_claim_whose_execution_row_is_gone_is_still_collected():
    # No row to join to, no state to reach a terminal value, no pod to
    # reap, no story to finish. It can only be found from the claim.
    conn = _Conn(rows=[], present=[])
    rows = await reclaim.releasable(conn, _Driver(held=[7]))
    assert [(r["id"], r["why"]) for r in rows] == [(7, "no execution row")]


@pytest.mark.asyncio
async def test_a_live_execution_is_not_collected_for_want_of_a_row():
    # The probe must not turn "running, joined, not yet releasable" into
    # "no execution row" -- that would delete the disk out from under a
    # running pod.
    conn = _Conn(rows=[], present=[7])
    assert await reclaim.releasable(conn, _Driver(held=[7])) == []


@pytest.mark.asyncio
async def test_an_errand_waits_for_the_story_exactly_like_a_story():
    """THE RULE I GOT WRONG, and it was dangerous rather than merely
    wrong.

    A version of this released on `kind = 'errand' AND state = 'DONE'`,
    reasoning that nothing reviews an errand so its execution reaching
    DONE is the end of it. The EXECUTION reaching DONE is the MACHINE
    finishing. The STORY reaching done or reviewed is a PERSON deciding
    they have what they need. An autonomous errand reaches the first
    without the second, and releasing there destroys the workspace
    while somebody is still reading the output it produced.

    Errand 187 is the shape: the execution sat PARKED_HOT waiting for a
    human, and only became DONE when one marked it.
    """
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    assert "kind = 'errand'" not in _executable(conn.sql), (
        "an errand is a story for this purpose; releasing on its own "
        "terminal state takes the tree away before anyone has looked")


@pytest.mark.asyncio
async def test_no_execution_state_alone_releases_a_finished_run():
    # DONE on the execution is the machine's word. The trigger is the
    # story's, for both kinds -- a person deciding they are finished
    # with it.
    conn = _Conn()
    await reclaim.releasable(conn, _Driver())
    assert "e.state = 'DONE'" not in _executable(conn.sql)


# ── what releasing does ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_pod_goes_before_the_disk():
    # A claim deleted while a pod still writes to it is a running
    # execution whose disk vanishes underneath it -- the API server
    # permits it and the kubelet finds out later, as I/O errors inside a
    # container nobody is watching.
    driver = _Driver()
    await reclaim.release(_Conn(), driver,
                          {"id": 9, "container_id": "exec-9", "why": "x"},
                          dry_run=False)
    assert driver.calls == [("destroy", "exec-9"), ("release", 9)]


@pytest.mark.asyncio
async def test_a_dry_run_touches_nothing():
    # The same switch the pod reaping already honours, so turning
    # deletes on is one decision and not two.
    driver = _Driver()
    out = await reclaim.release(_Conn(), driver,
                                {"id": 9, "container_id": "exec-9"},
                                dry_run=True)
    assert driver.calls == []
    assert out["action"] == "would_release"


@pytest.mark.asyncio
async def test_the_row_stops_offering_itself_once_released():
    conn = _Conn()
    await reclaim.release(conn, _Driver(),
                          {"id": 9, "container_id": "exec-9"}, dry_run=False)
    assert any("container_id = NULL" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_a_claim_that_was_already_gone_is_reported_honestly():
    out = await reclaim.release(_Conn(), _Driver(reclaimed=False),
                                {"id": 9, "container_id": None},
                                dry_run=False)
    assert out["action"] == "released"
    assert out["reclaimed"] is False
