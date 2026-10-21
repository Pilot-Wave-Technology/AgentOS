"""H0w — the one place that turns a request into a finished run.

`worker_tick` decides what happens after a leg. This assembles the parts
around it: create the execution, admit it, dispatch a sandbox, acquire the
workspace, and then tick until the run reaches a terminal state or parks.

WHY IT EXISTS AS A FUNCTION RATHER THAN A SCRIPT. Two callers need exactly
this: the live smoke test, and the console (H0v) when a human presses
"start". A script would mean the console reimplements it, and then the
thing a person watches is not the thing the test proves.

WHAT IT DELIBERATELY DOES NOT DO. It does not adjudicate. A story that
proposes completion lands in VERIFYING and this returns — Stage F answers
that, and inventing an answer here would put a second verdict source in
the system that the whole refactor exists to remove.
"""

from __future__ import annotations

import logging
import os

from kernel.conductor.dispatch import dispatch_admitted
from kernel.conductor.ports import allocate_bridge_port
from kernel.conductor.promote import (SandboxNotReady, promote_one,
                                      promote_ready)
from kernel.conductor.sandbox_spec import (WORKSPACE_MOUNT,
                                           build_sandbox_spec,
                                           spec_for_dispatch)
from kernel.profiles.registry import resolve
from kernel.workspace.podfs_client import is_pod
from kernel.worker.loop import claimable, run_one_leg
from kernel.workspace.bindings import driver_for
from kernel.workspace.composite import CompositeDriver
from kernel.workspace.driver import WorkspaceSpec
from kernel.workspace.driver import WorkspaceError
from kernel.workspace.mounts import mounts_for
from kernel.workspace.pod_sources import (local_directory_mounts,
                                          local_source_refusal)

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED"}
WAITING_STATES = {"PARKED_HOT", "PARKED_COLD", "VERIFYING"}


async def create_execution(conn, kind: str, domain: str, task: str,
                           goal_id: str, terminal_command: str | None = None,
                           workspace_source: str | None = None,
                           story_id: str | None = None,
                           driving_thread_id: str | None = None) -> int:
    """Record a request for work. Returns the execution id.

    Resolving the profile here means an unknown (kind, domain) is refused
    at the point a person asks for it, with a list of what exists — rather
    than at dispatch, after a row already exists to clean up.

    I1d — `story_id` IS HOW ANYBODY SEES THE RUN. There is no thread on an
    execution; the thread is derived twice, by card delivery and by the
    ledger's `principal_for`, and both walk execution -> story -> threads.
    An execution without one has no card, no principal, no sidebar row and
    no controls. Ec5's sweep passed none, so a scheduled errand that
    parked for approval was invisible and unanswerable.

    Defaults to None because every other caller supplies it by other
    means (`start_story` writes its own INSERT) and passing none keeps
    their behaviour byte-identical.
    """
    resolve(kind, domain)          # raises UnknownProfileError, by name
    return await conn.fetchval(
        """
        INSERT INTO executions
            (kind, domain, goal_id, state, task, terminal_command,
             workspace_source, story_id, driving_thread_id)
        VALUES ($1, $2, $3, 'PENDING', $4, $5, $6, $7, $8)
        RETURNING id
        """,
        kind, domain, goal_id, task, terminal_command, workspace_source,
        story_id, driving_thread_id,
    )


# Where the claim is mounted inside an execution pod. The same constant
# `kernel/drivers/k8s_spec.WORKSPACE_PATH` renders into the manifest --
# imported rather than respelled, because two files disagreeing about
# this would be a workspace composed at one path and looked for at
# another, which reads as an empty workspace.
from kernel.drivers.k8s_spec import WORKSPACE_PATH as POD_WORKSPACE
from kernel.workspace.briefing import write_workspace_md


class WorkspaceUnsupported(RuntimeError):
    """This driver cannot compose a workspace where the conductor runs.

    Its own type so dispatch_pass can tell "not built yet" from "the
    clone failed"; the first must not be retried every tick for ever
    under a message about file permissions.
    """


