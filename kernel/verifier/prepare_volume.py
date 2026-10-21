"""Section 5's prepare: a Job writes the environment onto the PVC.

REPLACES THE CAPTURE PATH. `prepare.build_prepared` runs a setup in a
container, tars what it created, pushes the tar through the artifact
store and splices it back into a tree. FOUR of the five faults of
2026-09-17 lived in those steps and none of them was about verification
(volume.py names them). Here the install writes `volume.ENV` directly
and the only thing that leaves the Job is an exit code.

AND THE JOB PULLS ITS OWN TREE (E15, plan 5.15). It used to carry the
manifests as a base64 tar.gz in its own command, capped at 256KB,
because prepare fires at step 3 and the tree is not seeded until step 6.
That was a REGRESSION: `.agentos/setup.sh` received only itself, so a
script reading a sibling -- or a `requirements.txt` with `-e .` or
`-r base.txt` -- failed, and execution 207 wrote a `setup.sh`. The
resolution is that the prepare Job is NETWORKED and always was: zero
egress is a property of the CRITERIA pod, and prepare runs no criterion.
So an initContainer streams the committed tree onto `volume.TREE` and
the setup runs against a real one. `source` below is where it comes
from; the payload and the cap are gone.

WHAT THE CACHE IS KEYED ON, exactly: the CONTRACT's image plus every
committed install input under every mount -- `.agentos/setup.sh`,
`.agentos/env`, and each `setups.REGISTRY` manifest -- hashed by
`env_payload.key`. The COMMITTED tree, never the working one (5.10): the
executor's `env_setup` reads the working tree, so a model that edits
`requirements.txt`, runs `env_setup` and does not commit has an
environment the verifier will never reproduce. `tree_tar` comes from
`archive_committed_tree`, which is what makes that true here.

IDEMPOTENT, AND TWICE OVER. `volume.stale` against the stamp the caller
last recorded skips the Job entirely -- a model calling `env_setup`
defensively five times does not churn five pods. And the SCRIPT re-asks
the volume, because the caller's copy can be stale and volume.py is
explicit that the volume is the one holding the bytes.

EVERY FAILURE IS `error`, NEVER `fail`. `report["ok"]` is false and the
caller turns that into `error` for every criterion, for the reason
`build_deps` gives and 5.8 row 5 restates: a check that could not be
given its dependencies did not run, so it found nothing wrong.
"""

from __future__ import annotations

import logging

from kernel.spec import ExecutionSpec
from kernel.verifier import env_payload, prepare_script, setups, volume
from kernel.verifier.environment import install_env
from kernel.verifier.kinds import VERIFIER_UID
from kernel.verifier.handover import claim_capable
from kernel.verifier.mount_env import parse_env
from kernel.verifier.prepare_source import describe
from kernel.verifier.setups import ENV_FILE

logger = logging.getLogger(__name__)

# An install is not a criterion: it fetches from package indexes over a
# proxy, and a cold `pnpm install` on a workspace is minutes rather than
# seconds. Overlapped with the model's own work (5.5), so this budget is
# spent where nobody is waiting. It now also covers the tree fetch,
# which is bounded by the same disk the install writes to.
# KEPT AS A NAME ONLY, for callers that pass nothing and know no mount
# count. The real budget is `setups.budget_for(len(discovered))`, chosen
# below once discovery has said how many installs this Job will run --
# see the note in `setups.py`. This used to BE the budget, at 900s
# against the executor's 1860 for the same committed command.
DEFAULT_TIMEOUT_S = setups.budget_for(1)

# The tar path's number, for the same reason it chose it: resolving a
# large dependency tree is the memory-hungriest thing the verifier runs.
MEMORY_MB = 2048


def _substrate_refusal(driver) -> str:
    """Why this driver cannot do it at all, or "" when it can.

    ASKED, NOT TRIED. `build_deps` makes the same call about captures:
    "this substrate has no volumes" and "the Job failed" are different
    facts, and surfacing the first as a traceback from the middle of a
    verification is how an unsupported substrate reads as a broken one.
    E17 moved the question onto the driver -- `CLAIM_JOB_MODEL` -- so it
    is declared beside every other thing a substrate can and cannot do
    rather than inferred here from `WORKSPACE_MODEL`, which is about who
    may WRITE the workspace and is a different question.
    """
    from kernel.verifier.handover import claim_capable        # noqa: PLC0415

    if claim_capable(driver):
        return ""
    return ("this substrate has no persistent claim to prepare an "
            "environment onto, so there is nothing for a prepare Job to "
            "write to. The tar path in kernel/verifier/prepare.py is what "
            "runs here.")


