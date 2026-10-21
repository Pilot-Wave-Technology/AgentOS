"""How many threads each kind of blocking work may have. One declaration.

NOBODY HAD DECIDED THIS. There is no `set_default_executor` anywhere, so
every `asyncio.to_thread` and `run_in_executor(None, ...)` in this
process shared one pool sized `min(32, os.cpu_count() + 4)` -- and
`os.cpu_count()` reports the NODE's CPUs, not the container's cgroup
limit. The conductor is capped at 1 CPU (deploy/k8s/conductor.yaml), so
that number is 12 on an 8-vCPU node and 6 on a 2-vCPU one. It is not a
tuned value; it is whatever machine the pod landed on.

WHAT COMPETES FOR IT, measured on 2026-09-17:

  * `kernel/bridge/client.py` -- ONE THREAD PER IN-FLIGHT TOOL CALL, held
    for the whole call. At the time that meant 615s (env_setup) or 135s
    (a model `bash`), ten legs deep.
  * `kernel/drivers/k8s_call.py` -- every Kubernetes API call, and
    `dispatch_pass` runs up to 24 dispatches at once, each at least two
    calls: a burst of ~50 submissions.
  * the verifier's `git archive` and tar work, moved off the event loop
    the same day -- which INCREASED the pressure on this pool.

Ten legs in `bash` saturate twelve threads. The fifty k8s items then
queue in the executor's unbounded `SimpleQueue`; `k8s_call` deliberately
has no `wait_for` (its own docstring explains why a belt that leaks a
thread is worse); `dispatch_pass` is awaited inline in `worker_tick`, so
the pass never returns, `alive.touch` never fires, and the liveness probe
SIGKILLs the pod -- discarding in-flight legs, which the peer replica
then re-claims and repeats.

TWO POOLS, NOT ONE BIGGER POOL. Sizing the default pool up would stop
the wedge and leave the coupling: a slow `bash` would still be competing
with dispatch for the same threads, so the worst case would merely need
more legs to reproduce. Tool calls get their own pool, so a sandbox that
takes ten minutes cannot delay a single Kubernetes call.

THREADS ARE CHEAP HERE BECAUSE THE WORK IS I/O. A thread waiting on a
socket costs a stack and nothing else; the pool is sized for how many
things may be OUTSTANDING, not for how much CPU there is. The CPU-bound
members (tar, git) do contend on the GIL, but contending is not wedging
-- the event loop still gets scheduled, which is the property that
matters.

D5 -- WHY THE TOOL POOL NO LONGER DERIVES FROM A LEG COUNT. This file
used to compute `tool_threads()` as `MAX_LEG_CONCURRENCY + 6`, because
the 615s above was a real thing a thread could be holding. E14 (plan
Sec.12) removed that call: `env_setup`, `env_install`, the prepare
install and the verifier's check run are JOBS now, started in the pod
and polled, and `kernel/bridge/limits.py` bounds every remaining bridge
call from the caller's side -- 10s for a poll, 30s for anything with no
timeout of its own, and `BASH_MAX_S + BRIDGE_MARGIN_S` = 135s for the
model's own foreground `bash`, which is the longest hold that exists.

So the number did not need re-deriving from a smaller ceiling; the
derivation itself stopped being the argument. Plan Sec.12.7: "the pool is
sized for polls and Kubernetes calls, both of which are bounded by their
own timeouts already". Sixteen is kept because it is the figure Phase R
soaked, not because 10 + 6 still means anything -- do not "simplify" it
back into an expression over some other module's concurrency setting,
which is the coupling D5 exists to cut.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Tool calls, one thread each while in flight -- and after E14 the
# longest of those is 135s, not 615s (see the docstring). Sixteen covers
# a conductor's whole fan-out of in-flight `bash` calls plus the
# `bridge_health` probes a dispatch fires at pods that are not answering
# yet, with the two no longer able to starve the Kubernetes pool either
# way. Flat, and deliberately not an arithmetic over a leg ceiling.
_TOOL_DEFAULT = 16

# Everything else: Kubernetes calls, tar and git in the verifier, the
# artifact store. Sized for the dispatch burst (~50) with room, because
# the cost of being too small is a wedge and the cost of being too large
# is some stacks.
_INFRA_DEFAULT = 64

_tools: ThreadPoolExecutor | None = None


def _sized(name: str, default: int) -> int:
    """An override that says its default out loud.

    `kernel/` bans `environ.get(x, default)` -- a default nobody can see
    is a policy nobody reviewed.
    """
    if name not in os.environ:
        return default
    try:
        return max(1, int(os.environ[name]))
    except ValueError:
        logger.warning("%s is not a number; using %d", name, default)
        return default


def tool_threads() -> int:
    """No import of `kernel.worker.concurrency`, and that is the point.

    A thread-pool size that reads another module's concurrency knob makes
    raising that knob silently resize this pool, in a process that may
    not even be running legs (the verifier imports this too).
    """
    return _sized("AGENTOS_TOOL_THREADS", _TOOL_DEFAULT)


def infra_threads() -> int:
    return _sized("AGENTOS_INFRA_THREADS", _INFRA_DEFAULT)


def tools() -> ThreadPoolExecutor:
    """The pool tool calls run in. Created once, per process."""
    global _tools
    if _tools is None:
        _tools = ThreadPoolExecutor(max_workers=tool_threads(),
                                    thread_name_prefix="agentos-tool")
    return _tools


def install(loop) -> None:
    """Give this process pools somebody chose. Call before the loops run.

    The default executor is replaced rather than left alone, because
    `asyncio.to_thread` has no way to name one -- every caller that is
    not the bridge lands here whether it knows it or not.
    """
    infra = ThreadPoolExecutor(max_workers=infra_threads(),
                               thread_name_prefix="agentos-infra")
    loop.set_default_executor(infra)
    logger.info("thread pools: infra=%d tools=%d (cpu_count reports %s, "
                "which is the NODE and not this container)",
                infra_threads(), tool_threads(), os.cpu_count(),
                extra={"event": "tick"})


def shutdown() -> None:
    """Let the tool pool go. Best effort -- never blocks the exit path.

    `wait=False`: the drain above this has already given in-flight legs
    their time, and a tool call still running at this point is one whose
    caller is gone. Waiting for it would hold the process past the grace
    period and turn a clean stop into a SIGKILL.
    """
    global _tools
    if _tools is not None:
        _tools.shutdown(wait=False)
        _tools = None
