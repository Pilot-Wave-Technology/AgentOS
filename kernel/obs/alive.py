"""Is this process's event loop still turning? A file mtime says so.

THE THIRD PART OF THE WEDGE, and the reason it lasted thirty minutes.
`kernel/drivers/k8s_call.py` describes the fault; this is why nobody
found out. `loop_heartbeats` recorded it perfectly -- `worker.pass`
stale 0:29:55 at tick 825 -- and `heartbeat.stale()` was written to ask
exactly that question. Nothing called it, there was no liveness probe
in `deploy/k8s/conductor.yaml`, and so both pods sat READY 2/2 while
doing nothing at all. Recovery was a person noticing.

WHY A FILE AND NOT THE DATABASE. The check has to work when the process
is wedged, which is when a database round trip is least trustworthy --
and the row is written BY the loop that has stopped, so asking the
database means asking the patient. A file's mtime is written by the same
loop but READ from outside the process entirely: an `exec` probe is run
by the kubelet, so it keeps working while every coroutine is blocked.
That is the one property this needs and an in-process HTTP health
endpoint would not have given us without a server the conductor does not
run.

WHY THE NEWEST TICK ACROSS ALL LOOPS, AND NOT EACH LOOP SEPARATELY.
This answers "is the process wedged", which is the failure that was
measured: a blocking call freezes every coroutine at once, so ALL the
files go stale together. One loop stalling while the others tick is a
narrower and much less dangerous fault -- and restarting a pod over it
would take out the healthy loops too. That one deserves an alert, which
`heartbeat.stale()` already knows how to ask for. Restarting is reserved
for the case where nothing at all is moving.

DRAINING IS NOT WEDGED. `run_forever` drains in-flight legs for up to
`AGENTOS_DRAIN_MAX_S` (180s in production, under a 210s grace period),
and the loops stop ticking the moment that starts. Without the marker
this probe would fail partway through every ordinary rollout and SIGKILL
the pod mid-drain -- turning a safety measure into the thing that
discards in-flight legs. So a draining process reports healthy: it is
shutting down on purpose and Kubernetes is already timing it.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

logger = logging.getLogger(__name__)

# `/tmp` because it is writable by every uid in every image, is not the
# workspace, and does not survive a restart -- which is correct: after a
# restart there is nothing to attest to yet. Spelled with the explicit
# `in os.environ` form because `kernel/` bans a default nobody can see.
BEAT_DIR = (os.environ["AGENTOS_ALIVE_DIR"]
            if "AGENTOS_ALIVE_DIR" in os.environ
            else "/tmp/agentos-alive")

# One file per loop; this name is not a loop and cannot collide with one
# because `/` is not legal in a loop name and every loop name has a dot.
DRAIN_MARKER = "draining"


def _path(name: str) -> str:
    return os.path.join(BEAT_DIR, name.replace("/", "_"))


# A loop is late when it has missed this many of its own passes.
#
# TEN, AND DELIBERATELY CONSERVATIVE. This number restarts pods, and the
# only thing separating "a long pass" from "a hung pass" from outside the
# process is this multiplier -- there is no third signal. `reconcile_all`
# declares a 60s interval but its BODY is a sweep over every goal making
# Kubernetes calls, each now bounded at 30s; a slow pass can legitimately
# run for minutes, and at a factor of four (240s) an ordinary busy
# reconcile would have restarted the pod in a loop.
#
# Ten still catches the measured failure by a wide margin -- the wedge
# that started this went unnoticed for THIRTY MINUTES -- and Phase T adds
# real pass-duration telemetry, which is what should tighten this rather
# than a guess made before the data exists.
_LATE_FACTOR = 10

# The floor, for a loop that declared no interval and for fast loops
# where four intervals would be seconds.
_FLOOR_S = 120.0


def touch(loop_name: str, every: float | None = None) -> None:
    """This loop just completed a pass. NEVER RAISES.

    `every` is the loop's OWN expected interval, and recording it is what
    makes a per-loop verdict possible. Without it the only question this
    module could answer was "has ANYTHING ticked recently" -- and the
    answer was yes, from `worker.pass` every four seconds, while
    `reconcile_all` hung in a Kubernetes call for as long as you like.
    That is not a hypothetical: a hung reconcile also HOLDS THE ADVISORY
    LOCK, so the peer replica stops reconciling too. One dead loop, both
    replicas degraded, probe green.


    Called from `heartbeat.beat`'s `finally`, where an exception would
    replace whatever the loop was actually reporting. An instrument that
    can take down what it measures is a liability -- the same argument
    the heartbeat's own database write makes one line further down.
    """
    try:
        os.makedirs(BEAT_DIR, exist_ok=True)
        with open(_path(loop_name), "w") as fh:
            # "<when> <expected interval>". Two fields so an older file
            # written by the previous build still parses as "no interval
            # declared" rather than crashing the probe mid-rollout.
            fh.write(f"{time.time()} {every if every else ''}".strip())
    except Exception as exc:                                # noqa: BLE001
        # BROADER THAN OSError ON PURPOSE. A misconfigured
        # AGENTOS_ALIVE_DIR raises ValueError, not OSError, and this
        # runs inside heartbeat's `finally` -- where anything raised
        # REPLACES the exception the loop was actually reporting. An
        # instrument that can take down what it measures is a
        # liability; the same argument the heartbeat's own database
        # write makes one line further down.
        logger.warning("could not record liveness for %s: %s",
                       loop_name, exc)


def draining(on: bool = True) -> None:
    """Shutting down on purpose. NEVER RAISES, for the same reason."""
    try:
        os.makedirs(BEAT_DIR, exist_ok=True)
        if on:
            with open(_path(DRAIN_MARKER), "w") as fh:
                fh.write(str(time.time()))
        elif os.path.exists(_path(DRAIN_MARKER)):
            os.remove(_path(DRAIN_MARKER))
    except Exception as exc:                                # noqa: BLE001
        logger.warning("could not record the drain marker: %s", exc)


def newest_age_s(now: float | None = None) -> float | None:
    """Seconds since ANY loop last finished a pass, or None if none has.

    None is not "unhealthy". A process that has not completed its first
    pass is starting up, and the probe's `initialDelaySeconds` is what
    covers that window -- reporting it as a failure here would restart
    every pod that takes longer than usual to reach its database.
    """
    now = time.time() if now is None else now
    try:
        names = os.listdir(BEAT_DIR)
    except Exception:                                       # noqa: BLE001
        return None
    ages = [now - os.path.getmtime(os.path.join(BEAT_DIR, n))
            for n in names if n != DRAIN_MARKER
            and os.path.exists(os.path.join(BEAT_DIR, n))]
    return min(ages) if ages else None


def _declared(name: str) -> float | None:
    """The interval this loop said it runs at, if it said one."""
    try:
        with open(os.path.join(BEAT_DIR, name)) as fh:
            parts = fh.read().split()
        return float(parts[1]) if len(parts) > 1 else None
    except Exception:                                       # noqa: BLE001
        return None


def late(max_age_s: float, now: float | None = None) -> list[tuple]:
    """Every loop past its own deadline: [(name, age, limit), ...].

    PER LOOP, because the interesting failure is one loop stopping while
    the others carry on -- and the fast ones always carry on.
    """
    now = time.time() if now is None else now
    out = []
    try:
        names = os.listdir(BEAT_DIR)
    except Exception:                                       # noqa: BLE001
        return out
    for name in names:
        if name == DRAIN_MARKER:
            continue
        path = os.path.join(BEAT_DIR, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        every = _declared(name)
        limit = max(max_age_s, _LATE_FACTOR * every) if every else max_age_s
        if age > limit:
            out.append((name, age, limit))
    return out


def verdict(max_age_s: float, now: float | None = None) -> tuple[bool, str]:
    """(healthy, why). The whole decision, so a test can hold it."""
    if os.path.exists(_path(DRAIN_MARKER)):
        return True, "draining on purpose"
    age = newest_age_s(now)
    if age is None:
        return True, "no pass has finished yet (still starting)"
    if age > max_age_s:
        return False, (f"no loop in this process has completed a pass in "
                       f"{age:.0f}s (limit {max_age_s:.0f}s) -- the event "
                       f"loop is wedged, see kernel/drivers/k8s_call.py")
    # AND THE NARROWER CASE. Everything above answers "is the process
    # wedged". This answers "has one loop stopped while the others cover
    # for it" -- which is the failure the first version could not see,
    # and which degrades BOTH replicas when the loop holding the
    # reconcile lock is the one that stopped.
    stalled = late(max_age_s, now)
    if stalled:
        worst = ", ".join(f"{n} {a:.0f}s (limit {lim:.0f}s)"
                          for n, a, lim in stalled)
        return False, (f"a loop has stopped while others keep ticking: "
                       f"{worst}. A hung pass that holds the reconcile "
                       f"lock stops the peer replica too.")
    return True, f"last pass {age:.0f}s ago"


def dump_the_wedged_process(pid: int = 1) -> None:
    """Ask the conductor for its stacks, on the way to killing it.

    THE PROBE IS THE ONLY THING PRESENT AT THE RIGHT MOMENT. A wedge
    takes about seventeen minutes to appear and lasts until the kubelet
    kills the pod -- so catching one by hand means watching, and by the
    time anyone looks the evidence has been restarted away. Diagnosing
    it from outside has already cost several rounds of speculation.

    This process is NOT the wedged one: the probe is exec'd fresh by the
    kubelet, which is exactly why it still works when the event loop
    does not. It cannot read the other process's stack, but it can ask
    for it -- `service.py` registers `faulthandler` on SIGUSR1, which
    writes every thread's Python stack to stderr, which is the log.

    So the dump lands in the log seconds before the container is killed,
    every time, with nobody watching.

    NEVER RAISES, and never changes the verdict. A probe that failed
    because it could not take a diagnostic would turn an instrument into
    an outage.
    """
    try:
        os.kill(pid, signal.SIGUSR1)
    except Exception as exc:                                # noqa: BLE001
        print(f"(could not ask pid {pid} for a stack dump: {exc})")


def main(argv: list[str] | None = None) -> int:
    """`python -m kernel.obs.alive <max_age_s>` for an exec probe.

    A module and not a shell one-liner in the manifest, so the rule is
    in one place, is testable, and reads the same in both.
    """
    argv = sys.argv[1:] if argv is None else argv
    max_age = float(argv[0]) if argv else 120.0
    healthy, why = verdict(max_age)
    print(why)
    if not healthy:
        # BEFORE returning failure, because returning it is what starts
        # the clock on the kill.
        dump_the_wedged_process()
    return 0 if healthy else 1


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
