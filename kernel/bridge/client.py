"""Bridge client — worker-side HTTP client for the sandbox bridge.

Calls the shim running inside the sandbox on localhost:9100 (shared netns).
The worker is the client; the sandbox is the server. This direction ensures
tools execute in the sandbox, not where the git credential lives (rule 9).
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from kernel.harness.models.types import ToolResult

BRIDGE_PORT = 9100
BRIDGE_TIMEOUT = 30  # seconds

# Where the shim answers when nobody says otherwise. Docker and E0 share
# a network namespace with the sandbox, so this is both correct and the
# tightest possible reach; a pod substrate overrides it per execution.
BRIDGE_HOST = "localhost"


async def call_bridge(
    tool: str,
    args: dict,
    tool_call_id: str,
    port: int = BRIDGE_PORT,
    timeout: int = BRIDGE_TIMEOUT,
    host: str = BRIDGE_HOST,
    extras: dict | None = None,
) -> ToolResult:
    """Call a tool via the bridge. Returns ToolResult.

    Runs the HTTP call in a thread to avoid blocking the event loop.

    `host` WAS HARDCODED `localhost`, and that is right on exactly one
    substrate. On Docker the sandbox joins the caller's network
    namespace, so localhost IS the sandbox and nothing else can reach
    the shim. On Kubernetes the caller is a different POD: there is no
    localhost route to the sandbox at all, and every tool call would
    have gone to the conductor's own loopback and been refused.

    Defaulted rather than required, so Docker and E0 call this exactly
    as before.

    `extras` IS AN OUT-PARAMETER, and it exists because `ToolResult` is
    deliberately narrow -- id, output, error, exit code -- while a few
    tools answer with structure the WORKER needs and the model must not
    read. `bash_output` is the case: it renders an exit code inside a
    sentence for a model, and the worker separately wants the job's
    state as data to mirror. Parsing the prose would be the fault shape
    that once reported a 45MB capture as "stored nothing".

    A dict passed here is updated with every non-envelope key the
    sandbox returned. Callers that pass nothing are unaffected, which is
    all of them but one.
    """
    def _sync_call():
        url = f"http://{host}:{port}/tool"
        payload = json.dumps({
            "tool": tool,
            "args": args,
            "id": tool_call_id,
        }).encode()

        req = Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
                if extras is not None and isinstance(body, dict):
                    # Everything the envelope does not itself define. The
                    # narrow four go to the model; the rest is for the
                    # worker, and nothing parses the prose to get it.
                    extras.update(
                        {k: v for k, v in body.items()
                         if k not in ("output", "error", "error_code",
                                      "retryable", "exit_code")})
                return ToolResult(
                    tool_call_id=tool_call_id,
                    output=body.get("output", ""),
                    error=body.get("error"),
                    exit_code=body.get("exit_code", 0),
                )
        except HTTPError as e:
            # A REPLY, NOT A FAILURE TO REACH. `urlopen` raises on any
            # non-2xx, and `HTTPError` is a subclass of `URLError` -- so
            # without this arm the shim's 409 "the sandbox is busy"
            # arrives as "bridge unreachable", which is the exact
            # misdiagnosis the 409 exists to prevent. The body carries
            # what is holding the sandbox and for how long; throwing it
            # away to report the status line loses the whole message.
            try:
                body = json.loads(e.read() or b"{}")
            except Exception:                               # noqa: BLE001
                body = {}
            return ToolResult(
                tool_call_id=tool_call_id,
                output=body.get("output", ""),
                error=body.get("error") or f"bridge returned HTTP {e.code}",
                exit_code=body.get("exit_code", 1),
            )
        except URLError as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                output="",
                error=f"bridge unreachable: {e}",
                exit_code=1,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                output="",
                error=f"bridge error: {type(e).__name__}: {e}",
                exit_code=1,
            )

    # THE TOOL POOL, not the default one. A tool call holds its thread
    # for the whole call, and one conductor has several legs in flight.
    # Sharing the default executor meant a handful of slow legs could
    # starve every Kubernetes call in the process, which stops the tick,
    # which ends in a liveness kill. See kernel/executors.py.
    #
    # THE LONGEST CALL IS NOW THE MODEL'S OWN `bash`: `BASH_MAX_S` 120 +
    # `BRIDGE_MARGIN_S` 15 = 135s, and `bridge_timeout` gives every other
    # tool 30s. The 615s `env_setup` the pool was once sized around does
    # not exist as a call at all -- E14 made it a job, and
    # `kernel/bridge/jobs_client.py` holds a thread for a 10s poll
    # instead. That is what let D5 retire `MAX_LEG_CONCURRENCY`: the pool
    # is now a flat 16 sized for polls, short calls and Kubernetes work,
    # and no longer an arithmetic over a leg count. The single ceiling on
    # how much runs at once is `admit.SYSTEM_CEILING`.
    from kernel.executors import tools                       # noqa: PLC0415

    return await asyncio.get_event_loop().run_in_executor(
        tools(), _sync_call)


async def bridge_health(port: int = BRIDGE_PORT, timeout: int = 5,
                        host: str = BRIDGE_HOST) -> bool:
    """Check if the bridge shim is reachable. Returns True if healthy.

    Takes the same host as `call_bridge` and for the same reason: a
    readiness probe aimed at the caller's own loopback reports a pod
    sandbox as never ready, which reads as a hung dispatch.
    """
    def _sync_check():
        try:
            with urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    # Same pool as the call above: a readiness probe against a sandbox
    # that is not answering yet waits the full `timeout`, and it is aimed
    # at the same pods.
    from kernel.executors import tools                       # noqa: PLC0415

    return await asyncio.get_event_loop().run_in_executor(
        tools(), _sync_check)
