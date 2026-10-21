"""Every criterion in ONE warm pod, and the verdicts read back out.

E19, and the module `test_the_batch_runner_survives_its_checks.py` has
been citing since E2 -- its docstring says "see `batch._timed_out`" and
nothing here existed. The runner (the shell program) was built; the
thing that CALLS it was not, which is the same gap 5.19 records one
layer up: Phase E wired the code that reaps verification pods and not
the code that uses them.

WHAT THIS REPLACES. `verify.py` ran one criterion per Kubernetes Job:
`evaluate(...)` -> a container -> a verdict. Correct, and it pays a pod
start, an image pull and an RWO attach PER CHECK. 5.11 replaced it with
a batch in the pod that is already warm and already holds the tree and
the `.venv` on `volume.TREE` / `volume.ENV`.

WHAT SURVIVES THE CHANGE, because each was measured:

  per-check timeouts    NOT one deadline for the batch. `budget.py`
                        exists because a hardcoded 300s let a real
                        suite time out, spend the adjudicator's
                        strikes, and tell the executor to fix an
                        environment that was not broken.
  crash isolation       one check dying does not take the verdicts
                        around it. Free with one Job per criterion;
                        the first thing batching could lose.
  no forged verdicts    a check's stdout goes to a FILE, so a line
                        shaped like a result line comes back as
                        OUTPUT. The marker is a constant, not a
                        secret, and a repository-authored test could
                        otherwise print a `pass` for its neighbour.
  per-check environment two checks in one container, two values for
                        one variable -- `environment_for` still
                        resolves per criterion, so mount A's `.venv`
                        cannot shadow B's.

All four are proved against a REAL `sh` in that test file. This module
is what makes them matter.

AND THE VERDICT THIS MODULE MUST NEVER INVENT. A criterion the runner
did not report is `error`, never `fail`. `outcome.from_exit` enforces
"no exit code means no verdict" for one check; the same rule holds for
a check that never appears in the output at all -- a batch truncated at
64KB, a pod evicted mid-run, a preflight that found no `base64`. Every
one of those is a machine that failed, and blaming the work for it is
the defect R1 spent execution 201 on: 168 tool calls over 7 generations
repairing work that was never broken.
"""

from __future__ import annotations

import logging

from kernel.verifier import runner, volume
from kernel.verifier.budget import timeout_for
from kernel.verifier.criterion_env import environment_for
from kernel.verifier.outcome import Outcome, from_exit, unrunnable

logger = logging.getLogger(__name__)

# What one `shell.run` call is given beyond the checks' own budgets.
# The runner writes every script before the first one runs, execs
# `timeout` per check and base64s each result; none of that is free and
# none of it scales with the work.
BATCH_MARGIN_S = 60.0


def checks_for(contract: dict, criteria: list[dict],
               mounts_env: dict) -> list[runner.Check]:
    """One `runner.Check` per criterion, in the order given.

    ORDER IS THE IDENTITY. The runner reports by INDEX, so the list the
    caller passed and the list parsed back are joined positionally --
    which is why nothing here filters, sorts or skips. A criterion with
    no command still takes its slot; it is refused below rather than
    silently shifting every verdict after it onto the wrong check.
    """
    out = []
    for criterion in criteria:
        command = (criterion.get("spec") or {}).get("run") or ""
        env, prepend = environment_for(command, mounts_env)
        out.append(runner.Check(command, int(timeout_for(contract, criterion)),
                                env=env, path_prepend=prepend))
    return out


def _timed_out(result: dict, budget: int) -> bool:
    """Did this check die AT its deadline, or merely exit 124?

    THE ELAPSED TIME IS THE OTHER HALF, and `outcome.py` is explicit
    that a check genuinely exiting 124 has FAILED -- `timeout` is an
    ordinary program and a suite may return its status for its own
    reasons. Only a 124 that arrives at the budget is a timeout, and
    the difference decides `fail` against `error`, which decides
    whether the executor is told to fix something.
    """
    if result.get("exit") not in runner.TIMEOUT_EXITS:
        return False
    elapsed = result.get("elapsed")
    if elapsed is None:
        # The runner reported a timeout exit and no clock. Treat it as
        # the timeout: the alternative reads a machine's own signal as
        # the work failing.
        return True
    return float(elapsed) >= float(budget)


def budget_for(checks: list[runner.Check]) -> float:
    """What the whole batch may take: the sum, plus the runner's own.

    SUM AND NOT MAX. The checks run in sequence -- one container, one
    shell -- so a batch whose budget was the longest single check would
    be stopped part-way through by its own caller, and the checks that
    never ran would come back absent, which this module reads as
    `error`. A machine's arithmetic would become several criteria
    reported as unrunnable.
    """
    return sum(c.timeout_s for c in checks) + BATCH_MARGIN_S


