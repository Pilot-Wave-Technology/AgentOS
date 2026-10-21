"""E4 and E13 — the environment is rebuilt when the MANIFEST changes, and
adjudication waits for the rebuild rather than racing it.

THE TRIGGER IS A CHANGE, NOT A FIRST SUCCESS. "Prepare when `env_setup`
succeeds" was the obvious reading and it is wrong: execution 207's second
generation wrote `.agentos/setup.sh` and called `env_setup` again, and a
once-only trigger verifies generation two against generation one's
`.venv`. So this is level-triggered on the hash -- whenever the committed
manifest hash differs from the one the volume is believed to hold, a
prepare Job fires, overlapped with the model still working (5.10).

AND THE HASH IS OF THE **COMMITTED** TREE, NEVER THE WORKING ONE. The
executor's `env_setup` reads the working tree. A model that edits
`requirements.txt`, runs `env_setup` and does not commit has an
environment the verifier will never reproduce, and the failure would
arrive as a criterion that cannot import a dependency the model watched
itself install. Every read here goes through `tree_state` and
`archive_committed_tree`, which are the committed halves.

TWO QUESTIONS, ASKED IN COST ORDER.

    the HEAD moved?        one `heads` call over podfs, bounded at 15s.
                           A manifest that has not been committed cannot
                           have moved the head, so an unchanged head is a
                           complete answer and buys nothing else.
    the MANIFEST moved?    a tar and `env_payload.key`, which is the
                           authority. An unchanged key after a moved head
                           -- somebody committed a source file -- records
                           the new head and runs no Job.

E13, AND IT IS THE HALF THAT PREVENTS A FALSE PASS. A model can commit
and claim in the same breath, so VERIFYING arrives while prepare is still
running. Verifying then would check the work against a `.venv` that is
halfway replaced: round two REMOVING a dependency would let a check PASS
THAT SHOULD FAIL (5.9, the dangerous direction). So the claim is SKIPPED,
with a `stall_reason` naming the hash being built, and taken on a later
tick. Every skip reports -- the S-phase rule -- because the alternative
to a loud skip is a run that sits in VERIFYING with nobody able to say
why.

AND THE POD COMES OFF THE DISK FIRST. The verification claim is
ReadWriteOncePod, so a prepare Job submitted while the warm pod holds it
never starts -- it is a Pending pod and a deadline, not an error anything
here could catch. `handover.detach` (reclaim's `reap_verification`, with
`keep_claim=True`) is that call and this module is the caller: the
manifest-hash trigger cannot be a sweep, because the hash comes from the
tree's own manifest bytes and only the thing about to verify knows what
is wanted.

WHAT THIS DOES NOT DO: block. Nothing here awaits a Job on the tick. A
process with a supervisor spawns the build and returns; the serial path
runs it inline because there is nowhere else to put it, and the substrate
that has no volumes refuses in microseconds (`_substrate_refusal`).
"""

from __future__ import annotations

import asyncio
import logging

from kernel.verifier import (env_payload, prepare_source, prepare_volume,
                             verify_state, volume)
from kernel.verifier import pod as verify_pod
from kernel.verifier.errored import prepare_failed
from kernel.verifier.handover import (attach, claim_capable, detach,
                                        on_volume, provision,
                                        warm_ready)
from kernel.verifier.kinds import TREE_PREFIX
from kernel.verifier.tree import pod_address, tree_state
from kernel.verifier.writer import load_contract
from kernel.workspace.mounts import mounts_for
from kernel.workspace.podfs_client import is_pod

logger = logging.getLogger(__name__)

# How often the OVERLAP path asks a RUNNING execution's pod for its head.
# The claim path is never held back by this -- see `verify_state.checked`
# -- so the only thing it bounds is how long a manifest change waits
# before its Job starts while the model is still working. A minute of
# that is invisible against a `pnpm install`; dialling every pod every
# four seconds is the shape of the credential-sweep wedge of 2026-09-17.
COOLDOWN_S = 60.0

