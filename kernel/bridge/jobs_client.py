"""E14, worker side: start a job in the sandbox, poll it, cancel it.

WHAT THE CALLER GETS THAT `call_bridge` COULD NOT GIVE IT. `call_bridge`
returns a `ToolResult` — output, error, exit code — which is the right
shape for a model's tool call and the wrong one for this: a job that is
still running has no exit code, and inventing one is precisely the
mistake `kernel/drivers/k8s_outcome.py` documents. So these helpers
return the shim's structured payload unchanged, and `state` is the field
a caller branches on.

NO CALL HERE IS EVER LONG. `start` spawns and returns, `poll` reads a
buffer, `cancel` signals — the budget is enforced in the pod. That is
what retires the sizing argument in `kernel/executors.py`: a tool-pool
thread is held for a poll, never for an install.

AND A LOST POLL COSTS NOTHING, which a lost blocking call did not. The
job outlives its poller; the next tick asks again by id. `run_job` takes
a `wait_s` for exactly that reason — it is how long THIS pass is willing
to watch, not how long the work may take, and a caller that runs out of
patience re-attaches with the same `key` on its next pass rather than
starting a second install.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from base64 import b64decode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kernel.bridge.client import BRIDGE_HOST, BRIDGE_PORT
from kernel.bridge.limits import (JOB_CANCEL, JOB_DEFAULT_S, JOB_POLL,
                                  JOB_START, bridge_timeout)

logger = logging.getLogger(__name__)

# How long a pass watches before leaving the job for the next one. Two
# minutes is what `kernel/worker/advance.py` already accepts for setup
# under the leg lock ("seconds to minutes"); past that the level-
# triggered answer is better than a longer hold.
DEFAULT_WAIT_S = 120.0

# Polls start eager and slow down: a 3-second `npm ci` should not cost a
# 5-second wait, and a 20-minute one should not cost 1200 polls.
#
# THE CEILING WENT 5 -> 15. At five seconds a two-hour job costs 1,440
# internal polls; at fifteen it costs 480, and the only thing the extra
# ten seconds buys is how quickly a FINISHED job is noticed -- which is
# noise against a job measured in hours, and the eager start keeps a
# three-second job answering in three seconds.
FIRST_INTERVAL_S = 1.0
MAX_INTERVAL_S = 15.0


def _knob(name: str, fallback: float) -> float:
    """A timings knob from the pod's env, or the standing value.

    RESOLVED AT CALL TIME, NOT AT IMPORT. `wait_s` and the poll ceiling
    are Python default arguments, bound when the function is defined, so
    rebinding a module constant after reading a setting changes nothing.
    A function call is the only shape that can be changed per dispatch.

    One-argument `.get` behind an `in os.environ` test: the two-argument
    form is banned across `kernel/`, and a bare subscript would raise on
    every production pod, which sets none of these.
    """
    if name not in os.environ:
        return fallback
    try:
        return max(0.1, float(os.environ.get(name) or fallback))
    except (TypeError, ValueError):
        return fallback


def wait_slice_s() -> float:
    return _knob("AGENTOS_WAIT_SLICE_S", DEFAULT_WAIT_S)


def poll_interval_max_s() -> float:
    return _knob("AGENTOS_POLL_INTERVAL_MAX_S", MAX_INTERVAL_S)

# Consecutive failed polls before a pass gives up on WATCHING. Not on the
# job: it is still in the pod either way. One dropped poll must not be
# reported as a failed install -- that is the misdiagnosis this whole
# change exists to stop, and it would arrive as "workspace setup failed"
# on a run whose install was fine.
POLL_RETRIES = 3

# What one caller keeps in memory from a chatty job. The tail, for the
# reason `tools_jobs` keeps the tail: nobody debugging an install reads
# its banner, and every consumer here already slices the last few
# hundred characters.
MAX_KEPT_BYTES = 256 * 1024


def _post(tool: str, args: dict, host: str, port: int, timeout: int) -> dict:
    """One bridge call, returning the shim's JSON body.

    SEPARATE FROM `call_bridge` rather than shared with it, and the
    duplication is deliberate twice over: that function's contract is a
    `ToolResult`, which drops every structured field this module exists
    to carry, and a test pins the shape of its source (the HTTPError arm
    must precede the URLError one, or a busy sandbox reads as an
    unreachable one). The same arm order is kept here for the same
    reason.
    """
    payload = json.dumps({"tool": tool, "args": args,
                          "id": f"harness-{tool}"}).encode()
    req = Request(f"http://{host}:{port}/tool", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:                                   # noqa: BLE001
            body = {}
        return {"error": body.get("error") or f"bridge returned {e.code}"}
    except URLError as e:
        return {"error": f"bridge unreachable: {e}"}
    except Exception as e:                                  # noqa: BLE001
        return {"error": f"bridge error: {type(e).__name__}: {e}"}


async def _call(tool: str, args: dict, host: str, port: int) -> dict:
    """The shim's `result` payload, or `{"error": ...}`."""
    from kernel.executors import tools                       # noqa: PLC0415

    body = await asyncio.get_event_loop().run_in_executor(
        tools(), _post, tool, args, host, port,
        bridge_timeout(tool, args))
    if body.get("error"):
        return {"error": body["error"]}
    result = body.get("result")
    if not isinstance(result, dict):
        return {"error": f"{tool}: the bridge answered without a result"}
    return result


