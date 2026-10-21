"""F2 — verify one completion claim. No model anywhere in this path.

    contract + criteria + a commit  ->  one Outcome per criterion

The Verifier does not decide whether the work is good. It decides
whether the criteria a human approved BEFORE the work started are
satisfied now. Every judgement in the loop below was made at approval
time by somebody else.

FOUR PROPERTIES MAKE THE ANSWER TRUSTWORTHY, and each is a defence
rather than a detail:

    a fresh container at the contract's DIGEST    not the executor's
    a different uid                               not the executor's
    the COMMITTED tree                            not the working copy
    the command from the CONTRACT                 not from the repo

Take away any one and a specific attack works: reuse the container and a
PATH shim survives; same uid and the executor pre-places an executable;
working copy and uncommitted edits count as work; command from the repo
and a rewritten Make target decides its own result.

E2 SPLIT THIS FILE rather than growing it. What left is named here so
the sequence is still readable in one place:

    kernel/verifier/stage.py          archive, setups, the recorded
                                      install replay, both surface
                                      hashes, prepare — everything that
                                      must be true before a criterion
                                      runs, in the order incidents put
                                      it in
    kernel/verifier/criterion_env.py  `environment_for`, which re-exports
                                      below because three callers name
                                      it here

E2'S BATCHING IS GONE, and the loop at the bottom is again the only path
a criterion takes. `batch.py` existed to stop N criteria costing N Jobs,
each unpacking the same tree into its own emptyDir. Phase E's
verification POD (plan 5.4, kernel/verifier/pod.py) subsumes that
whole argument: every criterion of an execution already runs in ONE warm
container against one seeded tree, so a wrapper that groups them into a
single Job groups something that is no longer separate.
"""

from __future__ import annotations

import logging
import time

from kernel.verifier.budget import timeout_for
from kernel.verifier.criterion_env import environment_for  # noqa: F401
from kernel.verifier.kinds import evaluator_for
from kernel.verifier.outcome import Outcome, unrunnable
from kernel.verifier.quorum import needs_another_run, runs_allowed, settle
from kernel.verifier.services import ServiceError, Services, declared
from kernel.verifier.stage import stage
from kernel.verifier.tree import held_out_files

logger = logging.getLogger(__name__)