# How many RUNNING executions may have their environment refreshed in one
# pass. The claim path is not counted against it: a run waiting on a
# verdict is somebody's story stopped, and the overlap is an optimisation.
BUILDS_PER_PASS = 2

# HOW MANY ROUNDS AN ENVIRONMENT MAY BE TRUSTED WITHOUT A FROM-SCRATCH
# REBUILD.
#
# The key is a hash of the image and the install inputs, so a matching
# key skips the install -- which is the whole warm-pod saving. But
# `.agentos/setup.sh` is arbitrary shell: it can read a file outside the
# hashed set, resolve a floating version, or depend on anything the
# bytes do not name. Widening the identity to all of `.agentos/` narrows
# that; nothing static closes it, because the input is a program.
#
# So the drift window is bounded by rounds instead. Five is a judgement:
# a story that verifies once never pays, a story on its sixth round has
# had four opportunities to change what its setup would install, and one
# install per five verifications is a cost proportional to exactly the
# runs where the risk accrued.
#
# GENERATION AND NOT WALL CLOCK: a story parked over a weekend changed
# nothing and should not pay for the calendar.
COLD_AFTER_GENERATIONS = 5


async def environments(conn, driver, goal_id: str | None, rows: list[dict],
                       *, acquire=None, supervisor=None) -> tuple:
    """`(rows still judgeable, one action per execution handled)`.

    Called by `adjudicate_pass` before it takes a verdict lock on
    anything, because the question "is this environment the one the
    manifest asks for?" is upstream of every other question about a
    claim.
    """
    # NO VOLUME, NOTHING TO KEEP FRESH. On Docker and E0 the environment
    # travels as a tar rebuilt inside every verification
    # (`kernel/verifier/prepare.py`), so there is no cache to invalidate
    # and no Job to wait for -- and a gate that skipped claims here would
    # wedge every story on the one substrate that cannot be unwedged by
    # waiting. `_substrate_refusal` makes the same call at the other end.
    if not is_pod(driver):
        return rows, []

    actions, blocked = [], set()
    for row in rows:
        act = await _claim_row(conn, driver, row, acquire, supervisor)
        if act is not None:
            blocked.add(row["id"])
            actions.append(act)
    actions += await _overlap(conn, driver, goal_id, blocked, acquire,
                              supervisor)
    return [r for r in rows if r["id"] not in blocked], actions


async def _hold(conn, eid: int, why: str):
    """Skip this claim, with the reason on the row. 7b's rule.

    ON KUBERNETES THE POD IS NOT OPTIONAL. Verification runs in the
    verification pod -- that is what it is for -- so a claim-capable
    substrate that cannot verify there WAITS rather than quietly
    verifying somewhere else. The tar path stays for Docker and E0,
    where it is the only thing possible, and stops being an invisible
    second implementation here.

    WHY SILENCE WAS THE REAL DEFECT. Executions 211, 217 and 218 all
    produced green verdicts through the tar path, months apart, and
    nothing anywhere said the designed path had not run. `executor_pass`
    already records this exact shape one layer down: a second
    implementation "drifted, in the way a second copy always does, and
    silently". A verdict from the fallback is now a row a person can
    see.

    AND IT IS A HOLD, NOT A REFUSAL. Nothing is failed and no verdict is
    written; the next tick tries again. A pod that never becomes ready
    therefore surfaces as a story visibly waiting with a reason, which
    is the outcome to want -- `preempted.py`'s "wrongly refusing is
    worse than wrongly not refusing" is about verdicts, and this writes
    none.
    """
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, "
            "stalled_at = COALESCE(stalled_at, now()) WHERE id = $1 "
            "AND stall_reason IS DISTINCT FROM $2", eid, why)
    except Exception:                                       # noqa: BLE001
        logger.warning("could not record why %s is waiting", eid)
    return {"execution_id": eid, "action": "awaiting_verification_pod",
            "detail": why}