async def prepare_workspace(conn, execution_id: int, kind: str, domain: str,
                            root: str, driver=None) -> str:
    """Acquire the workspace the profile's driver calls for (E7d).

    Idempotent by way of `acquire`, which adopts an existing workspace
    rather than clobbering it — this is also the resume path after a
    sandbox is lost, and a re-clone here would destroy uncommitted work.
    """
    profile = resolve(kind, domain)
    row = await conn.fetchrow(
        "SELECT workspace_source, workspace_path FROM executions WHERE id = $1",
        execution_id)
    if row and row["workspace_path"]:
        root = row["workspace_path"]
    else:
        # PER EXECUTION, never the shared root. H0x spent four attempts
        # learning that a shared tree means run 1 does the work and every
        # run after starts with it already done -- and dispatch_pass was
        # handing every execution the bare workspace root.
        root = os.path.join(root, f"exec-{execution_id}")

    # E7e — the workspace is COMPOSED. `mounts_for` reads what this
    # execution was granted and falls back to `workspace_source` as a
    # single mount at the root, so a row created before E7e still comes
    # up rather than acquiring an empty directory.
    #
    # The profile still names a binding, and it still decides for a
    # profile that has no mounts at all (`none` — the errand case). What
    # it no longer does is decide how MANY there are.
    mounts = await mounts_for(conn, execution_id)

    # ── WHOSE FILESYSTEM IS THIS? ────────────────────────────────────
    #
    # Everything below composes the workspace HERE, in the conductor,
    # on the assumption the sandbox will share these bytes. That is the
    # Docker model and it is true there. On Kubernetes the workspace is
    # a ReadWriteOncePod claim belonging to the execution's own pod: the
    # conductor cannot mount it, and the API server is configured to
    # refuse if it tried.
    #
    # Running on anyway produced "[Errno 13] Permission denied:
    # '/workspace'" every tick -- a permissions error for something that
    # is not a permissions problem, and a retry of something that cannot
    # ever succeed. Both halves sent a person to the wrong place.
    pod_model = getattr(driver, "WORKSPACE_MODEL", "local") == "pod"

    # ── I3: A LOCAL DIRECTORY CANNOT BE MOUNTED INTO A POD ───────────
    #
    # Checked FIRST, and here rather than in the driver, because here is
    # before the pod exists: the mount list is known at admission, so
    # the fact can be stated as a fact about the grant instead of
    # arriving later as an initContainer that copied nothing and a pod
    # stuck short of Ready. D4 -- a refusal naming the resource beats a
    # dispatch cycle spent looking at the cluster.
    #
    # Ahead of the general pod refusal below on purpose: that one says
    # "nothing populates the claim yet" and stops being true when I2
    # lands, while this stays true for ever. The specific fact is also
    # the actionable one -- re-grant the document with a URI.
    if pod_model:
        stranded = local_directory_mounts(mounts)
        if stranded:
            raise WorkspaceUnsupported(
                local_source_refusal(execution_id, stranded))

    if pod_model:
        # ── I2: THE POD COMPOSES ITS OWN, AND THIS RECORDS WHERE ─────
        #
        # This used to refuse, because nothing populated the claim. It
        # does now: `kernel/drivers/k8s_workspace.py` adds an init
        # container running `kernel.workspace.podinit`, which calls the
        # same `CompositeDriver.acquire` the lines below call here.
        #
        # NOTHING IS DONE LOCALLY. Not the makedirs, not the acquire,
        # not the completeness check -- the claim is ReadWriteOncePod
        # and belongs to the pod, so every one of them would either fail
        # (as `[Errno 13] Permission denied: '/workspace'`) or, worse,
        # succeed against a directory in the conductor's own container
        # that the pod will never see.
        #
        # The path is recorded because the row is what tells the rest of
        # the system a workspace exists: `worker/loop.py` refuses to
        # rebuild a sandbox without `workspace_path`, on the grounds
        # that "a rebuild would start from nothing, which is a new run
        # wearing an old id". Completeness is decided in the pod, and
        # carried by whether its init container exited 0 -- a pod that
        # could not compose never reaches Ready, so the run never
        # dispatches against a half-built tree.
        await conn.execute(
            "UPDATE executions SET workspace_path = $2 WHERE id = $1",
            execution_id, POD_WORKSPACE)
        logger.info("execution %s composes its workspace in-pod at %s "
                    "(%d mount(s))", execution_id, POD_WORKSPACE, len(mounts))
        return POD_WORKSPACE

    # THE ROOT MUST EXIST BEFORE THE SANDBOX STARTS, whatever the driver.
    #
    # The sandbox binds this directory as a volume subpath, and Docker
    # refuses to start a container whose subpath is not there -- with a
    # 404 on /start, after create has already returned an id, so the
    # failure reads as a container that vanished rather than a directory
    # that was never made.
    #
    # The composite driver makes it; the `none` driver does not, because
    # a profile with no workspace has nothing to establish. An errand is
    # exactly that case, and it could not start at all.
    os.makedirs(root, exist_ok=True)

    if profile.workspace_driver == "none":
        driver, spec = driver_for("none"), WorkspaceSpec(
            execution_id=execution_id, root=root)
    else:
        driver = CompositeDriver()
        story_id = await conn.fetchval(
            "SELECT story_id FROM executions WHERE id = $1", execution_id)
        spec = WorkspaceSpec(execution_id=execution_id, root=root,
                             mounts=mounts, story_id=story_id or "")
    path = driver.acquire(spec)

    # COMPLETENESS, not just "acquire returned". Every git mount the
    # execution was granted must be a real checkout before this counts
    # as a workspace -- a half-built tree is the same lie as no tree,
    # and `workspace_path` being set is what lets the run dispatch.
    # Raising here (rather than recording the path) keeps the execution
    # out of dispatch until the clone genuinely works.
    missing = [m.at or "." for m in mounts
               if m.binding == "git"
               and not os.path.isdir(os.path.join(
                   path, *(m.at.split("/") if m.at else []), ".git"))]
    if missing:
        raise WorkspaceError(
            f"workspace incomplete: {', '.join(missing)} did not clone")

    # Ec4d — THE CLOUD IDENTITY, IF THIS RUN WAS GRANTED ONE.
    #
    # Here because the workspace now exists and the lease lives inside
    # it: a credential that outlived its workspace would be one nobody
    # is tracking. Before dispatch, because the executor must find it on
    # its first step rather than discovering halfway through that it
    # cannot reach its project.
    #
    # NOT FATAL WHEN THERE IS NO GRANT -- `issue` returns None and almost
    # every run takes that path. FATAL when a grant exists and cannot be
    # honoured: a run that should have had an identity and silently did
    # not fails later, somewhere unrelated, with a permission error
    # nobody traces back to this decision.
    from kernel.credentials.issue import issue

    lease = await issue(conn, execution_id, path, _credential_provider())
    if lease:
        # THE KEYS ARE CLOUD-NEUTRAL. `sa_email`/`project_id` were the
        # GCP-shaped names before three clouds shared this road; this
        # line kept them, raised KeyError on a real errand, and the
        # caller read it as "workspace not ready" -- so the run retried
        # for ever, re-minting a credential each time. A logging
        # statement took down the thing it was reporting on.
        logger.info("execution %s: holds %s in %s (grant %s, expires %s)",
                    execution_id, lease["account"], lease["scope"],
                    lease["granted_via"], lease["expires_at"])

    await conn.execute(
        "UPDATE executions SET workspace_path = $2, stall_reason = NULL "
        "WHERE id = $1",
        execution_id, path)

    # THE WORKSPACE EXPLAINS ITSELF. Two checkouts of one repo -- the
    # working clone and a read-only @branch lane -- look identical from
    # `ls`, and an executor that has to guess which one it may edit will
    # sometimes guess wrong in the one that matters. WORKSPACE.md names
    # every mount, its branch, and whether it may be modified; the model
    # reads files, so the contract lives where the model reads.
    try:
        write_workspace_md(path, mounts, lease)
    except OSError:
        logger.warning("WORKSPACE.md not written for %s", execution_id)
    return path


