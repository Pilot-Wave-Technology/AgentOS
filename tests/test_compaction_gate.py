"""Regression tests for the compaction dispatch gate.

Guards the 2026-08-08 incident (goal MARL, 53h silent): a compaction-end event
was emitted by the gateway but never delivered, and the control panel's hold had
no expiry, so the thread was gated indefinitely.

Stdlib `unittest` on purpose — this repo has no pytest dependency and a bug fix
is the wrong place to introduce one. Run:

    .venv/Scripts/python.exe -m unittest discover -s tests -v

Time is advanced by rewriting `started_at` directly rather than sleeping, so the
suite exercises the 5-minute/15-minute production windows in milliseconds.
"""
from __future__ import annotations

import unittest

from app.chat_module.compaction_gate import CompactionGate

THREAD = "9bcd096e-f954-4fab-958b-6974a0925c54"  # the thread from the incident

SOFT_S = 300
MAX_S = 900


def _gate() -> CompactionGate:
    return CompactionGate(soft_hold_s=SOFT_S, max_hold_s=MAX_S)


def _age(gate: CompactionGate, thread_id: str, seconds: float) -> None:
    """Backdate a hold so it appears `seconds` old."""
    gate._holds[thread_id].started_at -= seconds


async def _count(value):
    """Build a fetcher returning a fixed compactionCount (or raising)."""
    return value


class CompactionGateTest(unittest.IsolatedAsyncioTestCase):

    # ── the happy path still holds ──────────────────────────────────────────

    async def test_no_hold_means_no_defer(self):
        self.assertFalse(await _gate().should_defer(THREAD))

    async def test_fresh_hold_defers(self):
        gate = _gate()
        gate.mark_start(THREAD)
        self.assertTrue(await gate.should_defer(THREAD))

    async def test_end_event_releases(self):
        gate = _gate()
        gate.mark_start(THREAD)
        self.assertTrue(gate.mark_end(THREAD))
        self.assertFalse(await gate.should_defer(THREAD))

    async def test_end_event_for_unheld_thread_is_a_noop(self):
        self.assertFalse(_gate().mark_end(THREAD))

    # ── the incident: end-event never arrives ───────────────────────────────

    async def test_status_check_releases_when_count_advanced(self):
        """The exact 2026-08-08 shape: the splice committed (compactionCount
        advanced) but the end-event never reached us."""
        gate = _gate()
        gate.mark_start(THREAD)
        gate.set_baseline(THREAD, 41)
        _age(gate, THREAD, SOFT_S + 1)

        async def fetch(_tid):
            return 42  # the gateway logged `succeeded#42`

        self.assertFalse(await gate.should_defer(THREAD, fetch_count=fetch))
        self.assertFalse(gate.is_held(THREAD), "hold must be dropped, not just skipped")

    async def test_holds_past_soft_window_when_count_has_not_advanced(self):
        """A genuinely slow compaction must NOT be cut short at the soft window."""
        gate = _gate()
        gate.mark_start(THREAD)
        gate.set_baseline(THREAD, 41)
        _age(gate, THREAD, SOFT_S + 1)

        async def fetch(_tid):
            return 41  # unchanged — round still running

        self.assertTrue(await gate.should_defer(THREAD, fetch_count=fetch))
        self.assertTrue(gate.is_held(THREAD))

    async def test_hard_cap_releases_even_without_confirmation(self):
        gate = _gate()
        gate.mark_start(THREAD)
        gate.set_baseline(THREAD, 41)
        _age(gate, THREAD, MAX_S + 1)

        async def fetch(_tid):
            return 41  # still no advance, but we are past the hard cap

        self.assertFalse(await gate.should_defer(THREAD, fetch_count=fetch))
        self.assertFalse(gate.is_held(THREAD))

    async def test_hard_cap_releases_with_no_fetcher_at_all(self):
        """Timer-only fallback: the status RPC path is entirely unavailable."""
        gate = _gate()
        gate.mark_start(THREAD)
        _age(gate, THREAD, MAX_S + 1)
        self.assertFalse(await gate.should_defer(THREAD))

    # ── degradation must never turn into a stuck gate ───────────────────────

    async def test_fetcher_failure_degrades_to_timer_not_a_stall(self):
        gate = _gate()
        gate.mark_start(THREAD)
        gate.set_baseline(THREAD, 41)

        async def boom(_tid):
            raise RuntimeError("gateway unreachable")

        _age(gate, THREAD, SOFT_S + 1)
        self.assertTrue(await gate.should_defer(THREAD, fetch_count=boom),
                        "before the hard cap a fetch failure should still hold")

        _age(gate, THREAD, MAX_S)
        self.assertFalse(await gate.should_defer(THREAD, fetch_count=boom),
                         "past the hard cap it must release despite the failure")

    async def test_missing_baseline_falls_back_to_the_timer(self):
        """set_baseline never landed (status RPC was down at start). The count
        alone can't prove completion, so only the hard cap may release."""
        gate = _gate()
        gate.mark_start(THREAD)
        _age(gate, THREAD, SOFT_S + 1)

        async def fetch(_tid):
            return 999

        self.assertTrue(await gate.should_defer(THREAD, fetch_count=fetch))
        _age(gate, THREAD, MAX_S)
        self.assertFalse(await gate.should_defer(THREAD, fetch_count=fetch))

    async def test_set_baseline_ignores_none_and_unknown_threads(self):
        gate = _gate()
        gate.mark_start(THREAD)
        gate.set_baseline(THREAD, None)
        gate.set_baseline("some-other-thread", 5)  # must not raise
        self.assertIsNone(gate._holds[THREAD].baseline_count)

    # ── disconnect handling — the path that failed in the incident ──────────

    async def test_clear_all_releases_every_hold(self):
        gate = _gate()
        gate.mark_start(THREAD)
        gate.mark_start("thread-b")

        freed = gate.clear_all()

        self.assertCountEqual(freed, [THREAD, "thread-b"])
        self.assertFalse(await gate.should_defer(THREAD))
        self.assertFalse(await gate.should_defer("thread-b"))

    async def test_clear_all_with_no_holds_is_empty(self):
        self.assertEqual(_gate().clear_all(), [])

    # ── isolation ───────────────────────────────────────────────────────────

    async def test_holds_are_per_thread(self):
        gate = _gate()
        gate.mark_start(THREAD)
        self.assertTrue(await gate.should_defer(THREAD))
        self.assertFalse(await gate.should_defer("unrelated-thread"))