def cold_due(state: dict, generation) -> bool:
    """Is this environment old enough to be rebuilt from scratch?

    NULL IS NOT DUE, and the first draft had this backwards. I argued
    that an environment nobody dated should be rebuilt, by analogy with
    `volume.stale` treating an unknown STAMP as stale. The analogy does
    not hold: an unknown stamp means we cannot say WHAT the environment
    is, and that is a correctness question. An unknown generation means
    we cannot say HOW OLD it is, while `env_key` still says exactly what
    it is -- which is a staleness question, and a much weaker one.

    Treating it as due forced a rebuild for every execution in flight at
    the moment this shipped, and made "an unchanged manifest costs no
    Job" false for any state predating the column -- 5.10's second rule,
    which eight tests hold. They failed, correctly, and that is how the
    direction was found to be wrong.

    The window closes on its own: any manifest change rebuilds and
    stamps, so an undated environment is dated the first time it is
    genuinely rebuilt, and cold rebuilds begin from there.
    """
    try:
        now = int(generation or 0)
    except (TypeError, ValueError):
        return False
    built = state.get("built_at_generation")
    if built is None:
        return False
    return (now - int(built)) >= COLD_AFTER_GENERATIONS


async def _claim_row(conn, driver, row: dict, acquire, supervisor):
    """A run waiting on a verdict: judge it, or say why not yet."""
    eid = row["id"]
    state = await verify_state.current(conn, eid)
    wip = verify_state.building(state)
    if wip:
        return await _waiting(conn, eid, wip)
    fingerprint, commit = await _committed(conn, driver, eid,
                                           row.get("workspace_path"))
    if fingerprint is None:
        # No committed tree, or a pod that would not answer. NOT this
        # module's refusal to make: `adjudicate_one` has said "no
        # committed tree to verify" since F2b and says it with the
        # evidence in hand.
        return None

    # THE MANIFEST FIRST, THEN THE VOLUME -- the same order `_overlap`
    # uses, and this function did NOT until it was measured. Provisioning
    # above `_committed` gave a claim and a pod to every run that reaches
    # VERIFYING, including the ones with nothing to install: a docs
    # story, an errand, any repository that declares no dependencies.
    # Each is a 10GiB ReadWriteOncePod disk against W1's 120-claim
    # ceiling, created for an environment no prepare Job will ever
    # build -- and the run then took the tar path anyway, because
    # `warm_for` needs an `env_key` that only a prepare writes.
    if not await on_volume(driver, eid, state):
        # E19: THE CLAIM DOES NOT EXIST YET, SO MAKE IT. This used to
        # `return None` unconditionally, which read as "nothing to be
        # stale about, so judge it" -- true, and it also meant no
        # execution ever acquired a volume, because nothing else
        # created one. See `handover.provision`.
        #
        # THE OVERLAP SHOULD HAVE GOT HERE FIRST. By 5.4 step 3 the
        # environment is built while the model is still working, so
        # reaching this line means the run claimed done before its
        # manifest settled -- or this is its first pass after the
        # feature landed. Either way one prepare is paid here, once,
        # and the row is held out of this pass while it runs.
        #
        # Provisioning that FAILS falls through to exactly the old
        # behaviour: judged on the tar path, nothing worse than today.
        if not await provision(conn, driver, eid):
            if claim_capable(driver):
                return await _hold(
                    conn, eid,
                    "waiting for a verification pod: this run's claim "
                    "could not be provisioned. On Kubernetes the checks "
                    "run in the verification pod, so the run waits here "
                    "rather than verifying through the tar path, which "
                    "would be a verdict from a path nobody is watching.")
            # Docker and E0 have no claims. The tar is not a fallback
            # there; it is the only thing that was ever possible.
            return None


    # THE FINGERPRINT MOVED, OR THE ENVIRONMENT IS SIMPLY OLD. The second
    # is `cold_due`: a key that still matches is not proof the volume
    # holds what the manifest would build, only that the bytes the hash
    # covers have not changed. See COLD_AFTER_GENERATIONS.
    if fingerprint != state["env_commit"] or cold_due(state,
                                                      row.get("generation")):
        started = await _start(conn, driver, row, commit, fingerprint,
                               acquire, supervisor)
        # A refresh that answered inline and found nothing to do leaves
        # the claim judgeable in the SAME tick. Only a build in flight
        # holds it back.
        return started or await _re_read(conn, eid)

    if state["build_error"]:
        # 5.8 row 5. A prepare that failed on THIS commit is a fact about
        # the work -- a manifest the contract's image cannot install --
        # so it becomes `error` on every criterion and reaches the
        # executor through the ordinary undecided path. Never `fail`:
        # nothing was checked, so nothing was found wrong.
        return await prepare_failed(conn, row, state["build_error"])

    # THE ENVIRONMENT IS BUILT; IS THE POD ANSWERING YET? On a
    # claim-capable substrate this is the last gate, and it is the one
    # execution 218 walked straight through: `attach` had recreated the
    # pod seconds earlier, it had no address, `warm_for` refused it, and
    # the tar path ran against a volume that was ready and unused.
    #
    # `attach` now waits, so reaching this line at all should be rare --
    # a preempted pod, a node that went away. Either way the answer is
    # to wait for it, not to verify somewhere else.
    if claim_capable(driver) and not await warm_ready(driver, eid):
        return await _hold(
            conn, eid,
            "waiting for the verification pod to answer: the environment "
            "is built and the pod is coming up. Verifying through the tar "
            "path instead would produce a verdict from a path this "
            "deployment does not use.")
    return None