async def start(conn, driver, execution_id: int, goal_id: str,
                workspace_root: str) -> dict:
    """Admit, acquire a workspace, dispatch a sandbox. Returns what happened."""
    from kernel.conductor.admit import admit_pending

    row = await conn.fetchrow(
        "SELECT kind, domain FROM executions WHERE id = $1", execution_id)
    workspace = await prepare_workspace(
        conn, execution_id, row["kind"], row["domain"], workspace_root,
        driver=driver)

    # admit_pending must run inside a transaction; dispatch must NOT.
    async with conn.transaction():
        admitted = await admit_pending(conn, goal_id)
    if execution_id not in admitted:
        logger.info("execution %s was not admitted this pass "
                    "(admitted: %s)", execution_id, admitted)

    # A port of its own (H1c). It travels in the spec's env, which is where
    # the shim reads it and where the driver reads it to write the label —
    # one value, not three.
    port = allocate_bridge_port()
    # The contract's image, so the executor works in the environment
    # the Verifier will judge it in. Without this every contracted run
    # starts with an exec/verify divergence F6 would then report for
    # its whole life.
    image = await conn.fetchval(
        "SELECT c.base_image FROM contracts c JOIN executions e "
        "ON e.contract_id = c.id WHERE e.id = $1", execution_id)
    # F6 — the SDK variables, resolved from the grant at the one moment
    # the container's environment is decided.
    from kernel.credentials.issue import pointers_for

    spec = await spec_for_dispatch(conn, driver, execution_id,
                                   bridge_port=port, image=image or "",
                                   workspace_subpath=workspace_subpath(
                                       workspace),
                                   credential_pointers=await pointers_for(
                                       conn, execution_id))
    container_id = await dispatch_admitted(conn, driver, execution_id, spec)

    # AND WAITS FOR IT, because this caller is a person watching. The
    # passes do not wait — `dispatch_pass` probes and comes back next
    # tick — but `start` returns to a button and to `drive`, which asks
    # `claimable` for RUNNING rows and would find nothing to do. So the
    # wait that left `dispatch_admitted` happens here instead, for this
    # one execution: the substrate's own startup budget (`timeout=None`)
    # and no grace after it (`budget=0`), which is exactly what dispatch
    # used to do inline — destroy the sandbox, leave the row DISPATCHING
    # for reconcile, and fail loudly rather than return a run that will
    # not move.
    promoted = await promote_one(conn, driver, execution_id,
                                 timeout=None, budget=0)
    if promoted["action"] != "promoted":
        raise SandboxNotReady(
            f"execution {execution_id}: {promoted['detail']}")
    return {"execution_id": execution_id, "container_id": container_id,
            "workspace": workspace, "bridge_port": port}


