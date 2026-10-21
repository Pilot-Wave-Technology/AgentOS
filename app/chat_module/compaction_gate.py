"""Compaction dispatch gate — a BOUNDED hold with evidence-based release.

While the gateway compacts a thread's pi session it emits `stream: "compaction"`
agent_events (`phase: "start"` / `phase: "end"`). The chat dispatcher defers new
turns for that thread in between, so a turn never runs against a context that is
about to be spliced out from under it.

WHY THIS MODULE EXISTS (incident 2026-08-08, goal MARL):
    Those events are fire-and-forget WebSocket broadcasts — no ack, no replay.
    The `end` event arrives up to minutes after `start` (88s in the incident), so
    any reconnect in that window destroys the ONLY signal that could release the
    hold. The original implementation was a bare `set[str]` with no expiry, so a
    single lost frame silenced a production agent for 53 hours. The gateway had
    long since finished the compaction successfully; only the control panel's
    belief was stale.

    Worse, the hold lived in per-process memory while several Cloud Run instances
    each ran a dispatcher. One wedged instance and one healthy instance raced for
    the same per-thread advisory lock, so whether a user got a reply depended on
    which pod answered — visible in prod as `run_batch done` and
    `deferring — agent mid-compaction` 37ms apart on the same thread.

THE CONTRACT THIS MODULE ENFORCES:
    **A hold released only by an unacknowledged event is a permanent hold.**
    So the `end` event is treated as the FAST path, never the only path:

      1. Fresh hold (< soft_hold_s)  → defer, as before. The common case.
      2. Past soft_hold_s            → ask the gateway whether the round actually
                                       finished, via `sessions.status`. A
                                       `compactionCount` above the baseline we
                                       recorded at `start` PROVES the splice
                                       committed, so the hold is stale → release.
      3. Past max_hold_s             → release regardless, with a WARNING. Blind,
                                       but bounded: an unbounded hold is strictly
                                       worse than a turn that runs against a
                                       pre-splice context.

    Releasing early is safe by design on the gateway side: entries appended
    during a compaction window survive the splice, and the splice re-validates
    its cut point and fails cleanly if the session moved. The gate is a
    cost/quality guard, not a data-safety one.

This module owns the decision; `_AgentSession` owns only the wiring (it supplies
the event signals and a way to read `compactionCount`). The gate never imports
the gateway client — it takes a fetcher callable — so it stays unit-testable
without a live WebSocket.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .config import get_chat_config

_log = logging.getLogger(__name__)

# Async callable returning the gateway's current `compactionCount` for a thread,
# or None when it can't be determined (RPC down, field absent on older builds).
CompactionCountFetcher = Callable[[str], Awaitable[Optional[int]]]


@dataclass
class _Hold:
    """One in-flight compaction hold for a single thread."""
    started_at: float
    # `compactionCount` as of the moment the hold was taken. The gateway bumps
    # this only after a splice commits, so `current > baseline` is positive
    # proof that the round we are waiting on has finished. None when the
    # baseline lookup failed — we then fall back to the timer alone.
    baseline_count: Optional[int] = None
    # Defer decisions made under this hold. Only used to rate-limit the
    # WARNING so a long hold doesn't write a log line every poll tick.
    defers: int = field(default=0)
    warned_at_defer: int = field(default=0)


class CompactionGate:
    """Per-session registry of compaction holds, keyed by thread_id.

    Not thread-safe by design: it is only ever touched from the single asyncio
    event loop that owns its `_AgentSession`.
    """

    def __init__(
        self,
        soft_hold_s: Optional[int] = None,
        max_hold_s: Optional[int] = None,
    ) -> None:
        # Windows are injectable so tests can use seconds instead of minutes;
        # production always takes the env-driven config defaults.
        if soft_hold_s is None or max_hold_s is None:
            cfg = get_chat_config()
            soft_hold_s = cfg.compaction_soft_hold_s if soft_hold_s is None else soft_hold_s
            max_hold_s = cfg.compaction_max_hold_s if max_hold_s is None else max_hold_s
        self._soft_hold_s = soft_hold_s
        self._max_hold_s = max_hold_s
        self._holds: dict[str, _Hold] = {}

    # ── signals from the gateway's compaction events ────────────────────────

    def mark_start(self, thread_id: str) -> None:
        """`phase: "start"` arrived — begin holding dispatch for this thread."""
        if not thread_id:
            return
        self._holds[thread_id] = _Hold(started_at=time.monotonic())
        _log.info(
            "compaction_gate: hold STARTED thread=%s (soft=%ds hard=%ds)",
            thread_id, self._soft_hold_s, self._max_hold_s,
        )

    def mark_end(self, thread_id: str) -> bool:
        """`phase: "end"` arrived — the fast path. True if a hold was released."""
        hold = self._holds.pop(thread_id, None)
        if hold is None:
            return False
        _log.info(
            "compaction_gate: hold RELEASED by end-event thread=%s after %.1fs (%d defer(s))",
            thread_id, time.monotonic() - hold.started_at, hold.defers,
        )
        return True

    def set_baseline(self, thread_id: str, count: Optional[int]) -> None:
        """Record `compactionCount` as of hold start. Best-effort: a None here
        just means the status path is unavailable and the timer is the only
        release mechanism for this round."""
        hold = self._holds.get(thread_id)
        if hold is None or count is None:
            return
        hold.baseline_count = int(count)
        _log.debug(
            "compaction_gate: baseline compactionCount=%d thread=%s",
            hold.baseline_count, thread_id,
        )

    def clear_all(self) -> list[str]:
        """Drop every hold and return the thread_ids that were held.

        Called on gateway WS teardown: the connection that would have delivered
        the `end` events is gone, so continuing to hold guarantees a stall.
        The caller re-notifies the dispatcher for each returned thread.
        """
        held = list(self._holds)
        if held:
            _log.warning(
                "compaction_gate: clearing %d hold(s) on gateway disconnect — "
                "the end-event can no longer arrive on this connection: %s",
                len(held), held,
            )
        self._holds.clear()
        return held

    # ── the dispatch decision ───────────────────────────────────────────────

    def is_held(self, thread_id: str) -> bool:
        """Raw latch presence, ignoring expiry. Diagnostics only — dispatch
        decisions must use `should_defer`, which is what bounds the hold."""
        return thread_id in self._holds

    async def should_defer(
        self, thread_id: str, fetch_count: Optional[CompactionCountFetcher] = None,
    ) -> bool:
        """True if dispatch for this thread should wait on an in-flight compaction.

        Escalates through the three tiers described in the module docstring.
        Never raises: a failure anywhere in the status path degrades to the
        timer, because a gate that errors must not become a gate that sticks.
        """
        hold = self._holds.get(thread_id)
        if hold is None:
            return False

        age = time.monotonic() - hold.started_at
        hold.defers += 1

        # Tier 1 — fresh hold. Overwhelmingly the common path.
        if age < self._soft_hold_s:
            return True

        # Tier 2 — overdue. Ask the gateway whether the round actually finished.
        if fetch_count is not None:
            try:
                current = await fetch_count(thread_id)
            except Exception:
                current = None
                _log.debug(
                    "compaction_gate: compactionCount lookup failed thread=%s — "
                    "falling back to the timer", thread_id, exc_info=True,
                )
            if current is not None and hold.baseline_count is not None:
                if current > hold.baseline_count:
                    _log.warning(
                        "compaction_gate: hold RELEASED by status check thread=%s "
                        "after %.0fs — compactionCount advanced %d→%d, so the splice "
                        "committed and the end-event was lost in transit "
                        "(%d defer(s) wasted)",
                        thread_id, age, hold.baseline_count, current, hold.defers,
                    )
                    self._holds.pop(thread_id, None)
                    return False

        # Tier 3 — hard cap. Blind release, but bounded and loud.
        if age >= self._max_hold_s:
            _log.warning(
                "compaction_gate: hold FORCE-RELEASED thread=%s after %.0fs "
                "(hard cap %ds, %d defer(s)) — no end-event and no status "
                "confirmation. Dispatching anyway; a turn against a pre-splice "
                "context is preferable to an unbounded stall.",
                thread_id, age, self._max_hold_s, hold.defers,
            )
            self._holds.pop(thread_id, None)
            return False

        # Still holding, past the soft window. Warn on a back-off schedule so a
        # long hold is visible without writing a line every poll tick — silence
        # at INFO is what hid the 2026-08-08 incident for two days.
        if hold.defers >= hold.warned_at_defer * 2 or hold.warned_at_defer == 0:
            hold.warned_at_defer = max(1, hold.defers)
            _log.warning(
                "compaction_gate: thread=%s still held after %.0fs and %d defer(s) "
                "— no end-event yet; releasing at %ds if unconfirmed",
                thread_id, age, hold.defers, self._max_hold_s,
            )
        return True