async def verify_claim(driver, contract: dict, criteria: list[dict],
                       repo_path: str, commit: str, mounts=(),
                       timeout: float = 300.0,
                       verification_id: str | None = None,
                       env_deltas: list | None = None,
                       report: dict | None = None,
                       workspace_host: str = "",
                       execution_id: int | None = None,
                       warm: dict | None = None
                       ) -> list[tuple[dict, Outcome]]:
    """Run every criterion against `commit`. Returns (criterion, outcome).

    ONE ARCHIVE FOR ALL CRITERIA, not one per criterion. They are
    checking the same commit, and re-archiving would let two criteria
    disagree about what they were checking. Where each criterion runs is
    the DRIVER's answer now: on the verification pod they share one warm
    container, which is what retired E2's batching wrapper.

    `report` is filled in with what F6 needs to measure parity — the
    environment replay's own account of itself. An out-parameter
    rather than a wider return type, because the pair shape is what
    `refuses`, `summarise` and every test are written against; and
    NOT a module global, which would give the wrong answer the first
    time two verifications run at once.

    `workspace_host` is the address of the container holding the tree,
    and EMPTY MEANS "THIS FILESYSTEM" — the convention
    `ExecutionEnvelope.workspace_host` already uses. It is read only
    when the driver says the archive is taken elsewhere, so a caller
    that never heard of it keeps the path it always had.

    `execution_id` TRAVELS WITH IT and is required whenever it is:
    `podfs` authenticates per execution, so an address on its own opens
    nothing. Set both or neither.

    AND THIS IS WHY IT IS STATED SO FIRMLY. Three of the four callers —
    the adjudicator, the dry-run and the inert probe — passed NEITHER,
    while `archive_is_remote(driver)` is true for the Kubernetes driver.
    So on a pod every one of them reached `archive_in_pod("")`, which
    refuses with "no address for the pod holding this workspace", which
    becomes `TreeExportError`, which is `error` for every criterion. Not
    a wrong verdict — no verdict at all, on every story, from the only
    three callers that produce one in production. It read as the
    Verifier being broken rather than as two arguments nobody passed.
    """
    started = time.monotonic()

    # E19 -- THE VOLUME PATH, AND IT COMES FIRST BECAUSE IT SKIPS THE
    # ARCHIVE ENTIRELY. `warm` is `batch.warm_for`'s answer: a pod
    # already holding the committed tree on `volume.TREE` and the
    # environment on `volume.ENV`, both put there by the prepare Job
    # `rebuild.environments` fired. There is no tar to build, nothing
    # to ship through the object store, and one container instead of
    # one per criterion.
    #
    # THE SERVICES GRAPH IS NOT SKIPPED WITH IT. F2d's declared
    # services are about what a criterion may TALK to, which is a
    # different question from where its files are, so the batch runs
    # inside the same `Services` block below. A first draft of this
    # returned early here and silently dropped every declared service.
    #
    # None means the tar path, and `warm_for` returns None on ANY
    # doubt -- no claim substrate, no pod, no `env_key`. Judging
    # against a volume nobody has identified is a check PASSING THAT
    # SHOULD FAIL, which is the one direction 5.10 forbids.
    # THE WARM HOST TRAVELS ON THE REPORT, exactly as `_tree_tar` does
    # from `stage`. The assessor and the investigator run AFTER this
    # function and need to reach the tree; on the volume path there is
    # no archive to hand them, and without this they saw nothing and
    # silently did not run -- the second key absent from every
    # verification that used the pod.
    if warm and report is not None:
        report["_warm_host"] = warm.get("host")

    staged, early = (None, None) if warm else await stage(
        driver, contract, criteria, repo_path, commit, mounts=mounts,
        timeout=timeout, env_deltas=env_deltas, report=report,
        workspace_host=workspace_host, execution_id=execution_id)
    if early is not None:
        return early

    # F2d: the services this contract declares, fresh for this
    # verification and torn down whatever happens. ONE graph for all the
    # criteria — they are checking one commit, and a database per check
    # would cost minutes and let two criteria disagree about the schema.
    ident = verification_id or f"{commit[:12]}-{abs(hash(repo_path)) % 65536:04x}"

    results: list[tuple[dict, Outcome]] = []
    async with Services(driver, ident) as graph:
        try:
            await graph.up(declared(contract))
        except ServiceError as e:
            # A service that will not start means nothing was checked,
            # so nothing was found wrong. `error`, never `fail` — R1.
            logger.warning("verify: services for %s: %s", commit[:12], e)
            return [(c, unrunnable("a declared service did not come up",
                                   commit=commit, error=str(e)))
                    for c in criteria]

        # E19: one container for every criterion, in the pod that is
        # already warm.
        #
        # F3's QUORUM SURVIVES, AND IT COST A REWRITE. The first draft
        # ran the batch once and returned, with a comment explaining
        # that quorum was somebody else's item. That is a silent
        # behaviour change: `runs_allowed` exists because checks FLAKE,
        # and a contract that declares a quorum would have stopped
        # getting one the day the volume path switched on -- with
        # nothing failing to say so.
        #
        # RE-RUN ONLY WHAT HAS NOT PASSED, which is what F3 means and
        # what one-Job-per-criterion gave for free. Re-running the whole
        # batch would repeat checks that already passed, so a green
        # suite would cost its whole runtime again per round; returning
        # after one pass would drop the defence. The batch shrinks each
        # round instead, and on the happy path round two never happens.
        if warm is not None:
            from kernel.verifier import batch          # noqa: PLC0415
            allowed = runs_allowed(contract)
            history: dict = {id(c): [] for c in criteria}
            pending = list(criteria)
            rounds = 0
            while pending:
                rounds += 1
                for criterion, outcome in await batch.run(
                        warm["host"], contract, pending,
                        warm.get("mounts_env") or {}):
                    history[id(criterion)].append(outcome)
                pending = [c for c in pending
                           if needs_another_run(history[id(c)], allowed)]
            results = [(c, settle(history[id(c)])) for c in criteria]
            logger.info(
                "verify: %s criteria in the warm pod for %s in %.1fs, "
                "%s round(s) -> %s", len(results), commit[:12],
                time.monotonic() - started, rounds,
                ", ".join(o.passes for _, o in results),
                extra={"event": "verify.batch"})
            return results

        allowed = runs_allowed(contract)
        for criterion in criteria:
            evaluate = evaluator_for(criterion.get("kind"))
            # F3: run again only while it has NOT passed. A criterion
            # green first time costs exactly one container, so the
            # quorum is free on the happy path — a defence that triples
            # the cost of a passing suite is a defence somebody turns
            # off.
            runs: list[Outcome] = []
            while needs_another_run(runs, allowed):
                try:
                    c_env, c_prepend = environment_for(
                        (criterion.get("spec") or {}).get("run") or "",
                        staged.mounts_env)
                    # PER CRITERION, declarable. Every timeout here
                    # used to be a hardcoded 300s that no caller
                    # overrode, so a real suite timed out, spent the
                    # adjudicator's strikes, and told the executor to
                    # fix an environment that was not broken.
                    c_timeout = timeout_for(contract, criterion)
                    runs.append(await evaluate(
                        driver, contract, criterion, staged.tar,
                        timeout=c_timeout,
                        network=graph.network, sidecars=graph.sidecars,
                        deps_tar=staged.deps_tar,
                        extra_env=c_env, path_prepend=c_prepend))
                except Exception as e:                      # noqa: BLE001
                    # A criterion that blows up in the harness is
                    # `error`. Any other reading blames the work for our
                    # bug.
                    logger.exception("verify: criterion %s raised",
                                     criterion.get("id"))
                    runs.append(unrunnable(
                        f"the evaluator raised: {type(e).__name__}: {e}",
                        criterion_id=criterion.get("id"),
                        kind=criterion.get("kind")))
            results.append((criterion, settle(runs)))

    logger.info("verify: %s criteria against %s in %.1fs -> %s",
                len(results), commit[:12], time.monotonic() - started,
                ", ".join(o.passes for _, o in results))
    return results


def held_out_for(criteria: list[dict]) -> dict:
    """The held-out checks this claim will be verified against.

    Exported so a caller can assert the executor never had them. There
    is nothing to remove from the executor's workspace — they were never
    in the repository — which is what makes the defence one that cannot
    delete itself.
    """
    return held_out_files(criteria)


def refuses(results: list[tuple[dict, Outcome]]) -> bool:
    """Does this set of outcomes refuse the claim?

    R1: `fail` only. A `flake` or an `error` did not find the work wrong,
    and refusing on one permanently blocks re-claiming — so a rate limit
    or a container that would not start would kill a story that was
    actually finished.

    F3 replaces this with the quorum, which is the same rule applied to
    several runs of each criterion instead of one.
    """
    return any(outcome.refuses for _, outcome in results)


def summarise(results: list[tuple[dict, Outcome]]) -> dict:
    """Counts by value, for a log line and for the console."""
    counts: dict[str, int] = {}
    for _, outcome in results:
        counts[outcome.passes] = counts.get(outcome.passes, 0) + 1
    return counts