async def drive(conn, driver, execution_id: int, run_leg_fn, make_executor,
                adapter, max_ticks: int = 20) -> dict:
    """Tick until the run finishes, parks, or the tick ceiling is reached.

    The ceiling is a test and console affordance, not a policy: the run's
    own limits are the leg ceiling and the budget predicates. Hitting this
    means the caller stopped watching, not that the run failed.
    """
    actions = []
    for _ in range(max_ticks):
        state = await conn.fetchval(
            "SELECT state FROM executions WHERE id = $1", execution_id)
        if state in TERMINAL_STATES or state in WAITING_STATES:
            break
        rows = [r for r in await claimable(conn) if r["id"] == execution_id]
        if not rows:
            break
        actions.append(await run_one_leg(
            conn, driver, rows[0], run_leg_fn, make_executor, adapter))
        if actions[-1]["action"] in ("done", "failed", "parked", "verifying",
                                     "leg_ceiling", "error"):
            break

    final = await conn.fetchval(
        "SELECT state FROM executions WHERE id = $1", execution_id)
    return {"execution_id": execution_id, "state": final, "actions": actions}


async def dispatch_pass(conn, driver, goal_id: str | None,
                        workspace_root: str,
                        limit: int | None = None) -> list[dict]:
    """Admit and dispatch whatever is waiting. The Conductor's own tick.

    `limit` DEFAULTS TO `dispatch_concurrency()`, not to the caller's leg
    concurrency. The caller passed `leg_concurrency()` — a number sized
    by the connection pool, because a leg holds a connection for minutes
    — and dispatch holds nothing and takes an API call per row. One
    number answered two unrelated questions and rationed the cheap one
    at the expensive one's price; see `conductor/dispatch_limit.py`.

    `admit_pending` had exactly one caller: `start()`, reached from a
    person pressing a button. So an execution that became PENDING any
    OTHER way sat there for ever, and nothing in the system would ever
    look at it again.

    That is not hypothetical. An amended story goes DONE -> PENDING
    deliberately — back through admission so the slot it released when
    it finished is re-claimed rather than bypassed — and with no
    periodic admission it stopped there. "Say what you want at any
    point" was true right up until the story had finished, which is the
    case it was written for.

    Admission was always meant to be a loop. It only looked like a
    function because one caller happened to be enough.
    """
    from kernel.conductor.admit import admit_pending
    from kernel.conductor.dispatch_limit import dispatch_concurrency

    if limit is None:
        limit = dispatch_concurrency()

    # PROMOTE FIRST. A sandbox created on an earlier pass and answering
    # now becomes RUNNING before `worker_tick` asks `claimable` for legs,
    # so a run starts working in the tick it became ready rather than the
    # one after. Cheap by construction: the probes run together and the
    # rows created later in THIS pass are not yet worth asking about — a
    # container is never ready one second after `create` returned.
    actions: list[dict] = list(await promote_ready(
        conn, driver, goal_id, limit=limit))
    waiting = await conn.fetch(
        "SELECT id, kind, domain, goal_id, state, story_id FROM executions "
        "WHERE ($1::text IS NULL OR goal_id = $1) AND state = 'PENDING' "
        "AND goal_id IS NOT NULL "
        "ORDER BY id LIMIT $2",
        goal_id, limit)

    # THE BUCKETS FIRST, FOR EVERYONE. A goal's slots are created lazily
    # on its first pending execution, so this cannot be folded into the
    # capacity filter below -- a fresh goal has no slot rows at all, and
    # a filter that asks for a free slot would find none, skip the
    # execution, and never reach the line that creates them. The story
    # would sit PENDING forever.
    # KEYED BY OWNER NOW, not by (kind, domain): the GO's autonomous
    # errands share one budget of ten per goal and each person's
    # assistant has five, so the bucket a row needs depends on who is
    # running it rather than on what sort of work it is.
    from kernel.conductor.admit import ensure_bucket
    from kernel.conductor.owner import resolve as resolve_owner
    for row in waiting:
        await ensure_bucket(
            conn, row["goal_id"],
            await resolve_owner(conn, row["id"], row["goal_id"],
                                row["story_id"]))

    # ONLY WHAT A SLOT COULD TAKE. Preparing a workspace is a git
    # operation, and it ran for EVERY pending execution on every tick --
    # including ones whose bucket is full, which admission then refuses,
    # so they come round again unchanged. One execution queued behind a
    # full bucket turned the loop into a clone-and-adopt spin: the
    # console log was nothing but "workspace already present, adopting"
    # for a run that was never going to start this tick.
    #
    # Advisory, not a lock. Another replica may take the slot between
    # this and the claim; that costs one prepared workspace, which is
    # idempotent and waiting when the slot frees.
    free = {r["id"] for r in await conn.fetch(
        """
        SELECT e.id FROM executions e
         WHERE e.id = ANY($1::bigint[])
           AND EXISTS (SELECT 1 FROM admission_slots s
                        WHERE s.goal_id = e.goal_id
                          AND s.domain = e.domain
                          AND s.kind = e.kind
                          AND (s.execution_id IS NULL
                               OR s.released_at IS NOT NULL))
        """, [r["id"] for r in waiting])} if waiting else set()

    # PENDING rows a slot could take, PLUS every ADMITTED row whose
    # workspace never materialised.
    #
    # THE SECOND HALF IS NOT AN OPTIMISATION -- without it a failed clone
    # is PERMANENT. Admission runs unconditionally further down this same
    # pass, so an execution whose clone just failed is admitted anyway;
    # the `ready` guard below then holds it back for exactly the right
    # reason (`workspace_path IS NULL`), and this loop -- the only thing
    # that would ever clear that -- never looks at it again, because it
    # only ever walked PENDING. The comment on the stall below promised
    # "it retries next tick"; that was true of PENDING and of nothing
    # else, and the gap is invisible because the run sits in a legitimate
    # state with a stale reason string.
    #
    # Observed on KER15: two stories, a 404 from the credential broker,
    # both admitted, both stuck at ADMITTED with empty `repos/` for
    # hours. Fixing the 404 alone would not have moved them.
    #
    # `prepare_workspace` is idempotent (`acquire` adopts an existing
    # tree) and clears `stall_reason` itself on success, so re-entering
    # it costs a stat on the happy path and is the whole recovery on the
    # unhappy one.
    from kernel.worker.workspace_retry import workspace_incomplete
    for row in list(waiting) + await workspace_incomplete(conn, goal_id, limit):
        if row["state"] == "PENDING" and row["id"] not in free:
            continue
        # The workspace first: admission does not need it, but dispatch
        # does, and for a reopened story `acquire` ADOPTS the tree with
        # its commits rather than starting one.
        try:
            await prepare_workspace(conn, row["id"], row["kind"],
                                    row["domain"], workspace_root,
                                    driver=driver)
        except WorkspaceUnsupported as e:
            # NOT A TRANSIENT, and not retried as one. The message a
            # person reads must say the capability is missing, not that
            # a directory could not be made -- the previous wording sent
            # somebody to check file modes on a container that has no
            # such volume by design. Left ADMITTED rather than FAILED:
            # the run is well-formed and resumes when the pod-side
            # workspace lands, and failing it would throw away a
            # correctly-proposed errand over a gap in the substrate.
            await conn.execute(
                "UPDATE executions SET stall_reason = $2, updated_at = now() "
                "WHERE id = $1", row["id"], str(e)[:500])
            logger.info("dispatch_pass: %s waits for a pod-side workspace",
                        row["id"])
            continue
        except Exception as e:                              # noqa: BLE001
            if "no profile for" in str(e):
                # Unplaceable, permanently: no profile means no envelope,
                # no surface, no dispatch -- ever. Leaving it PENDING is a
                # lie that also blocks the queue: four null-exec fixtures
                # from the old database sat at the head of the LIMITed
                # scan and starved every real run behind them.
                await conn.execute(
                    "UPDATE executions SET state = 'FAILED', "
                    "updated_at = now() WHERE id = $1 AND state = 'PENDING'",
                    row["id"])
                actions.append({"execution_id": row["id"],
                                "action": "failed",
                                "detail": f"unplaceable: {e}"})
                continue
            # RECORDED, and the execution is NOT dispatched below: a
            # sandbox with no workspace is a model staring at an empty
            # directory, and it will invent work to fill it. Live proof:
            # a transient 404 from the credential broker failed the
            # clone, the run dispatched anyway, and the executor wrote a
            # ten-step plan to create from scratch the branches the
            # engine had already cut. Left PENDING/ADMITTED, it retries
            # next tick -- which is all a transient needed, and which is
            # true of ADMITTED only because `workspace_incomplete` puts
            # it back in this loop. Until it did, this sentence was true
            # of PENDING and quietly false of everything else.
            await conn.execute(
                "UPDATE executions SET stall_reason = $2, updated_at = now() "
                "WHERE id = $1",
                row["id"], f"workspace not ready: {e}"[:500])
            logger.warning("dispatch_pass: workspace for %s: %s",
                           row["id"], e)

    async with conn.transaction():
        admitted = await admit_pending(conn, goal_id)
    for eid in admitted:
        actions.append({"execution_id": eid, "action": "admitted",
                        "detail": "a slot was claimed"})

    # ADMITTED and with NO container. Without the second half this
    # dispatched an execution that already had a sandbox and leaked the
    # first one — seen as two live containers for the same execution id.
    # `dispatch_admitted` guards the state transition with a CAS; it does
    # not guard against being asked twice in the window before the state
    # moves.
    # NO WORKSPACE, NO SANDBOX. The one invariant that was missing: an
    # execution whose mounts never materialised must not be handed to a
    # model. `workspace_path IS NULL` is exactly that state -- set only
    # by a completed prepare_workspace -- and an execution holding
    # mounts it could not clone stays here until the clone works.
    ready = await conn.fetch(
        "SELECT e.id FROM executions e "
        "WHERE ($1::text IS NULL OR e.goal_id = $1) "
        "AND e.state = 'ADMITTED' AND e.container_id IS NULL "
        "AND (e.workspace_path IS NOT NULL "
        "     OR NOT EXISTS (SELECT 1 FROM execution_mounts m "
        "                     WHERE m.execution_id = e.id)) "
        "ORDER BY e.id LIMIT $2", goal_id, limit)
    for row in ready:
        eid = row["id"]
        try:
            port = allocate_bridge_port()
            row2 = await conn.fetchrow(
                "SELECT c.base_image, e.workspace_path FROM executions e "
                "LEFT JOIN contracts c ON c.id = e.contract_id "
                "WHERE e.id = $1", eid)
            # THE SUBSTRATE DECIDES THE SHAPE. This called
            # `build_sandbox_spec` unconditionally, which asks the driver
            # for `sandbox_attachments` -- and Kubernetes refuses that by
            # name, because a pod's containers are declared together and
            # there is nothing to attach afterwards. The first errand to
            # reach dispatch on the cluster failed on it every tick and
            # sat at ADMITTED with no stall reason.
            #
            # The narrow bind is still the rule, and still the reason
            # this goes through one function: three dispatch paths build
            # a spec and each one has to agree; a single wide one gives
            # some runs the reach the others were denied, which makes
            # containment depend on which path happened to start you.
            spec = await spec_for_dispatch(
                conn, driver, eid, bridge_port=port,
                image=(row2["base_image"] if row2 else "") or "",
                workspace_subpath=workspace_subpath(
                    row2["workspace_path"] if row2 else None))
            container_id = await dispatch_admitted(conn, driver, eid, spec)
            # WHERE THE TREE IS, recorded for readers that are not this
            # process. The control panel serves the file explorer and
            # resolves AGENTOS_DRIVER from its OWN environment, which
            # only the conductor's deployment sets -- so asked which
            # substrate this execution runs on it would answer "docker"
            # and walk its own container. Written once here, by the
            # process that actually resolved it.
            # THE ADDRESS IS RECORDED BY THE PROMOTE PASS, not here.
            # This resolved it immediately after dispatch, which worked
            # only while dispatch blocked until the sandbox answered.
            # It no longer does, so the pod was ~1s old with no IP and
            # `resolve_bridge_host` refused -- surfacing as a failed
            # dispatch on a pod that was coming up fine (execution 194).
            # See kernel/conductor/promote.py:claim.
            actions.append({
                "execution_id": eid, "action": "dispatched",
                "detail": f"sandbox {container_id[:12]} on port {port}"})
        except Exception as e:                              # noqa: BLE001
            # One execution failing to dispatch must not stop the others;
            # reconcile picks up anything left stuck in ADMITTED.
            logger.exception("dispatch_pass: execution %s", eid)
            # THE SENTENCE, NOT THE TRANSPORT. `str(ApiException)` puts
            # the HTTP response HEADERS in front of the body, and on a
            # reconstructed GKE quota refusal the message begins at
            # offset 521 of a 698-character string -- so the `[:500]`
            # below used to cut it off entirely and store a header dump.
            # See kernel/drivers/k8s_error.py; the 403 is the LIKELIER
            # half of the xlarge quota trap D5 measured (plan 11.5i),
            # because ResourceQuota is an admission plugin.
            from kernel.drivers import k8s_error      # noqa: PLC0415
            # S1 · ON THE ROW, not only in a list nobody keeps. The
            # action goes to a log line and an in-memory summary; the
            # comment further up records the symptom this leaves --
            # "sat at ADMITTED with no stall reason" -- and it is the
            # same silence admission had. Best effort: a dispatch that
            # failed must not also fail to say so.
            try:
                await conn.execute(
                    "UPDATE executions SET stall_reason = $2, "
                    "stalled_at = COALESCE(stalled_at, now()) "
                    "WHERE id = $1",
                    eid, f"dispatch failed: {type(e).__name__}: "
                         f"{k8s_error.sentence(e)}"[:500])
            except Exception:                               # noqa: BLE001
                logger.warning("could not record the dispatch failure for "
                               "%s", eid, exc_info=True)
            actions.append({"execution_id": eid, "action": "dispatch_failed",
                            "detail": f"{type(e).__name__}: "
                                      f"{k8s_error.sentence(e)}"})
    return actions


