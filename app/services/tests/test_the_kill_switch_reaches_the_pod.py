"""D4 · I8: a paused goal stops a RUNNING story at its next leg.

THE INVARIANT IS OLDER THAN THE POD AND THAT IS THE POINT. I8 reads "the
kill switch stops work", and its evidence today is `frozen()` gating
dispatch and leg claiming inside the conductor's tick. The mechanism is
a REFUSAL TO CLAIM: the conductor declines to take the next leg, so the
run stops at a boundary where the workspace is committed and the lease
released.

D2 moves the leg into the execution pod, and a pod that drives its own
legs claims nothing from the conductor. Left there, I8's evidence
evaporates while its wording stays true -- Freeze still stops NEW
stories, so the banner is red, the queue stops growing, and the runaway
story an operator pressed the button FOR keeps spending, keeps running
model-authored bash and keeps committing. That is worse than a button
that does nothing, because this one reports success.

`kernel/harness/loop_gate.py` is the pod's half, and these are its
tests. They are fake-backed rather than database-backed, and that is a
real limit worth naming here rather than in a report nobody re-reads:
the fakes prove the DECISIONS -- which reason wins, what the pod does
with each -- and they cannot prove the SQL matches a live schema. LT1
(plan 7b) is the item that would, and it is not this one.

WHAT IS DELIBERATELY NOT TESTED HERE: mid-leg cancellation. A leg is a
model turn and its tool calls, and `pauses.py` argues at length that
interrupting one leaves a sandbox in a state nobody described. The
existing machinery has no mid-leg stop ON PURPOSE, and a test asserting
one would be a test asking for the bug.
"""

from __future__ import annotations

import ast
import asyncio
import io
import pathlib

from kernel.harness.loop_gate import CANCEL_PENDING_SQL, refuse_reason

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Conn:
    """The three reads the gate makes, answered from a script.

    `pause_raises` is not decoration: `frozen()` fails CLOSED, and the
    only way to see that from out here is to break the read it makes.
    """

    def __init__(self, *, state="RUNNING", goal_id="KER28", exists=True,
                 cancel=None, pause=None, pause_raises=False):
        self.state, self.goal_id, self.exists = state, goal_id, exists
        self.cancel, self.pause, self.pause_raises = cancel, pause, pause_raises
        self.seen: list[str] = []

    async def fetchrow(self, sql, *args):
        if "FROM executions" in sql:
            self.seen.append("executions")
            if not self.exists:
                return None
            return {"state": self.state, "goal_id": self.goal_id}
        self.seen.append("pauses")
        if self.pause_raises:
            raise RuntimeError("permission denied for table execution_pauses")
        return self.pause

    async def fetchval(self, sql, *args):
        assert sql is CANCEL_PENDING_SQL
        self.seen.append("cancel")
        return self.cancel

    async def execute(self, sql, *args):
        # THE GATE WRITES NOTHING, and this is where that is enforced
        # rather than asserted about. A run that must not take a leg has
        # not failed, has spent no generation and has journalled nothing;
        # the process that DECIDED to freeze or cancel owns saying so in
        # the row. A pod writing it too is a second, less careful copy of
        # `deliver_cancellations`, which checks the run's own machine
        # version for a legal edge before it transitions anything.
        raise AssertionError(f"the gate wrote to the database: {sql!r}")


SYSTEM_PAUSE = {"scope": "system", "scope_id": "", "reason": "incident",
                "paused_by": "abhishek"}


# -- I8, the pod's half ---------------------------------------------------

def test_a_running_story_takes_no_leg_while_its_goal_is_frozen():
    """THE INVARIANT. Not 'no new stories' -- this execution is RUNNING,
    it has a pod, and it is refused anyway."""
    conn = _Conn(pause=SYSTEM_PAUSE)
    assert _run(refuse_reason(conn, 42)) == "frozen"


def test_an_unfrozen_running_story_is_allowed_to_proceed():
    """A gate that never opens is a fleet that never runs; half of I8 is
    that the switch is OFF by default."""
    assert _run(refuse_reason(_Conn(), 42)) is None


def test_a_goal_pause_is_matched_on_the_executions_own_goal():
    """The pod reads `goal_id` from the row rather than being told it, so
    a per-goal freeze cannot be aimed at the wrong story by a caller."""
    conn = _Conn(goal_id="KER28", pause={"scope": "goal",
                                         "scope_id": "KER28",
                                         "reason": "runaway",
                                         "paused_by": "abhishek"})
    assert _run(refuse_reason(conn, 42)) == "frozen"
    assert conn.seen == ["executions", "cancel", "pauses"]


def test_an_unreadable_switch_refuses_the_leg():
    """FAIL CLOSED, inherited from `frozen()` rather than re-decided:
    'an unreadable kill switch is not an absent one'. The cost is named
    where it bites -- a grant mistake looks like a fleet that stopped."""
    assert _run(refuse_reason(_Conn(pause_raises=True), 42)) == "frozen"