def _report(key: str, cache: str, ok: bool, **extra) -> dict:
    return {"mode": "volume", "ok": ok, "key": key, "key12": key[:12],
            "cache": cache, **extra}


def _mounts_env(found: dict, discovered: list) -> dict:
    """{at: {"prepend": [...], "env": {...}}} plus the dropped keys.

    DERIVED FROM THE DECLARED LAYOUT. The tar path had to scan a
    captured archive to learn whether `.venv/bin` was in it; the volume
    path knows where it put things, so `prepare_script.bin_paths` answers
    without reading anything. `.agentos/env` is parsed from the bytes
    `collect` already holds rather than from the tree a second time --
    5.5's rule is that nothing here re-parses a whole archive.
    """
    by_at: dict = {}
    for s in discovered:
        by_at.setdefault(s["at"], []).extend(s["bins"])
    out, dropped_all = {}, []
    for at, bins in by_at.items():
        prepend = prepare_script.bin_paths(at, dict.fromkeys(bins))
        blob = found.get((at, ENV_FILE))
        env, dropped = {}, []
        if blob is not None:
            base = f"{volume.TREE}/{at}" if at else volume.TREE
            more, env, dropped = parse_env(
                blob.decode("utf-8", "replace"), base)
            prepend += more
        out[at] = {"prepend": prepend, "env": env}
        dropped_all += dropped
    return {"mounts_env": out, "dropped_env_keys": dropped_all}


def _tree_source(source: dict, execution_id: int) -> tuple:
    """(the inert description, the read-only token) for the Job.

    THE TOKEN IS THE READ-ONLY ONE AND DERIVED HERE. Plan 5.15a: it goes
    to the things that run no check -- this Job and the verifier
    Deployment -- and never to the criteria pod, because a criterion
    runs model-authored test code and any reach the pod has, that code
    has. Derived from the FULL token rather than the signing key, so the
    worker verifies it with no second variable to keep in step.
    """
    from kernel.workspace.podfs_auth import (                # noqa: PLC0415
        derive_read_only)

    # THE UID THE CRITERIA RUN AS, when the caller names none. The
    # archive's ownership is re-written by `git archive`'s consumer and
    # the fetch unpacks as a non-root user that cannot chown anyway, so
    # this is belt-and-braces -- but 0 would be a claim that root owns
    # the tree, and volume.py's whole contract is that one uid writes
    # this disk and the same uid reads it.
    described = describe(source["host"], source["mounts"],
                         source.get("uid") or VERIFIER_UID)
    return described, derive_read_only(execution_id)


async def build_on_volume(driver, image: str, tree_tar, ats, *,
                          claim: str, source: dict,
                          execution_id: int,
                          stamped: str | None = None,
                          timeout: float | None = None) -> dict:
    """Make `volume.ENV` on `claim` be the environment `tree_tar` declares.

    `stamped` is what the caller last recorded for this volume; pass None
    when nothing is known, which `volume.stale` treats as stale -- the
    safe direction, because a volume whose environment we cannot identify
    gets rebuilt and the alternative is a check passing that should fail.

    `claim` is a PARAMETER and not derived here. The claim is provisioned
    with the verification pod (E7) and naming it in two modules is how
    two modules come to disagree; the provisioner passes the name it made.

    `source` is `{"host", "uid", "mounts"}` -- where the execution's
    workspace surface answers and which mounts to pull, as
    `prepare_source.describe` takes them. REQUIRED, because a prepare
    Job with no tree is the regression 5.15 removed.

    `execution_id` is REQUIRED, and plan 13.2 is why: the label is what
    Cloud Logging indexes and what stops `orphan_sweep` destroying a
    live install. There is no `None` branch anywhere below it.
    """
    discovered = setups.from_tar(tree_tar, ats)
    found = env_payload.collect(tree_tar, ats)
    key = env_payload.key(image, found)
    env_report = _mounts_env(found, discovered)
    warnings = [s["warn"] for s in discovered if s.get("warn")]

    if not discovered and not claim_capable(driver):
        # NO MANIFEST, NO ENVIRONMENT, AND NO JOB -- on a substrate with
        # no claim to put a tree on. Nothing would be written, so a pod
        # start would buy a stamp on an empty directory. `ok` is true
        # because that is the honest answer: this tree declares no
        # dependencies.
        return _report(key, "none", True, mounts=[], warnings=[],
                       **env_report)

    if not discovered:
        # AND ON KUBERNETES IT STILL RUNS, because the Job is no longer
        # only the install. `prepare_fetch` (E15) made the init
        # container SEED `volume.TREE` -- so a Job with no install
        # commands still buys the one thing a no-manifest run needs to
        # be verified in its pod: the committed tree on the volume, and
        # a stamp naming the empty environment that goes with it.
        #
        # THE COMMENT ABOVE WAS WRITTEN BEFORE THAT. It was right that
        # nothing would be written when the Job's only job was
        # installing; it stopped being right when the Job grew a second
        # one, and nobody revisited it. The cost was that a docs story,
        # an errand, or any repository declaring no dependencies could
        # never reach `warm_for` -- no Job, no `env_key`, no warm pod --
        # so the one class of work with nothing to install was the class
        # that always verified through the tar path.
        logger.info("execution %s: no manifest; seeding the tree onto the "
                    "volume so its checks can run in the pod",
                    execution_id, extra={"event": "verify.prepare"})

    if not volume.stale(stamped, key):
        return _report(key, "hit", True, warnings=warnings,
                       mounts=[s["at"] or "." for s in discovered],
                       **env_report)

    refusal = _substrate_refusal(driver) or _source_refusal(source)
    if refusal:
        return _report(key, "miss", False, output=refusal,
                       warnings=warnings, **env_report)

    spec = ExecutionSpec(
        # THE CONTRACT's IMAGE, never the executor's -- defence 1, and
        # 5.12 names it first: a surviving PATH shim makes F4 meaningless.
        image=image,
        command=["sh", "-c", prepare_script.script(
            key, [(s["at"], s["command"]) for s in discovered])],
        # NETWORKED, and told WHERE the network is. `network_mode` is a
        # Docker word and says nothing in a default-deny namespace; an
        # install that does not use the proxy does not fail, it HANGS.
        # `install_env` states which of the proxy's two ports this gets,
        # and why a writable HOME is not optional.
        network_mode="bridge",
        env=install_env(driver),
        memory_mb=MEMORY_MB,
        # WHOSE RUN THIS IS -- the label `orphan_sweep` matches on. A pod
        # that cannot name its execution is indistinguishable from a leak,
        # and one was destroyed seven seconds into an install for it.
        execution_id=execution_id,
    )
    return await _submit(driver, spec, key, claim, source, execution_id,
                         timeout or setups.budget_for(len(discovered)),
                         discovered, warnings, env_report)