async def _re_read(conn, eid: int):
    """Did the inline refresh leave a build running? Re-read and say so."""
    wip = verify_state.building(await verify_state.current(conn, eid))
    return await _waiting(conn, eid, wip) if wip else None


async def _overlap(conn, driver, goal_id, blocked: set, acquire,
                   supervisor) -> list:
    """5.10's overlap: prepare while the model is still working.

    This is what makes the common case cost nothing. A story whose
    manifest settles in generation one has its environment built during
    that generation, so the claim arrives to a warm volume and the ~30s
    RWOP detach/attach has already been paid where nobody was waiting.
    """
    # NOTHING TO OVERLAP WITH IF NOTHING WILL VERIFY. This pass exists
    # to build the environment while the model still works, so the ~30s
    # RWOP handover costs nobody anything. With verification switched
    # off no run reaches VERIFYING, so every claim and pod it provisioned
    # would be a 10GiB disk and a container created for a verification
    # that never comes -- one per story, against the 120-claim ceiling.
    from kernel.conductor.verification_switch import (          # noqa: PLC0415
        enabled as verification_enabled)

    if not await verification_enabled(conn):
        return []

    rows = [dict(r) for r in await conn.fetch(
        "SELECT id, generation, workspace_path, contract_id FROM executions "
        "WHERE state = 'RUNNING' AND container_id IS NOT NULL "
        "AND ($1::text IS NULL OR goal_id = $1) ORDER BY id LIMIT 50",
        goal_id)]
    actions, started = [], 0
    for row in rows:
        if started >= BUILDS_PER_PASS:
            break
        eid = row["id"]
        if eid in blocked:
            continue
        state = await verify_state.current(conn, eid)
        if verify_state.building(state):
            continue
        ago = state["checked_ago"]
        if ago is not None and float(ago) < COOLDOWN_S:
            continue
        # THE MANIFEST FIRST, THEN THE VOLUME, and the order is the
        # point. Provisioning before knowing whether there is anything
        # to install would put a 10Gi RWOP claim on every RUNNING
        # execution in the fleet, against the 120-PVC ceiling W1
        # records -- and most of them would never build anything.
        fingerprint, commit = await _committed(conn, driver, eid,
                                               row.get("workspace_path"))
        if fingerprint is None:
            # No committed manifest. STAMP THE COOLDOWN ANYWAY, or this
            # asks the pod again every four seconds for the life of the
            # run: `_committed` is a podfs round trip and this loop
            # takes fifty rows a pass. Nothing was built, so `env_key`
            # stays NULL and the run is still judged through the tar.
            await verify_state.checked(conn, eid)
            continue
        if not await on_volume(driver, eid, state):
            # E19, and THIS is where the claim should normally be born:
            # during the work, where the ~30s RWOP detach/attach costs
            # nobody anything (5.5 E4). A failure here is not fatal --
            # the run simply verifies through the tar path as it does
            # today.
            if not await provision(conn, driver, eid):
                continue
        if fingerprint == state["env_commit"]:
            await verify_state.checked(conn, eid)
            continue
        act = await _start(conn, driver, row, commit, fingerprint,
                           acquire, supervisor)
        if act is not None:
            actions.append(act)
            started += 1
    return actions


