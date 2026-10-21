"""The kernel speaks unqualified names. The app's pool does not.

THE 500 THAT FOUND IT. Cancelling an in-progress errand from the board
returned 500 with `InFailedSQLTransactionError`. The cause was four
frames earlier: `stop_the_run` handed the control panel's pooled
connection to `submit_proposal`, which writes `INSERT INTO proposals`
-- unqualified, because every kernel function speaks unqualified names
against the `kernel` schema. The app's connections have no `kernel` on
their search_path, so the insert raised `UndefinedTableError`.

IT HAD BEEN FAILING SILENTLY FOR AS LONG AS IT EXISTED. The `except`
in `stop_the_run` caught it and the cancellation carried on, so no run
was ever actually asked to stop -- invisible, because until errands
were exempted from the started-story guard, the only cancellable
stories had no live run and the insert never ran.

`_kernel_conn` is the existing answer: `kernel_run_control` and
`cancel_goal_runs` both use it, which is why the thread's own Cancel
button worked while the board's did not.
"""
from __future__ import annotations

import asyncio
import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_stop_the_run_opens_a_kernel_connection():
    fn = _src("app/services/story_cancellation.py")
    fn = fn[fn.index("async def stop_the_run"):]
    fn = fn[:fn.index("async def", 10)]
    assert "_kernel_conn" in fn, (
        "a kernel write on an app connection raises UndefinedTableError "
        "and aborts the caller's transaction")


def test_it_does_not_hand_the_callers_connection_to_the_mailbox():
    """The precise mistake. `conn` is the caller's, inside the caller's
    transaction; the mailbox write must not go through it."""
    fn = _src("app/services/story_cancellation.py")
    fn = fn[fn.index("async def stop_the_run"):]
    fn = fn[:fn.index("async def", 10)]
    call = fn[fn.index("submit_proposal("):]
    first_arg = call[call.index("(") + 1:].split(",")[0].strip()
    assert first_arg == "kconn", (
        f"submit_proposal is called with {first_arg!r}, not the kernel "
        f"connection")


def test_the_kernel_connection_is_closed():
    """One leaked connection per cancellation would exhaust the server
    long before anybody connected it to cancelling stories."""
    fn = _src("app/services/story_cancellation.py")
    fn = fn[fn.index("async def stop_the_run"):]
    fn = fn[:fn.index("async def", 10)]
    assert "finally:" in fn and "kconn.close()" in fn


def test_every_mailbox_call_from_the_app_uses_a_kernel_connection():
    """The general rule, so the next one is caught here. `cancel_goal_runs`
    already did this correctly -- `stop_the_run` was the outlier."""
    import re
    for rel in ("app/services/story_cancellation.py",
                "app/services/kernel_goal_teardown.py",
                "app/services/kernel_run_control.py"):
        s = _src(rel)
        if "submit_proposal(" not in s:
            continue
        assert "_kernel_conn" in s, f"{rel} writes to the mailbox on an app connection"
        for m in re.finditer(r"submit_proposal\(\s*(\w+)", s):
            assert m.group(1) in ("kconn", "conn"), (rel, m.group(1))


# ── and the teardown must survive an already-broken transaction ──────

def test_the_session_teardown_does_not_raise_on_a_dead_transaction():
    """It promised "NEVER RAISES" and only wrapped the gateway call, so
    an aborted transaction turned somebody else's silent bug into a
    visibly broken button."""
    from app.services.story_session_teardown import close_story_sessions

    class _Broken:
        async def fetch(self, *a):
            raise RuntimeError("current transaction is aborted")
        async def execute(self, *a):
            raise RuntimeError("current transaction is aborted")

    assert asyncio.run(close_story_sessions(_Broken(), "G1", "s1")) == 0


def test_a_failing_thread_update_does_not_stop_the_others():
    """One bad row must not leave the rest of a story's threads open."""
    from app.services.story_session_teardown import close_story_sessions

    class _Half:
        def __init__(self):
            self.tries = 0
        async def fetch(self, *a):
            return [{"id": "bad", "instance_id": None, "agent_id": ""},
                    {"id": "good", "instance_id": None, "agent_id": ""}]
        async def execute(self, sql, *a):
            self.tries += 1
            if a[0] == "bad":
                raise RuntimeError("nope")

    conn = _Half()
    assert asyncio.run(close_story_sessions(conn, "G1", "s1")) == 1
    assert conn.tries == 2, "it gave up after the first failure"