# -- cancellation, which is a different fact from a freeze ----------------

def test_a_pending_cancellation_stops_the_next_leg():
    assert _run(refuse_reason(_Conn(cancel="the user asked"), 42)) == "cancelled"


def test_cancellation_outranks_a_freeze():
    """Collapsing the two is how a cancelled run gets reported as frozen
    and then waits for a freeze to be lifted that was never on."""
    conn = _Conn(cancel="the user asked", pause=SYSTEM_PAUSE)
    assert _run(refuse_reason(conn, 42)) == "cancelled"


def test_the_pod_declines_the_leg_and_does_not_apply_the_cancellation():
    """`deliver_cancellations` checks the run's OWN machine version for a
    legal edge to CANCELLED and declines rather than forcing one. A pod
    writing the transition itself would be a second, less careful copy of
    that decision -- so the gate reads and writes nothing.

    Enforced by `_Conn.execute` raising, which makes EVERY test in this
    file a write test rather than only this one."""
    conn = _Conn(cancel="the user asked")
    assert _run(refuse_reason(conn, 42)) == "cancelled"


# -- the run moved while the pod was starting -----------------------------

def test_a_run_that_has_left_running_takes_no_leg():
    """A pod's start is not instantaneous, and VERIFYING, PARKED_* and
    CANCELLED are all states a run can reach between dispatch and the
    runner's first query."""
    for state in ("VERIFYING", "PARKED_HOT", "CANCELLED", "DONE", "FAILED"):
        got = _run(refuse_reason(_Conn(state=state), 42))
        assert got == f"not_runnable:{state}", state


def test_the_state_travels_in_the_reason():
    """'The run moved' and 'somebody cancelled it' are different stories,
    and the refusal line is the only place the difference survives."""
    assert "VERIFYING" in _run(refuse_reason(_Conn(state="VERIFYING"), 42))


def test_a_missing_row_is_reported_rather_than_raised():
    """A pod outliving the row it was dispatched for is a reaping
    question. Raising turns it into a traceback the reader has to parse
    to learn the same thing."""
    assert _run(refuse_reason(_Conn(exists=False), 42)) == "no_such_execution"


# -- the order the gate asks in -------------------------------------------

def test_the_cheap_and_terminal_questions_come_first():
    """The freeze is the only one of the three a later tick may find
    lifted, so it is asked last -- and a run that is already finished
    never costs a read of the pause table at all."""
    conn = _Conn(state="DONE")
    _run(refuse_reason(conn, 42))
    assert conn.seen == ["executions"]


def test_every_reason_is_a_token_a_reader_can_match():
    """These land in `loop_result.refusal(stop_reason=...)` and travel
    the wire. A sentence there would be a sentence something greps for."""
    for conn, expect in ((_Conn(pause=SYSTEM_PAUSE), "frozen"),
                         (_Conn(cancel="x"), "cancelled"),
                         (_Conn(exists=False), "no_such_execution")):
        got = _run(refuse_reason(conn, 42))
        assert got == expect and " " not in got


# -- and the conductor's half is still there ------------------------------

def _calls_in_the_lock() -> list[str]:
    """Every call `leg_runner.run` makes inside its `leg_lock` block."""
    tree = ast.parse(_src("kernel/harness/leg_runner.py"))
    run = next(n for n in tree.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")

    def names(node):
        return [getattr(c.func, "id", getattr(c.func, "attr", ""))
                for c in ast.walk(node) if isinstance(c, ast.Call)]

    for node in ast.walk(run):
        if isinstance(node, ast.AsyncWith) and "leg_lock" in names(
                ast.Module(body=[ast.Expr(value=i.context_expr)
                                 for i in node.items], type_ignores=[])):
            return names(node)
    raise AssertionError("leg_runner.run no longer takes leg_lock")


def test_the_gate_is_asked_inside_the_lock_and_before_the_first_call():
    """Two orderings, and each is a different failure if reversed.

    BEFORE `run_leg`, or the switch stops nothing this leg was going to
    spend. INSIDE `leg_lock`, or the refusal is about an execution this
    process has no claim on -- another worker could be advancing it, and
    a freeze read a millisecond earlier can be contradicted a
    millisecond later."""
    calls = _calls_in_the_lock()
    assert "refuse_reason" in calls, "the gate is outside the lock"
    assert calls.index("refuse_reason") < calls.index("run_leg")


def test_the_conductors_gate_was_not_moved_out_from_under_it():
    """D4 ADDS a gate; it does not relocate one. The conductor still runs
    every leg in every environment that exists today (`off` is permanent
    -- see `k8s_sqlproxy.OFF`), so removing its `frozen()` call in the
    name of 'the pod does it now' would turn the switch off everywhere
    for the sake of a deployment that does not exist yet."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/worker/loop.py", encoding="utf-8").read()
    assert "pause = await frozen(conn, goal_id)" in src
