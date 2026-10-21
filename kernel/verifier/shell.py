"""Running a command in the warm verification pod, over its bridge.

WHAT THIS REPLACES, AND WHAT IT COST. `investigate.py`'s `run` tool
calls `driver.run_ephemeral` PER SHELL COMMAND -- so on Kubernetes a
fifteen-step investigation is fifteen Jobs, fifteen pods, fifteen image
pulls and fifteen fetches of the same tree out of the artifact store,
to answer fifteen questions about one directory that has not changed
between them. The investigator has a step budget of twenty
(`MAX_STEPS`) and a wall clock of six minutes, and the first live run
hit the stop-loss with its report unwritten. A pod that stays up turns
each of those into one HTTP call.

THE SAME BRIDGE THE EXECUTION POD USES, deliberately. `kernel/bridge/`
is already the answer to "run something in a container the caller does
not share a filesystem or a namespace with", it already serialises
tools behind a lock while answering /health, and it already reports a
busy sandbox as a 409 rather than a hang -- all three learned on the
executor's side at some expense. A second mechanism would relearn them.

AND THE POD IT TALKS TO HOLDS NOTHING. The shim runs there under the
Verifier's uid with no network out at all, so the reach this module
grants is a command in an offline container over a tree -- which is the
criminal's cell `investigate.py` describes, now with the door left
open between visits instead of a new cell built for each question.
"""

from __future__ import annotations

from kernel.bridge import jobs_client, limits
from kernel.verifier.pod import BRIDGE_PORT

# E14 — THE CHANNEL IS NOW AS WIDE AS THE BUDGET IT CARRIES. This used
# to clamp to the harness's 600s foreground ceiling while the contract's
# own budget (`kernel/verifier/budget.MAX_S`) is two hours: twelve times
# over, so a suite that needed longer "must not come through this call"
# and there was nowhere else to send it. `limits.JOB_MAX_S` IS that
# budget, enforced in the pod by the job rather than by this call.
HARNESS_MAX_S = limits.JOB_MAX_S


async def run(host: str, command: str, timeout_s: float,
              port: int = BRIDGE_PORT) -> dict:
    """One command in the warm pod. `{output, exit_code, error}`.

    `timeout_s` is CLAMPED rather than refused, which is the opposite of
    what `limits.bash_timeout` does to a model and is right for the
    opposite reason: a model that asked for too long should be told to
    use a different tool, and the verifier has no different tool -- a
    refusal here would turn a long criterion into no criterion at all.
    The caller's budget is the contract's (`kernel/verifier/budget.py`,
    up to two hours) and the clamp now lands on the SAME number, so a
    long suite has somewhere to go -- it runs as a job under a budget
    the pod enforces, and this coroutine polls it.

    NEVER RAISES ON A BRIDGE FAULT, because an unreachable pod is not a
    verdict. `jobs_client` turns every transport failure into a
    `state: "error"` with a message, and the adjudicator reads `error`
    to tell "the check said no" from "the check could not be run" --
    plan 5.8 rows 1 and 3, which today blur into one. A job stopped at
    its budget is a THIRD fact and now says so rather than arriving as
    exit 137.
    """
    budget = max(limits.JOB_MIN_S, min(int(timeout_s), HARNESS_MAX_S))
    # WAITS PAST THE BUDGET, never inside it. The pod kills the job at
    # `budget`; this pass has to still be here to read the exit code, or
    # a suite that ran out of time would be reported as a suite nobody
    # watched. `BRIDGE_MARGIN_S` is the measured margin between the two
    # and exists for exactly this.
    job = await jobs_client.run_job(
        command, host=host, port=port, budget_s=budget,
        wait_s=budget + limits.BRIDGE_MARGIN_S)
    if job["state"] == "running":
        # Defensive: the wait above clears the budget, so getting here
        # means the pod did not enforce it. Stop the job rather than
        # leave a suite running in a pod the next criterion reuses.
        await jobs_client.cancel(job["job"], host=host, port=port)
        return {"output": job["output"], "exit_code": 1,
                "error": f"{job['job']} outlived its {budget}s budget"}
    if job["state"] == "error":
        return {"output": job["output"], "exit_code": 1,
                "error": job.get("error") or "the bridge did not answer"}
    if job.get("ended") == "budget":
        return {"output": job["output"], "exit_code": 124,
                "error": f"E_TIMEOUT: stopped at its {budget}s budget"}
    return {"output": job["output"], "exit_code": job["exit_code"],
            "error": ""}


async def healthy(host: str, port: int = BRIDGE_PORT,
                  timeout: int = 5) -> bool:
    """Is the shim answering?

    A SEPARATE QUESTION FROM `k8s_verify.await_ready`, and both are
    needed. Readiness is what the API server last observed, on a two
    second period; this is what is true now, from here. The pair that
    matters is "ready and unreachable" -- a NetworkPolicy that does not
    admit the verifier -- which under default-deny is a DROP and
    therefore a hang, and is exactly how a missing line of YAML reads
    as a slow criterion.
    """
    from kernel.bridge.client import bridge_health           # noqa: PLC0415

    return await bridge_health(port=port, timeout=timeout, host=host)
