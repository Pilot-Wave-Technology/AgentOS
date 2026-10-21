"""Freeze System must stop the thing that writes to git.

`execution_pauses` is consulted by every dispatch path in app/ and the
string appeared NOWHERE under kernel/. So pressing Freeze turned the
banner red, stopped control-panel dispatch and stopped conversational
replies -- while every admitted kernel execution kept taking legs,
spending tokens, running model-authored bash and committing. The one
lever an operator reaches for first was the one that did not reach the
thing doing the damage.
"""

from __future__ import annotations

import asyncio
import io
import pathlib

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION, shipped_privileges
from kernel.conductor import pauses


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _src(rel: str) -> str:
    root = pathlib.Path(__file__).resolve().parents[3]
    return io.open(root / rel, encoding="utf-8").read()


class _Conn:
    def __init__(self, row=None, raises=False):
        self.row, self.raises, self.seen = row, raises, []

    async def fetchrow(self, sql, *args):
        if self.raises:
            raise RuntimeError("connection gone")
        self.seen.append(args)
        return self.row


def test_no_row_means_not_frozen():
    assert _run(pauses.frozen(_Conn(None), "KER28")) is None


def test_a_system_pause_freezes_everything():
    row = {"scope": "system", "scope_id": "", "reason": "incident",
           "paused_by": "abhishek"}
    got = _run(pauses.frozen(_Conn(row), "KER28"))
    assert got["scope"] == "system"
    assert "incident" in pauses.describe(got)
    assert "abhishek" in pauses.describe(got)


def test_a_goal_pause_is_matched_on_that_goal():
    conn = _Conn(None)
    _run(pauses.frozen(conn, "KER28"))
    assert conn.seen == [("KER28",)], (
        "the goal is not passed to the query, so a per-goal pause could "
        "never match")


def test_a_worker_serving_every_goal_passes_an_empty_scope():
    """goal_id=None is how a standing worker runs. Only a SYSTEM freeze
    should block it -- a pause on one goal must not stop the others."""
    conn = _Conn(None)
    _run(pauses.frozen(conn, None))
    assert conn.seen == [("",)]


def test_there_is_no_story_scope_to_read():
    """A draft added one, for a GO tool that wrote `execution_pauses`
    while the Pause BUTTON submitted a `hold` proposal. Two mechanisms
    for one word. `kernel/go/run_control.py` uses the proposal now, and
    nothing writes a story-scoped row -- so the kill switch must not go
    looking for one."""
    assert "'story'" not in pauses.SQL
    import inspect
    assert list(inspect.signature(pauses.frozen).parameters) ==         ["conn", "goal_id"]


def test_an_unreadable_switch_is_treated_as_frozen():
    """FAIL CLOSED, matching the direction execution_control.py chose for
    dispatch_block. A tick is four seconds; the alternative is an agent
    writing to a repository while an operator believes it is stopped."""
    got = _run(pauses.frozen(_Conn(raises=True), "KER28"))
    assert got is not None, "an unreadable kill switch was treated as absent"
    assert "could not be read" in got["reason"]


def test_the_kernel_can_only_read_it():
    """A run that could clear its own pause is not a kill switch, it is a
    suggestion."""
    src = _src("kernel/conductor/pauses.py")
    for write in ("INSERT", "UPDATE", "DELETE", "insert", "delete "):
        assert write not in pauses.SQL, f"{write} in the kernel's pause SQL"
    if IS_DISTRIBUTION:
        privs = shipped_privileges("public.execution_pauses", "agentos_worker")
        assert privs == {"SELECT"}, (
            f"the kernel holds {sorted(privs)} on execution_pauses")
        return
    grant = _src("migrations/execution_pauses_kernel_read.sql")
    assert "GRANT SELECT ON public.execution_pauses TO agentos_worker" in grant
    assert "INSERT" not in grant.split("GRANT")[1]


def test_the_tick_gates_the_three_take_new_work_paths():
    """Finish what you are holding, take nothing new."""
    src = _src("kernel/worker/loop.py")
    assert "pause = await frozen(conn, goal_id)" in src, (
        "the tick does not consult the kill switch")

    i = src.index("pause = await frozen(conn, goal_id)")
    after = src[i:]
    for taken in ("_schedule_sweep(conn)", "dispatch_pass",
                  "rows = await claimable"):
        assert taken in after, (
            f"{taken} runs BEFORE the freeze check, so a frozen system "
            f"still takes new work")


def test_deliveries_still_run_while_frozen():
    """Being unable to cancel during a freeze would be perverse."""
    src = _src("kernel/worker/loop.py")
    i = src.index("pause = await frozen(conn, goal_id)")
    before = src[:i]
    for delivery in ("deliver_answers", "deliver_cancellations"):
        assert delivery in before, (
            f"{delivery} is gated by the freeze; an operator could not "
            f"cancel a run while the system is paused")