def outcomes(criteria: list[dict], checks: list[runner.Check],
             parsed: dict, missing: str,
             transport_error: str = "") -> list[tuple[dict, Outcome]]:
    """Join the runner's indexed results back onto the criteria.

    `missing` is the preflight's answer -- the tools the runner needs
    and did not find. It is not a property of any one check, so it
    refuses ALL of them: a batch whose shell has no `base64` produced
    no verdicts, and the honest report is that none was run.
    """
    if missing:
        return [(c, unrunnable(
            "the verification pod's shell is missing " + missing,
            criterion_id=c.get("id"))) for c in criteria]

    results = []
    for index, criterion in enumerate(criteria):
        got = parsed.get(index)
        if got is None:
            # NEVER `fail`. See the module docstring: a check that did
            # not report is a machine that failed, and the executor
            # must not be sent to repair it.
            results.append((criterion, unrunnable(
                transport_error or
                "the batch returned no result for this criterion",
                criterion_id=criterion.get("id"),
                kind=criterion.get("kind"))))
            continue
        budget = checks[index].timeout_s
        results.append((criterion, from_exit(
            got.get("exit"), got.get("output") or "",
            checks[index].command,
            timed_out=_timed_out(got, budget),
            timeout_s=budget,
            # DECLARED, because the criterion carries its own budget
            # through `timeout_for`. `from_exit` reads an undeclared
            # timeout as `error` -- nobody chose the number, so nobody
            # can say the work overran -- and a declared one as `fail`.
            timeout_declared=True)))
    return results


async def run(host: str, contract: dict, criteria: list[dict],
              mounts_env: dict, *, cwd: str = volume.TREE,
              port: int | None = None) -> list[tuple[dict, Outcome]]:
    """Run every criterion in the warm pod at `host`. Never raises.

    `cwd` is `volume.TREE` -- the committed tree the prepare Job pulled
    onto the claim, replaced every round, beside the `volume.ENV` it
    built. NOT `kinds.TREE_PATH`, which is where the tar path unpacked
    a copy; the whole point of 5.5 is that no tree is shipped.

    A bridge fault is an `error` for every criterion and not an
    exception, because an unreachable pod is not a verdict -- `shell.run`
    already turns transport failures into a message rather than raising,
    and this keeps that property at the boundary the caller sees.
    """
    from kernel.verifier import shell                       # noqa: PLC0415

    checks = checks_for(contract, criteria, mounts_env)
    if not checks:
        return []

    kwargs = {} if port is None else {"port": port}
    answer = await shell.run(host, runner.script(checks, cwd=cwd),
                             budget_for(checks), **kwargs)

    parsed, missing = runner.parse(answer.get("output") or "")
    transport = answer.get("error") or ""
    if transport:
        logger.warning("batch: %s criteria at %s: %s",
                       len(criteria), host, transport)
    return outcomes(criteria, checks, parsed, missing, transport)


async def warm_for(conn, driver, execution_id) -> dict | None:
    """The warm pod to verify in, or None to use the tar path.

    ASKED, NOT TRIED -- `prepare_volume._substrate_refusal` makes the
    same call and for the same reason: "this substrate has no claims"
    and "the pod is not up" are different facts, and surfacing the first
    as a traceback from the middle of a verification makes an
    unsupported substrate read as a broken one. Docker and E0 answer
    None here and take the tar path, which is not a fallback so much as
    the only thing that was ever possible there.

    NONE ON ANY DOUBT. A missing pod, an address that is not up yet, a
    row with no `env_key` -- every one returns None and the caller
    archives a tree instead. That costs a tar; the other direction runs
    the criteria against `volume.TREE` on a claim whose contents nobody
    has established, which is a check passing that should fail. 5.10's
    whole argument is that the environment must be IDENTIFIED before it
    is trusted, and `env_key` is the identification.
    """
    from kernel.verifier.handover import claim_capable        # noqa: PLC0415

    if not claim_capable(driver):
        return None
    if execution_id is None:
        return None

    from kernel.drivers import k8s_verify                   # noqa: PLC0415
    from kernel.verifier import pod as verify_pod           # noqa: PLC0415
    from kernel.verifier import verify_state                # noqa: PLC0415

    state = await verify_state.current(conn, execution_id)
    if not state.get("env_key"):
        # Nothing has been built, or the build failed. `rebuild` will
        # take this row next tick; judging it now would check work
        # against an environment nobody has identified.
        return None
    try:
        host = await k8s_verify.address(driver, verify_pod.pod_name(
            execution_id))
    except Exception as exc:                                # noqa: BLE001
        logger.info("execution %s: no warm pod (%s: %s); using the tar "
                    "path", execution_id, type(exc).__name__, exc)
        return None

    raw = state.get("mounts_env")
    if isinstance(raw, str):
        import json                                         # noqa: PLC0415
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return {"host": host, "mounts_env": raw or {},
            "env_key": state["env_key"]}