async def _committed(conn, driver, eid: int, repo: str | None):
    """`(fingerprint, commit)` for the committed workspace, or (None, None).

    TWO VALUES BECAUSE THEY ANSWER TWO QUESTIONS, and conflating them was
    a bug this comment is the fix for.

      fingerprint  "has anything been committed since we last looked?" --
                   every git mount's head, together. E7f: the digest is a
                   SET, and one sha names a fraction of a multi-repo
                   workspace, so a manifest change in the second
                   repository would hide behind an unchanged first. It is
                   compared, stored, and never handed to git.
      commit       a git commit-ish, for `stage._archive` and for the
                   prepare Job's own fetch. The root's head, or the first
                   head there is -- the same fallback `adjudicate_one`
                   applies when the root is not itself a repository.
    """
    if not repo:
        return None, None
    mounts = await mounts_for(conn, eid)
    digest, head = await tree_state(conn, eid, repo, mounts)
    heads = [(at, sha) for at, sha in digest if sha]
    if not heads and head:
        heads = [("", head)]
    if not heads:
        return None, None
    commit = head or next(sha for _, sha in heads)
    return ",".join(f"{at}@{sha}" for at, sha in heads), commit


async def _start(conn, driver, row: dict, commit: str, fingerprint: str,
                 acquire, supervisor):
    """Refresh this execution's environment, off the tick where possible."""
    eid = row["id"]
    if supervisor is not None and acquire is not None:
        from kernel.verifier.adjudicator import (                # noqa: PLC0415
            verify_concurrency)

        if supervisor.free_slots(verify_concurrency()) <= 0:
            return None

        async def _own(row=row, commit=commit, fingerprint=fingerprint):
            # Its own connection, for the reason the adjudication path
            # gives: this holds one for the whole prepare -- a Job,
            # minutes -- and borrowing the sweep's would put every other
            # pass behind an install.
            async with acquire() as own:
                return await _refresh(own, driver, row, commit,
                                      fingerprint)

        if not supervisor.spawn(eid, _own()):
            return None
        return {"execution_id": eid, "action": "preparing_environment",
                "detail": f"the committed tree moved to {commit[:12]}; "
                          f"building the verification environment"}

    # SERIAL, and it awaits. There is nowhere else to put the work, and
    # which path a process takes depends on whether it was given a
    # supervisor rather than on how slow a prepare is. Both production
    # callers supply one (`kernel/worker/loop.py`,
    # `kernel/verifier/service.py`).
    return await _refresh(conn, driver, row, commit, fingerprint)