class DispatcherCompactionWiringTest(unittest.IsolatedAsyncioTestCase):
    """The dispatcher's gate call must FAIL OPEN.

    Every one of these paths used to be a `return False` that left rows queued
    forever. A conversational gate that fails closed converts any local fault
    into indefinite silence — the exact user-visible symptom of the incident.
    """

    @staticmethod
    def _defer(session):
        from app.chat_module.dispatcher import Dispatcher
        return Dispatcher._defer_for_compaction(session, THREAD)

    async def test_no_session_dispatches(self):
        self.assertFalse(await self._defer(None))

    async def test_session_without_the_bounded_check_dispatches(self):
        class Legacy:
            """An older session object exposing only the unbounded latch."""
            def is_compacting(self, _tid):
                return True

        self.assertFalse(await self._defer(Legacy()))

    async def test_raising_check_dispatches(self):
        class Broken:
            async def should_defer_for_compaction(self, _tid):
                raise RuntimeError("gate exploded")

        self.assertFalse(await self._defer(Broken()))

    async def test_gate_decision_is_honoured_when_it_works(self):
        class Holding:
            async def should_defer_for_compaction(self, _tid):
                return True

        self.assertTrue(await self._defer(Holding()))


class ConnectTeardownTest(unittest.IsolatedAsyncioTestCase):
    """`_connect` must release compaction holds on EVERY exit path.

    Regression guard for a bug found during local verification on 2026-08-10:
    the cleanup was written after the `async with websockets.connect(...)`
    block, but a real gateway death raises ConnectionClosedError out of the
    receive loop, so that code never ran — the disconnect was caught upstream
    in `_connect_loop`. Symptom was identical to the original incident: hold
    set, connection gone, nothing to ever release it.

    Exercised against the real `_connect` with a stand-in `self`, so the
    try/finally structure itself is under test rather than a copy of it.
    """

    def _fake_session(self, inner):
        from types import SimpleNamespace
        calls = {"released": 0, "stale_notified": 0}

        async def release():
            calls["released"] += 1

        def notify_stale():
            calls["stale_notified"] += 1

        session = SimpleNamespace(
            _gw_ws="a-live-socket",
            _gw_ready=SimpleNamespace(clear=lambda: None),
            _connect_inner=inner,
            _release_compaction_holds_on_disconnect=release,
            _notify_stale_runs_on_disconnect=notify_stale,
        )
        return session, calls

    async def _run_connect(self, session):
        from app.openclaw import _AgentSession
        await _AgentSession._connect(session)

    async def test_teardown_runs_on_clean_close(self):
        async def inner(_url):
            return None

        session, calls = self._fake_session(inner)
        await self._run_connect(session)

        self.assertEqual(calls["released"], 1)
        self.assertEqual(calls["stale_notified"], 1)
        self.assertIsNone(session._gw_ws)

    async def test_teardown_runs_when_the_socket_dies(self):
        """The path that actually matters — and the one that was broken."""
        from websockets.exceptions import ConnectionClosedError

        async def inner(_url):
            raise ConnectionClosedError(None, None)

        session, calls = self._fake_session(inner)

        with self.assertRaises(ConnectionClosedError):
            await self._run_connect(session)

        self.assertEqual(calls["released"], 1,
                         "compaction holds MUST be released when the socket dies")
        self.assertEqual(calls["stale_notified"], 1)
        self.assertIsNone(session._gw_ws)

    async def test_exception_still_propagates_to_the_reconnect_loop(self):
        """Teardown must not swallow the error — _connect_loop needs it to back off."""
        async def inner(_url):
            raise RuntimeError("handshake blew up")

        session, _ = self._fake_session(inner)
        with self.assertRaises(RuntimeError):
            await self._run_connect(session)


if __name__ == "__main__":
    unittest.main()