async def start(command: str, *, host: str = BRIDGE_HOST,
                port: int = BRIDGE_PORT, budget_s: int = JOB_DEFAULT_S,
                cwd: str | None = None, env: dict | None = None,
                key: str | None = None) -> dict:
    """`{"job": id, "reattached": bool}` or `{"error": ...}`.

    `key` MAKES THE START IDEMPOTENT while the job runs, which is what
    lets a restarted worker find an install instead of racing it — two
    `pip`s against one tree is a measured failure (execution 200), not a
    theoretical one.
    """
    args: dict = {"command": command, "budget_s": budget_s}
    if cwd:
        args["cwd"] = cwd
    if env:
        args["env"] = env
    if key:
        args["key"] = key
    return await _call(JOB_START, args, host, port)


async def poll(job: str, since: int = 0, *, host: str = BRIDGE_HOST,
               port: int = BRIDGE_PORT) -> dict:
    """`{state, exit_code, bytes, offset, elapsed_s, ended, dropped}`."""
    return await _call(JOB_POLL, {"job": job, "since": since}, host, port)


async def cancel(job: str, *, host: str = BRIDGE_HOST,
                 port: int = BRIDGE_PORT) -> dict:
    return await _call(JOB_CANCEL, {"job": job}, host, port)


def _keep(buf: bytearray, chunk: bytes) -> None:
    """Append, keeping the tail. BYTES, not text: a multi-byte character
    split across two polls only decodes if the halves are joined first,
    and decoding each chunk alone turns one character into two
    replacement marks."""
    buf.extend(chunk)
    if len(buf) > MAX_KEPT_BYTES:
        del buf[:len(buf) - MAX_KEPT_BYTES]


def _text(buf: bytearray) -> str:
    """Decoded ONCE, over everything held. `errors="replace"` then only
    marks bytes that are genuinely not UTF-8, rather than the seam
    between two polls."""
    return bytes(buf).decode("utf-8", "replace")


async def run_job(command: str, *, host: str = BRIDGE_HOST,
                  port: int = BRIDGE_PORT, budget_s: int = JOB_DEFAULT_S,
                  cwd: str | None = None, env: dict | None = None,
                  key: str | None = None,
                  wait_s: float | None = None) -> dict:
    """Start (or re-attach to) a job and watch it for up to `wait_s`.

    Returns `{state, exit_code, output, job, elapsed_s, ended}` where
    `state` is "exited", "running" or "error". A "running" answer is not
    a failure and must never be reported as one: the work is still
    going, and the caller's next pass re-attaches with the same `key`.
    """
    started = await start(command, host=host, port=port, budget_s=budget_s,
                          cwd=cwd, env=env, key=key)
    if started.get("error"):
        return {"state": "error", "exit_code": None, "output": "",
                "job": None, "error": started["error"]}
    job = started["job"]
    logger.info("harness job %s %s: %s", job,
                "re-attached" if started.get("reattached") else "started",
                command[:120])
    # None rather than a default argument, so a compressed profile
    # actually reaches this: a default binds once, at import.
    wait_s = wait_slice_s() if wait_s is None else wait_s
    poll_max = poll_interval_max_s()
    deadline = time.monotonic() + wait_s
    buf, cursor, interval = bytearray(), 0, FIRST_INTERVAL_S
    last, lost, misses = {}, 0, 0
    while True:
        before = cursor
        answer = await poll(job, cursor, host=host, port=port)
        if answer.get("error"):
            # A POLL THAT DID NOT LAND SAYS NOTHING ABOUT THE JOB, so it
            # is retried before it is believed.
            misses += 1
            if misses >= POLL_RETRIES:
                return {"state": "error", "exit_code": None,
                        "output": _text(buf), "job": job,
                        "error": answer["error"]}
            await asyncio.sleep(min(interval, poll_max))
            continue
        misses = 0
        last = answer
        _keep(buf, b64decode(answer.get("bytes") or ""))
        cursor = answer.get("offset", cursor)
        lost += answer.get("dropped") or 0
        if answer.get("state") == "exited":
            # ONE POLL IS NOT THE WHOLE OUTPUT. A poll answers with at
            # most POLL_BYTES, so an exited job with a large tail still
            # has bytes to hand over — returning here would truncate the
            # log of the run that just failed, which is the only thing
            # anybody wants to read.
            #
            # ONLY WHILE THE CURSOR MOVES. Draining without that check is
            # a hot loop with no sleep in it the moment an offset stops
            # advancing, and a spin inside the conductor is worse than a
            # truncated log.
            if answer.get("bytes") and cursor > before:
                continue
            logger.info("harness job %s %s exit=%s in %.1fs", job,
                        answer.get("ended"), answer.get("exit_code"),
                        answer.get("elapsed_s") or 0.0)
            return {"state": "exited", "exit_code": answer.get("exit_code"),
                    "output": _text(buf), "job": job,
                    "ended": answer.get("ended"),
                    "elapsed_s": answer.get("elapsed_s"), "dropped": lost}
        if time.monotonic() >= deadline:
            logger.info("harness job %s still running after %.0fs; left for "
                        "the next pass", job, wait_s)
            return {"state": "running", "exit_code": None,
                    "output": _text(buf), "job": job,
                    "elapsed_s": last.get("elapsed_s"),
                    "dropped": lost}
        await asyncio.sleep(min(interval, poll_max))
        interval = min(interval * 1.5, poll_max)