async def _refresh(conn, driver, row: dict, commit: str, fingerprint: str):
    """Make the volume hold the environment `commit`'s manifests declare.

    THREE AUTHORITIES, EACH CHEAPER THAN THE NEXT IS WRONG. This compares
    `env_payload.key` against the row; `build_on_volume` compares it
    again against `stamped`; and the prepare SCRIPT re-asks the volume's
    own stamp before installing. The row can be stale -- it is a cache --
    and the design makes that cost one pod rather than one wrong answer.
    """
    eid = row["id"]
    # CLAIMED BEFORE ANYTHING IS READ, and that order is E13's exclusion
    # rather than an optimisation: the tick four seconds from now must see
    # that a rebuild is under way, or it judges the claim against the very
    # environment being replaced. The manifest hash is not known until the
    # tar has been read, so the claim starts labelled with the commit and
    # `now_building` relabels it below.
    if not await verify_state.claim_build(conn, eid, "tree " + commit[:12]):
        return {"execution_id": eid, "action": "preparing_elsewhere",
                "detail": "another process is already building this "
                          "execution's verification environment"}
    try:
        contract, _ = await load_contract(conn, eid)
        image = verify_pod.image_for(contract)
        mounts = await mounts_for(conn, eid)
        ats = [m.at for m in mounts] or [""]
        tar = await _tree(driver, conn, eid, row.get("workspace_path"),
                          commit, mounts)
        key = await asyncio.to_thread(_key_of, image, tar, ats)
    except Exception as exc:                                # noqa: BLE001
        # NOT `build_failed`. A pod that would not answer and a tree that
        # would not archive say nothing about the manifest, and recording
        # them as a prepare failure would send the executor to fix a
        # `requirements.txt` that is fine (5.8 rows 2 and 5 are different
        # rows for this reason).
        logger.warning("execution %s: could not read the committed "
                       "manifest: %s", eid, exc, exc_info=True)
        await verify_state.released(conn, eid)
        return {"execution_id": eid, "action": "manifest_unreadable",
                "detail": f"{type(exc).__name__}: {exc}"}

    state = await verify_state.current(conn, eid)
    cold = cold_due(state, row.get("generation"))
    if not volume.stale(state["env_key"], key) and not cold:
        # 5.10's second rule: an unchanged manifest costs nothing. A
        # model calling `env_setup` defensively five times does not churn
        # five pods, and a source-only commit moves the head without
        # moving the key.
        #
        # `cold` is the exception, and it is the whole point of the cold
        # rebuild: an unchanged KEY is not proof of an unchanged
        # ENVIRONMENT, only that the bytes the hash covers are the same.
        # Without this the skip above would swallow every cold rebuild
        # before a Job was ever submitted.
        await verify_state.built(conn, eid, key, fingerprint)
        return None
    await verify_state.now_building(conn, eid, key)

    try:
        # THE DETACH IS PART OF THE JOB, not a tidy-up after it. RWOP:
        # the warm pod holds the disk and the API server refuses the
        # second attacher, so a prepare submitted while the pod is up
        # never starts.
        await detach(driver, eid)
        # WHERE THE JOB PULLS ITS TREE FROM (E15). The prepare Job is
        # networked and fetches the committed tree itself rather than
        # carrying the manifests in its command, so it needs the same
        # host `_tree` above already resolved. `from_mounts` derives the
        # per-mount commits with the SAME rule `stage._archive` uses --
        # one spelling, or the environment is prepared from one tree and
        # the work checked against another.
        report = await prepare_volume.build_on_volume(
            driver, image, tar, ats, claim=verify_pod.claim_name(eid),
            source=prepare_source.from_mounts(
                await pod_address(conn, eid, driver),
                row.get("workspace_path") or "", mounts, commit),
            # `None` ON A COLD REBUILD, because `prepare_script`'s step
            # 1 re-asks the VOLUME's own stamp and skips the install
            # when it matches -- correct for a stale database row, and
            # exactly what a cold rebuild must not hit. `build_on_volume`
            # documents None as "nothing is known", which `volume.stale`
            # already treats as stale: the safe direction, reused rather
            # than a second flag threaded down two layers.
            stamped=None if cold else state["env_key"], execution_id=eid)
    except Exception as exc:                                # noqa: BLE001
        logger.exception("execution %s: prepare raised", eid)
        await verify_state.released(conn, eid)
        return {"execution_id": eid, "action": "prepare_crashed",
                "detail": f"{type(exc).__name__}: {exc}"}

    if report.get("ok"):
        # E19: `mounts_env` TRAVELS WITH THE KEY. `build_on_volume`
        # derived it while building -- it knows where it put
        # `.venv/bin` -- and `verify_claim` runs in another pass and
        # often another process, where the only other ways to learn
        # it are re-reading the tree (5.5 exists to stop that) or
        # asking the pod (which holds no database credential).
        # THE GENERATION STAMPS THE COLD CLOCK, and only here -- this is
        # the path where an install actually ran. The early return above
        # records a cache hit and deliberately passes none, or a story
        # that never rebuilds would keep resetting the counter and the
        # cold rebuild would never come due.
        await verify_state.built(conn, eid, report["key"], fingerprint,
                                 report.get("mounts_env"),
                                 generation=row.get("generation"))
        await attach(driver, eid, contract)
        logger.info("execution %s: environment %s ready (%s)", eid,
                    report["key12"], report.get("cache"),
                    extra={"event": "verify.prepare"})
        return None
    await verify_state.build_failed(conn, eid, fingerprint,
                                    report.get("output") or "the prepare "
                                    "Job failed without saying why")
    logger.warning("execution %s: prepare %s failed", eid, report["key12"])
    return {"execution_id": eid, "action": "prepare_failed",
            "detail": (report.get("output") or "")[-200:]}