def workspace_subpath(workspace_path: str | None) -> str:
    """Where this story's root sits INSIDE the shared volume.

    The sandbox binds the volume at `/workspace`, and binding all of it
    put every executor's cwd one level above its own tree -- so a
    relative `rm -rf` reached other stories. Returning the story's own
    subdirectory makes the bind narrow: inside the sandbox `/workspace`
    IS the story's root and there is nothing else to reach.

    Empty for a story whose root IS the volume, which is every execution
    created before per-run roots and the shape the legacy fallback
    produces.
    """
    path = (workspace_path or "").replace("\\", "/").rstrip("/")
    base = WORKSPACE_MOUNT.rstrip("/")
    if not path or path == base:
        return ""
    return path[len(base) + 1:] if path.startswith(base + "/") else ""


def _credential_provider() -> str:
    """Which provider mints for this deployment.

    STUB IS THE DEFAULT AND THAT IS DELIBERATE. A deployment that has not
    said it wants real cloud credentials must not get them by accident,
    and the stub fails loudly against a real API rather than quietly
    succeeding at something. Set AGENTOS_CREDENTIAL_PROVIDER=gcp where
    the host has an ambient identity and the IAM bindings to use it.

    B1 -- the fallback is spelled out here rather than hidden inside an
    `environ.get(x, default)` call, so it is visible in the code.
    """
    import os as _os

    name = _os.environ.get("AGENTOS_CREDENTIAL_PROVIDER")
    return name.strip() if name and name.strip() else "stub"