def _source_refusal(source: dict) -> str:
    """A prepare Job with nowhere to pull from is refused, not started."""
    if source and source.get("host") and source.get("mounts"):
        return ""
    return ("this prepare Job was given no workspace to pull the "
            "committed tree from, so the setup would run against an "
            "empty directory and report a repository that committed no "
            "manifests. See plan 5.15: prepare is networked and fetches "
            "its own tree.")


async def _submit(driver, spec, key, claim, source, execution_id, timeout,
                  discovered, warnings, env_report) -> dict:
    """Through the driver seam (E17), and turn the ending into a report."""
    from kernel.workspace.podfs_auth import TokenMissing      # noqa: PLC0415

    try:
        described, token = _tree_source(source, execution_id)
    except TokenMissing as exc:
        # A CONFIGURATION FACT, REPORTED AS ONE. Without the signing key
        # no read-only token can be derived, so the fetch would be
        # refused 401 by the worker and the reader would be told the
        # workspace was unreachable -- which sends somebody to look at a
        # pod that is perfectly healthy.
        return _report(key, "miss", False, output=str(exc),
                       warnings=warnings, **env_report)

    result = await driver.run_job_on_claim(
        spec, claim=claim, timeout_s=timeout, tree_source=described,
        tree_token=token)
    tail = (result.get("output") or "")[-1500:]
    # A run the SUBSTRATE ended -- a preempted node, an image that would
    # not pull -- says nothing about the manifest, and a report carrying
    # only the install's output would send somebody to debug a package
    # name that was never reached.
    if result.get("infra_error"):
        tail = f"[{result['infra_error']}]\n{tail}"
    fetched = (result.get("extra") or {}).get("tree-fetch") or ""
    if fetched and not result.get("exit_code") == 0:
        # THE FETCH'S OWN WORDS, on the failure path. "The tree never
        # arrived" and "the install failed" are different facts and only
        # the init container's log tells them apart; the Job is deleted
        # in `run_job`'s `finally`, so this is the last chance to keep it.
        tail = f"{fetched[-500:]}\n{tail}"
    ok = result.get("exit_code") == 0
    if ok:
        logger.info("prepare %s wrote %d mount(s) to %s", key[:12],
                    len({s["at"] for s in discovered}), claim)
    else:
        logger.warning("prepare %s failed on %s: %s", key[:12], claim,
                       tail[-300:])
    return _report(key, "miss", ok, output=tail, warnings=warnings,
                   timed_out=result.get("timed_out", False),
                   mounts=[s["at"] or "." for s in discovered],
                   **env_report)