def _key_of(image: str, tar: bytes, ats) -> str:
    """The committed manifest hash. OFF THE LOOP, and this is not
    theoretical: `collect` walks every member of a tar measured at 45MB on
    execution 210, in pure Python, on a container capped at 1 CPU. A
    spawned task is not a thread -- `test_the_verifier_never_tars_on_the
    _event_loop` exists because verification froze both conductors for
    154s and 184s doing exactly this."""
    return env_payload.key(
        image, env_payload.collect(tar, ats, prefix=TREE_PREFIX))


async def _tree(driver, conn, eid: int, repo: str, commit: str, mounts):
    """The committed tree as a tar, from wherever the tree physically is.

    IMPORTED FROM `stage`, NOT RESPELLED. Which of three ways a tree is
    taken -- out of a pod, across several mounts, or off this filesystem
    -- is I4's decision and each branch has an incident behind it; a
    second copy here would be a fourth way for the prepare Job and the
    criteria to disagree about what they are looking at. The leading
    underscore says `stage` owns it, and this reads it. `prepare_volume`
    reaches into `k8s_run` and `kubernetes._api` the same way, and 5.16
    item 3 already prices hoisting both.

    No `inject`: the held-out checks are the criteria's, and a prepare
    Job has no business seeing them.
    """
    from kernel.verifier.stage import _archive                   # noqa: PLC0415

    host = await pod_address(conn, eid, driver)
    return await _archive(driver, repo, commit, mounts=mounts, injected={},
                          workspace_host=host, execution_id=eid)


async def _waiting(conn, eid: int, key: str):
    """Skip this claim, loudly, naming the hash being built.

    ITS OWN COPY OF THE stall_reason WRITE, and deliberately: the
    adjudicator's `stranded_verifying` keeps its own because
    `test_no_loop_dials_something_that_is_gone` reads that file BY PATH
    and a guard that passes because the code moved is a guard that passes
    vacuously.
    """
    why = (f"waiting for the verification environment: the committed "
           f"manifest changed and prepare {key[:16]} is still running. "
           f"Verifying now would check this work against a half-replaced "
           f"environment.")
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, "
            "stalled_at = COALESCE(stalled_at, now()) WHERE id = $1 "
            "AND stall_reason IS DISTINCT FROM $2", eid, why)
    except Exception:                                       # noqa: BLE001
        logger.warning("could not record why %s is waiting", eid,
                       exc_info=True)
    return {"execution_id": eid, "action": "awaiting_environment",
            "detail": why}
