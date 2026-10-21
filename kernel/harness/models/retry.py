"""Retry the CALL, not the leg.

Before this, a transient 429 on step 30 of a leg raised, `_take_leg`
caught it, advanced the generation, and the next tick started a fresh
leg — which is real recovery, and it is **leg-granular**. The cost of
that granularity is E5c's orientation floor: everything the model had
worked out is gone and it pays 4.6–6.1K tokens to work it out again,
because a rate limiter said "not right now".

The step is the right unit. Nothing about the request was wrong, the
conversation is intact in memory, and repeating the call unchanged is
the whole fix.

WHAT THIS IS NOT ALLOWED TO DO:

  * Retry a terminal fault. A 401 or a malformed request will answer
    identically however many times it is asked. `faults.classify` owns
    that judgement so no call site re-decides it.
  * Retry a context overflow. LR2 makes it a PREDICATE — the leg ends
    and a brief is written. Retrying is not merely wasteful, it destroys
    the one mechanism that makes long work possible.
  * Retry a tool that already ran. A timeout is ambiguous: the request
    may have been executed and the response lost. This retries the MODEL
    call, which happens strictly before any tool executes in a step, so
    the ambiguity does not arise here — and the journal's idempotency
    key is the backstop if it ever does.
  * Hide the outage. Exhausted retries raise `ProviderUnavailable`, not
    the raw SDK error, so the worker can tell "the provider is down"
    from "this run failed" — the third time a provider fact was about to
    be recorded as a verdict on the work.

EVERY NUMBER IS CONFIGURATION. `RetryPolicy` carries attempts, base
delay, ceiling, jitter, whether to honour Retry-After, and the breaker's
three figures; `AGENTOS_RETRY_POLICY` overrides any of them and refuses
unknown fields. Setting `max_attempts` to 0 disables retrying entirely
and surfaces the raw fault, which is how the red-check proves the
mechanism is doing something.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.harness.models import faults as _faults
from kernel.harness.models.breaker import ProviderUnavailable

logger = logging.getLogger(__name__)


class TerminalProviderError(Exception):
    """A fault repeating cannot fix. Carries the classification."""

    def __init__(self, fault, original: BaseException):
        super().__init__(fault.detail)
        self.fault = fault
        self.original = original


async def call_with_retry(fn, *, policy, breaker=None, model: str = "",
                          sleep=None):
    """Await `fn()`, retrying transient provider faults.

    Returns whatever `fn` returns. Raises:
      * `ProviderUnavailable` — the breaker is open, or every attempt hit
        a retryable fault. The run is not at fault and must not be.
      * `TerminalProviderError` — classified as not worth repeating.
      * the original exception for `context_overflow`, so the adapter's
        existing LR2 handling sees exactly what it saw before.
    """
    sleep = sleep or asyncio.sleep

    if breaker is not None and not breaker.allows():
        raise ProviderUnavailable(
            f"circuit open for {model or 'provider'}; not calling for "
            f"another {breaker.seconds_until_probe():.0f}s",
            retry_after_s=breaker.seconds_until_probe())

    # attempts = 1 + retries. max_attempts=0 means "call once, no retry",
    # which is the red-check's setting and must still make the call.
    attempts = max(1, policy.max_attempts)
    last = None

    for attempt in range(1, attempts + 1):
        try:
            result = await fn()
        except asyncio.CancelledError:
            # NEVER RETRIED. `faults.classify` returns UNKNOWN for it and
            # UNKNOWN is retryable, so cancelling a leg made this sleep
            # and CALL THE PROVIDER AGAIN -- and charged
            # `breaker.record_failure` for the privilege. A shutdown, or
            # any `wait_for`, could therefore trip the circuit breaker
            # open and stall every run on that model. A cancellation is
            # not a fault; it is the caller saying stop.
            raise
        except BaseException as exc:                        # noqa: BLE001
            fault = _faults.classify(exc)

            # LR2 is a predicate and belongs to the adapter. Hand it back
            # untouched — and do NOT let it near the breaker, since a
            # prompt too long for the window says nothing about whether
            # the provider is up.
            if fault.kind == _faults.OVERFLOW:
                raise

            if breaker is not None:
                breaker.record_failure(fault.kind, fault.retryable)

            if not fault.retryable:
                raise TerminalProviderError(fault, exc) from exc

            last = fault
            if attempt >= attempts:
                break

            delay = policy.delay_for(attempt, fault.retry_after_s)
            logger.warning(
                "provider %s on attempt %d/%d (%s); retrying the CALL in "
                "%.1fs — the leg and its context are kept",
                fault.kind, attempt, attempts, model or "?", delay)
            await sleep(delay)
            continue

        if breaker is not None:
            breaker.record_success()
        return result

    raise ProviderUnavailable(
        f"{attempts} attempt(s) exhausted against {model or 'provider'}; "
        f"last fault {last.kind if last else 'unknown'}: "
        f"{last.detail if last else ''}",
        kind=last.kind if last else "provider_unavailable",
        retry_after_s=last.retry_after_s if last else None)
