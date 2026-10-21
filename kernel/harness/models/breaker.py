"""Stop asking a provider that is down, and stop admitting work to it.

WHY A CEILING IS NOT A CIRCUIT BREAKER. `MAX_LEGS` is 12 and the worker
tick is 4 seconds, so a sustained outage burns every running story's
entire leg ceiling in about 48 seconds and lands the lot in FAILED. That
ceiling exists to stop a run FLAILING — spending legs on work that is
not converging. Spending it on somebody else's downtime is the ceiling
doing the opposite of its job: the run was fine, the provider was not,
and twelve generations later there is nothing left to resume.

So the breaker's real output is not "fail faster". It is **do not count
this against the run**. Retries handle a blip; the breaker handles an
outage, and the difference is whether the story still exists afterwards.

THREE STATES, and the middle one is the one that matters:

  closed     normal. Failures accumulate inside a rolling window.
  open       tripped. Calls are refused WITHOUT being made, so an outage
             costs no tokens and no latency. Refusal is not failure —
             the caller must not advance a generation on it.
  half_open  one probe is allowed through after the probe interval. A
             success closes; a failure re-opens and the wait restarts.

FAILURES ARE COUNTED IN A WINDOW, not cumulatively. A provider that
fails once an hour is healthy, and a cumulative counter trips on it
eventually — which is how a breaker becomes a thing people disable.

Only RETRYABLE faults count. A 401 is a real failure and it is ours: an
outage breaker that trips on a bad API key stops the fleet for a
misconfiguration and reports it as a provider outage.

Process-level and per (provider, model). Not persisted: a worker that
restarts has lost only the knowledge that a provider was down, which it
rediscovers on its first call, and a breaker whose state outlives the
process it was measuring is a breaker nobody is watching.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class ProviderUnavailable(Exception):
    """The breaker is open, or retries were exhausted on a live outage.

    Distinct from every other exception the harness raises, because the
    worker must treat it differently: a story stopped by this did not
    fail, and its generation must not advance.
    """

    def __init__(self, message: str, kind: str = "provider_unavailable",
                 retry_after_s: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after_s = retry_after_s


@dataclass
class CircuitBreaker:
    failures: int = 5
    window_s: float = 60.0
    probe_s: float = 30.0
    enabled: bool = True

    _events: list = field(default_factory=list)
    _opened_at: float | None = None
    _probing: bool = False

    def _now(self) -> float:
        return time.monotonic()

    def state(self) -> str:
        if not self.enabled or self._opened_at is None:
            return CLOSED
        if self._now() - self._opened_at >= self.probe_s:
            return HALF_OPEN
        return OPEN

    def allows(self) -> bool:
        """May a call be made? Advances into probing when it is time."""
        state = self.state()
        if state == CLOSED:
            return True
        if state == HALF_OPEN and not self._probing:
            self._probing = True        # exactly one probe in flight
            return True
        # OPEN, or HALF_OPEN with a probe already out. Either way the
        # answer is no, and saying it plainly beats an expression that
        # has to be traced to see it can only be False here.
        return False

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("provider recovered; circuit closed")
        self._events.clear()
        self._opened_at = None
        self._probing = False

    def record_failure(self, kind: str, retryable: bool) -> None:
        """Only retryable faults count — see the module docstring."""
        if not self.enabled or not retryable:
            return
        now = self._now()
        if self._probing:
            # The probe failed. Restart the wait rather than letting the
            # next tick probe again immediately.
            self._opened_at = now
            self._probing = False
            logger.warning("provider probe failed (%s); circuit re-opened",
                           kind)
            return
        self._events = [t for t in self._events if now - t < self.window_s]
        self._events.append(now)
        if self._opened_at is None and len(self._events) >= self.failures:
            self._opened_at = now
            logger.error("circuit OPEN: %d retryable failures in %.0fs "
                         "(last: %s). Admission stops; running stories "
                         "hold rather than burning their leg ceiling.",
                         len(self._events), self.window_s, kind)

    def seconds_until_probe(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.probe_s - (self._now() - self._opened_at))


_BREAKERS: dict = {}


def for_model(model: str, policy) -> CircuitBreaker:
    """The breaker for one model. Shared across every caller in-process."""
    existing = _BREAKERS.get(model)
    if existing is None:
        existing = CircuitBreaker(
            failures=policy.breaker_failures,
            window_s=policy.breaker_window_s,
            probe_s=policy.breaker_probe_s,
            enabled=policy.breaker_enabled)
        _BREAKERS[model] = existing
    return existing


def reset_all() -> None:
    """For tests. A breaker carried between tests is a shared fixture."""
    _BREAKERS.clear()
