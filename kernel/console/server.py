"""H0v — a local window onto a running executor.

Stdlib `http.server` and one connection pool. No framework, because the
bridge shim already established that a small HTTP surface here needs no
dependency, and adding one for a debugging window would be the tail wagging
the dog.

NOT A PRODUCT SURFACE. No auth, no multi-user anything, and exactly two
write paths: start a run, and answer a park. It is a window onto the
database for a person sitting in front of it before any of this is wired to
something real.

It does not invent verdicts. A story that proposes completion shows as
VERIFYING and stays there, because that is genuinely where it stops until
Stage F exists. Showing it as finished would make the console lie about the
one thing it is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg

from kernel.bridge.client import bridge_health
from kernel.console import api
from kernel.console.page import PAGE

from kernel.conductor.dispatch import NotAdmittedError
from urllib.parse import parse_qs

from kernel.loopbridge import LoopRuntime
from kernel.worker.executor_pass import build_executor
from kernel.workspace.mounts import writable_scope_for
from kernel.obs import configure

logger = logging.getLogger(__name__)

DSN = os.environ["DATABASE_URL"] if "DATABASE_URL" in os.environ else ""
CONSOLE_PORT = int(os.environ["CONSOLE_PORT"]) if "CONSOLE_PORT" in os.environ else 8770
WORKSPACE_ROOT = "/workspace"
GOAL_ID = "console"

# How many legs run at once now lives in `kernel/worker/concurrency.py`
# (AGENTOS_LEG_CONCURRENCY), with the pool-safety cap beside it. It is NOT
# restated here: a policy declared in two places is one nobody can change with
# confidence, and this one already misled — `WORKER_CONCURRENCY = 4` read as
# "four at once" and meant "four per sweep, in turn".


class Handler(BaseHTTPRequestHandler):
    def __init__(self, runtime, *a, **kw):
        self.runtime = runtime
        super().__init__(*a, **kw)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _send(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Never cached. The page is edited and the server restarted many
        # times an hour, and stdlib http.server sends no cache headers at
        # all — so a browser applies its own heuristic and keeps serving
        # the old page. Observed: a fixed approval panel reported as still
        # broken, twice, because the fix was on the wire and not on the
        # screen. The JSON is live data and must never be cached either.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def log_message(self, fmt, *args):
        pass                            # the journal is the interesting log

    async def _with_conn(self, fn):
        async with self.runtime.pool.acquire() as conn:
            return await fn(conn)

    # ── reads ────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            # ── H12: the executor's files, per mount ────────────────
            #
            # Read-only and served from the WORKER's view of the volume,
            # which is the same tree the sandbox has mounted at its own
            # root. The live viewer walks the assistant's directory over
            # the gateway RPC; the assistant has no directory any more.
            if path == "/api/tasks":
                from kernel.corpus.seed import available
                return self._json({"tasks": available()})
            if path == "/api/workspace":
                q = parse_qs(self.path.split("?", 1)[1]
                             if "?" in self.path else "")
                # A STORY ID IS NOT AN INT. This read `int(story)` and
                # the control panel passes the id it holds, which is a
                # story like 'de5512b2' -- so every open of the file
                # explorer raised ValueError behind a 502. The kernel's
                # own browse resolves either form.
                sid = (q.get("story") or [""])[0]
                rel = (q.get("path") or [""])[0]
                mount = (q.get("mount") or [""])[0]
                want = (q.get("view") or ["tree"])[0]
                from kernel.workspace.browse import browse
                return self._json(self.runtime.call(
                    self._with_conn(lambda c: browse(c, sid, mount, rel,
                                                     want))))
            if path == "/api/executions":
                return self._json(self.runtime.call(
                    self._with_conn(lambda c: api.executions(c, GOAL_ID))))
            if path.startswith("/api/execution/"):
                eid = int(path.rsplit("/", 1)[1])
                return self._json(self.runtime.call(
                    self._with_conn(lambda c: api.detail(c, eid))))
            if path.startswith("/api/artifact/"):
                art = path.rsplit("/", 1)[1]
                from kernel.artifacts.store import ArtifactNotFound, get_store
                try:
                    body = get_store().read_range(art, 0, 200_000)
                except ArtifactNotFound:
                    return self._json({"error": "no such artifact"}, 404)
                return self._send(200, body, "text/plain; charset=utf-8")
            if path == "/api/profiles":
                from kernel.profiles.registry import all_profiles
                return self._json([
                    {"kind": p.kind, "domain": p.domain,
                     "tools": len(p.tools), "driver": p.workspace_driver,
                     "verifier": p.has_verifier}
                    for p in all_profiles()])
            return self._json({"error": "not found"}, 404)
        except Exception as e:                              # noqa: BLE001
            logger.exception("console GET %s", path)
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ── the writes ───────────────────────────────────────────────────────
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            # -- H13: start one of the seeded tasks --------------
            #
            # The corpus tasks were contracts with no way to put one in
            # front of an executor. This is that way: it builds a fresh
            # origin, records the mounts, writes the contract from the
            # task's own checks and dispatches -- so "run the multi-repo
            # one and watch it" is a click rather than an afternoon.
            if self.path == "/api/seed":
                return self._json(self.runtime.call(self._seed(body)))
            if self.path == "/api/start":
                return self._json(self.runtime.call(self._start(body)))
            if self.path == "/api/answer":
                return self._json(self.runtime.call(self._answer(body)))
            if self.path == "/api/propose":
                return self._json(self.runtime.call(self._propose(body)))
            if self.path == "/api/reopen":
                return self._json(self.runtime.call(self._reopen(body)))
            if self.path == "/api/amend":
                return self._json(self.runtime.call(self._amend(body)))
            if self.path == "/api/accept":
                return self._json(self.runtime.call(self._accept(body)))
            if self.path == "/api/carry_forward":
                return self._json(self.runtime.call(self._carry(body)))
            return self._json({"error": "not found"}, 404)
        except Exception as e:                              # noqa: BLE001
            logger.exception("console POST %s", self.path)
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    async def _seed(self, body):
        """Seed a corpus task and dispatch it. Returns what it started."""
        from kernel.console import contract as contract_mod
        from kernel.corpus.seed import seed
        from kernel.drivers.registry import make as make_driver
        from kernel.harness.loop import run_leg
        from kernel.harness.models import get_adapter
        from kernel.worker.runner import drive, prepare_workspace, start

        driver = make_driver()
        async with self.runtime.pool.acquire() as conn:
            # The goal the story belongs to. Defaults to the console's
            # own, but an assistant resolves ITS goal from the chat
            # session -- so a story seeded into `console` while somebody
            # is talking to an assistant in KER01 is invisible to them,
            # and the assistant is right to say nothing is running.
            goal = body.get("goal") or GOAL_ID
            made = await seed(conn, body["task"], goal,
                              f"{WORKSPACE_ROOT}/origins",
                              f"{WORKSPACE_ROOT}/runs")
            eid = made["execution_id"]
            row = await conn.fetchrow(
                "SELECT kind, domain FROM executions WHERE id = $1", eid)
            await _ensure_bucket(conn, row["kind"], row["domain"], goal)

            # The contract BEFORE the work starts, from the task's own
            # checks. Without one the adjudicator reports `no_contract`
            # every tick and the story sits in VERIFYING for ever.
            note = ""
            try:
                await prepare_workspace(conn, eid, row["kind"], row["domain"],
                                        WORKSPACE_ROOT)
                contract_id, note = await contract_mod.create(
                    conn, goal, row["kind"], row["domain"],
                    await conn.fetchval(
                        "SELECT workspace_path FROM executions WHERE id = $1",
                        eid),
                    made["checks"],
                    contract_mod.parse_globs(body.get("surface")),
                    image=body.get("image") or contract_mod.DEFAULT_IMAGE)
                if contract_id:
                    await conn.execute(
                        "UPDATE executions SET contract_id = $2 "
                        "WHERE id = $1", eid, contract_id)
            except Exception as e:                          # noqa: BLE001
                logger.exception("console: contract for seeded %s", eid)
                note = f"no contract ({type(e).__name__}: {e})"

            try:
                started = await start(conn, driver, eid, goal,
                                      WORKSPACE_ROOT)
            except NotAdmittedError:
                started = {"bridge_port": None}

        adapter = get_adapter(body.get("adapter") or "gemini")

        async def _drive():
            async with self.runtime.pool.acquire() as conn:
                def make_executor(profile, row, plan_approved=None):
                    # ONE construction site, in kernel/worker/executor_pass.
                    # This was a copy; see the note there for what the
                    # copies had already drifted into.
                    return build_executor(
                        profile, row, plan_approved, conn=conn,
                        adapter=adapter, actor_prefix="console")
                return await drive(conn, driver, eid, run_leg,
                                   make_executor, adapter)

        asyncio.run_coroutine_threadsafe(_drive(), self.runtime.loop)
        return {"execution_id": eid, "goal": goal, "task": made["task"],
                "kind": made["kind"], "mounts": made["mounts"],
                "contract": note,
                "bridge_port": started.get("bridge_port")}

    async def _workspace(self, eid: int, mount: str, rel: str, want: str):
        """tree | file | diff, for one story's mounts."""
        from kernel.workspace import explorer as ws

        async with self.runtime.pool.acquire() as conn:
            mounts = await ws.mounts_of(conn, eid)
        if not mounts:
            return {"story": eid, "mounts": [],
                    "detail": "this story has no workspace yet"}

        chosen = next((m for m in mounts if m["at"] == mount), mounts[0])
        if want == "file":
            return {"story": eid, "mount": chosen["at"],
                    **ws.read(chosen["root"], rel)}
        if want == "diff":
            return {"story": eid, "mount": chosen["at"],
                    "diff": ws.diff(chosen["root"], rel)}
        return {
            "story": eid,
            # Every mount is listed even when only one is being shown, so
            # a person can see they were granted three repositories and
            # which one they are looking at.
            "mounts": [{"at": m["at"], "kind": m["kind"],
                        "writable": m["writable"]} for m in mounts],
            "showing": chosen["at"],
            **ws.tree(chosen["root"], chosen["at"], chosen["writable"]),
        }

    async def _start(self, body):
        """Create and drive a run — the same path the live smoke uses."""
        from kernel.drivers.registry import make as make_driver
        from kernel.harness.loop import run_leg
        from kernel.harness.models import get_adapter
        from kernel.console import contract as contract_mod
        from kernel.worker.runner import (
            create_execution, drive, prepare_workspace, start,
        )

        kind, domain = body["profile"].split("/", 1)
        # G7: selected once, in the registry. Stage I adds a row.
        driver = make_driver()
        async with self.runtime.pool.acquire() as conn:
            eid = await create_execution(
                conn, kind=kind, domain=domain, task=body["task"],
                goal_id=GOAL_ID,
                terminal_command=body.get("terminal_command") or None,
                workspace_source=body.get("workspace_source") or None)
            await _ensure_bucket(conn, kind, domain)

            # A workspace of its own, when the caller asks for one.
            #
            # Every console story has shared one `/workspace` since H0v,
            # and it persists in a named volume. So the FIRST run of a
            # task does it and every run after starts with the work
            # already done -- which is not a slow degradation, it is the
            # test measuring nothing from the second run onward. It also
            # means two stories cannot run at once without clobbering
            # each other, which H11 and H14 both need.
            #
            # `prepare_workspace` already prefers `workspace_path` over
            # the root it is passed, so this is the whole change.
            # A ROOT OF ITS OWN, per execution.
            #
            # Every console story shared `/workspace` -- which is itself a
            # git repo -- so two stories could not run at once without
            # clobbering each other, and one of them cleaning its tree
            # deleted the other's. Observed: execution 1541's leg 12
            # committed "Restore clean check surface", ran a clean at the
            # shared root, and took out the freshly seeded workspace of
            # the run beside it plus two stages' worth of artifacts.
            #
            # A story's workspace must be a place nothing else owns. It
            # stays UNDER /workspace because that is the volume the
            # sandbox shares; what changes is that each story gets its
            # own directory in it rather than the root itself.
            await conn.execute(
                "UPDATE executions SET workspace_path = $2 WHERE id = $1",
                eid, body.get("workspace_path")
                or f"{WORKSPACE_ROOT}/runs/{eid}")

            # The contract, BEFORE the work starts. Without one the
            # adjudicator reports `no_contract` every tick and the story
            # sits in VERIFYING for ever — the exact state F2b exists to
            # end. The workspace has to exist first, because the check
            # surface is hashed over the tree as it is at dispatch.
            note = ""
            try:
                await prepare_workspace(conn, eid, kind, domain,
                                        WORKSPACE_ROOT)
                contract_id, note = await contract_mod.create(
                    conn, GOAL_ID, kind, domain,
                    (await conn.fetchval(
                        "SELECT workspace_path FROM executions WHERE id = $1",
                        eid)) or WORKSPACE_ROOT,
                    contract_mod.parse_checks(body.get("checks")),
                    contract_mod.parse_globs(body.get("surface")),
                    image=body.get("image") or contract_mod.DEFAULT_IMAGE)
                if contract_id:
                    await conn.execute(
                        "UPDATE executions SET contract_id = $2 WHERE id = $1",
                        eid, contract_id)
            except Exception as e:                          # noqa: BLE001
                logger.exception("console: contract for execution %s", eid)
                note = f"no contract ({type(e).__name__}: {e})"

            try:
                started = await start(conn, driver, eid, GOAL_ID, WORKSPACE_ROOT)
            except NotAdmittedError as e:
                # The background sweep got there first, which is not a
                # failure -- it is level-triggered reconciliation doing
                # exactly its job. The console admitted the row and the
                # worker dispatched it; the run IS starting. Raising here
                # returned a 500 to the caller for a story that was
                # already under way, and the cancel handler below would
                # then not fire (the row is DISPATCHING, not PENDING), so
                # the only casualty was the caller's belief.
                logger.info("console: %s already taken by the sweep: %s",
                            eid, e)
                # The same keys the response is built from. Returning a
                # partial dict here traded a 500 on a race for a 500 on a
                # KeyError -- the story started fine both times and the
                # caller was told it had not, which is the failure this
                # branch exists to stop.
                started = {"bridge_port": None, "taken_by": "worker sweep",
                           "container_id": await conn.fetchval(
                               "SELECT container_id FROM executions "
                               "WHERE id = $1", eid) or "",
                           "workspace": await conn.fetchval(
                               "SELECT workspace_path FROM executions "
                               "WHERE id = $1", eid) or WORKSPACE_ROOT}
            except Exception:
                # A start that fails must not leave the row behind holding
                # an admission slot.
                #
                # `admit_pending` is FIFO over the whole goal (S7): it
                # admits every PENDING execution it can, while this handler
                # dispatches exactly one. So a failed start left a row at
                # PENDING, the NEXT start admitted it, and it sat ADMITTED
                # holding a slot nothing would ever dispatch. Three failed
                # clicks in a row cost three of four slots, and the fourth
                # failure was the one a person noticed.
                #
                # Machine v3 has PENDING -> CANCELLED and
                # ADMITTED -> CANCELLED edges for precisely this.
                await conn.execute(
                    "UPDATE executions SET state = 'CANCELLED' WHERE id = $1 "
                    "AND state IN ('PENDING', 'ADMITTED')", eid)
                raise

        adapter = get_adapter(body.get("adapter") or "gemini")

        # Driven on the background loop so the page can poll while it runs.
        async def _drive():
            async with self.runtime.pool.acquire() as conn:
                # make_executor is defined HERE so it closes over the
                # connection this run is using. Passing conn=None was the
                # first version, and ask_human and register_plan both need
                # a handle to record what the run is waiting on — so every
                # park attempt returned "no database context", the model
                # retried, and the run spun through register_plan and
                # ask_human six times without ever parking.
                def make_executor(profile, row, plan_approved=None):
                    # Workspace and port come from the `start` call rather
                    # than the row, which has not been updated yet -- that
                    # is the only difference from the sweep's path, and it
                    # is now an argument instead of a second copy.
                    #
                    # This copy also omitted `identity`, so a run driven
                    # from the console committed with no attribution. The
                    # shared builder always sets it.
                    return build_executor(
                        profile, row, plan_approved, conn=conn,
                        adapter=adapter, actor_prefix="console",
                        workspace=started["workspace"],
                        bridge_port=started["bridge_port"])

                return await drive(conn, driver, eid, run_leg,
                                   make_executor, adapter)

        asyncio.run_coroutine_threadsafe(_drive(), self.runtime.loop)
        return {"execution_id": eid,
                "container": (started.get("container_id") or "")[:12],
                "bridge_port": started.get("bridge_port"),
                "taken_by": started.get("taken_by", "console"),
                "contract": note}

    async def _amend(self, body):
        """One act: the human changes what this story is for.

        Available in EVERY state, because a person should not have to
        know whether the executor is mid-leg, parked, being verified or
        finished in order to say "actually, also do X".
        """
        async def go(conn):
            from kernel.console import contract as contract_mod
            from kernel.worker.amend import AmendRefused, amend
            checks = body.get("checks")
            parsed = (contract_mod.parse_checks(checks)
                      if checks is not None else None)
            try:
                return {"ok": True, **await amend(
                    conn, int(body["execution_id"]),
                    scope=body.get("scope") or "", checks=parsed,
                    by=body.get("who") or "console")}
            except AmendRefused as e:
                return {"ok": False, "error": str(e)}
        return await self._with_conn(go)

    async def _accept(self, body):
        """The human finishes a story the Verifier will not pass.

        Never a pass. The refused verdict stays refused and this writes
        a separate record of who overrode it and why — so the page can
        keep saying both things afterwards, which is the only reason
        overriding a Verifier is safe to offer at all.
        """
        async def go(conn):
            from kernel.worker.accept import AcceptRefused, accept
            try:
                return {"ok": True, **await accept(
                    conn, int(body["execution_id"]),
                    reason=body.get("reason") or "",
                    by=body.get("who") or "console")}
            except AcceptRefused as e:
                return {"ok": False, "error": str(e)}
        return await self._with_conn(go)

    async def _carry(self, body):
        async def go(conn):
            from kernel.conductor.machine import CarryRefused, carry_forward
            try:
                return {"ok": True,
                        **await carry_forward(conn,
                                              int(body["execution_id"]),
                                              body.get("who") or "console")}
            except CarryRefused as e:
                return {"ok": False, "error": str(e)}
        return await self._with_conn(go)

    async def _reopen(self, body):
        return await self._with_conn(lambda c: _reopen_impl(c, body))

    async def _propose(self, body):
        return await self._with_conn(lambda c: _propose_impl(c, body))

    async def _answer(self, body):
        """Answer a park. For a PLAN, carry the person's notes with it.

        `verdict` is approve | revise | declined. `revise` is not a
        decline: the approach is right and the plan is not, and telling
        a model its approach was rejected when a person wanted one step
        changed makes it abandon something they asked for.
        """
        from kernel.harness.park import ApprovalRefused, resolve_request
        verdict = body.get("verdict") or (
            "declined" if body.get("declined") else "approve")
        async with self.runtime.pool.acquire() as conn:
            try:
                await resolve_request(
                    conn, int(body["request_id"]), body.get("answer") or "",
                    answered_by=body.get("who") or "console",
                    # H8a — the console is a person at a keyboard, which
                    # is what makes this the approving path. In
                    # production the router supplies a real uid here.
                    human_uid=body.get("who") or "console",
                    # `revise` blocks work exactly as a decline does — it
                    # is not permission — while reading to the model as
                    # "change this plan" rather than "abandon this
                    # approach".
                    declined=verdict != "approve",
                    verdict=verdict, step_notes=body.get("step_notes") or [])
            except ApprovalRefused as e:
                # F11. Shown, not swallowed: the person tried to approve
                # and the reason they could not is the useful part.
                return {"ok": False, "error": str(e)}
        return {"ok": True}


async def _reopen_impl(conn, body):
    """Re-scope a finished story: more work, same execution, new contract.

    The contract is written HERE, before the state moves, because
    reopening without one puts the story back to work with a definition
    of done it has already met — it would claim completion immediately
    and E5's fence would refuse it for an unchanged reason set.
    """
    from kernel.console import contract as contract_mod
    from kernel.worker.reopen import ReopenRefused, reopen

    eid = int(body["execution_id"])
    row = await conn.fetchrow(
        "SELECT kind, domain, workspace_path FROM executions WHERE id = $1",
        eid)
    if row is None:
        return {"ok": False, "error": f"no execution {eid}"}

    checks = contract_mod.parse_checks(body.get("checks"))
    if not checks:
        return {"ok": False, "error":
                "a re-scope needs checks: they are the new definition of "
                "done, and without them the story can work but never "
                "finish"}
    contract_id, note = await contract_mod.create(
        conn, GOAL_ID, row["kind"], row["domain"],
        row["workspace_path"] or WORKSPACE_ROOT, checks,
        contract_mod.parse_globs(body.get("surface")),
        image=body.get("image") or contract_mod.DEFAULT_IMAGE,
        approved_by=body.get("who") or "console")
    if contract_id is None:
        return {"ok": False, "error": note}

    try:
        result = await reopen(conn, eid, body.get("addendum") or "",
                              contract_id,
                              actor=body.get("who") or "console")
    except ReopenRefused as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "note": f"{result['detail']} · {note}"}


async def _propose_impl(conn, body):
    """Speak as the assistant: put an intent in the mailbox.

    Deliberately NOT a direct write to the run. The console is a window
    onto the same mechanism the assistant uses, and an "inject a message"
    button that bypassed the mailbox would let this UI do something no
    real caller can — which would make every test run through it prove
    less than it appears to.
    """
    from kernel.mailbox.api import submit_proposal
    from kernel.mailbox.schema import Invalid

    kind = body.get("kind") or "steer"
    payload = dict(body.get("payload") or {})

    # ROUTED, NOT TAKEN LITERALLY, for questions. `ask` is answered by
    # the executor itself and only exists while a leg is live; `question`
    # is answered beside the run and works everywhere. A caller that
    # picked the wrong one would either get silence (an `ask` to a parked
    # run, waiting out the fallback) or a reconstruction where the model
    # itself could have answered. Deciding it here means no caller has to
    # know the leg model to ask a question — which is the same reason
    # `assistant/write.ask_executor` routes rather than accepts.
    if kind in ("question", "ask"):
        from kernel.mailbox.schema import route_question

        state = await conn.fetchval(
            "SELECT state FROM executions WHERE id = $1",
            int(body["execution_id"]))
        kind = route_question(state or "")
        if body.get("text"):
            payload.setdefault("question", body["text"])
    if kind == "steer" and body.get("text"):
        payload.setdefault("instruction", body["text"])
    if kind == "cancel" and body.get("text"):
        payload.setdefault("reason", body["text"])
    if kind == "replan" and body.get("text"):
        payload.setdefault("reason", body["text"])

    result = await submit_proposal(
        conn, body.get("who") or "console-assistant", kind, payload,
        body.get("key") or "", int(body["execution_id"]))
    if isinstance(result, Invalid):
        return {"ok": False, "code": result.code, "error": result.message}
    return {"ok": True, "proposal_id": result,
            "note": "queued; it lands at the executor's next step boundary"}


async def _ensure_bucket(conn, kind: str, domain: str,
                         goal_id: str = GOAL_ID) -> None:
    """Buckets are keyed by OWNER now, not by (kind, domain).

    The console seeds runs for its own testing and has no assistant
    behind them, so they go to the unattributed bucket -- which carries
    the assistant ceiling rather than no limit. `kind` and `domain` are
    kept in the signature because several console handlers pass them and
    changing every call site to drop two arguments would be churn in an
    ops surface that says of itself "this is not a product surface".
    """
    from kernel.conductor.admit import ensure_bucket
    from kernel.conductor.owner import UNKNOWN
    return await ensure_bucket(conn, goal_id, UNKNOWN)


async def _ensure_bucket_old(conn, kind: str, domain: str,
                             goal_id: str = GOAL_ID) -> None:
    """Admission needs a bucket. The console makes one rather than failing.

    Per GOAL: the ceiling and its slots are keyed by (goal, domain, kind),
    so a story seeded into a goal with no bucket is admitted by nothing
    and sits at PENDING with no explanation.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM admission_buckets WHERE goal_id = $1 AND domain = $2 "
        "AND kind = $3", goal_id, domain, kind)
    if exists:
        return
    await conn.execute(
        "INSERT INTO admission_buckets (goal_id, domain, kind, ceiling) "
        "VALUES ($1, $2, $3, 4)", goal_id, domain, kind)
    for i in range(4):
        await conn.execute(
            "INSERT INTO admission_slots (goal_id, domain, kind, ceiling, "
            "slot_index) VALUES ($1, $2, $3, 4, $4)", goal_id, domain, kind, i)


def serve(port: int = CONSOLE_PORT) -> None:
    """The view. IT NO LONGER RUNS THE EXECUTION PLANE.

    `_worker_forever` used to be started here, as a side effect of
    binding this port -- so the only way to run a story was to start a
    debug HTTP server, two conductors were impossible, and deleting this
    unauthenticated surface would have deleted the execution plane with
    it. It lives in `kernel/worker/service.py` now and is started by
    name:

        python -m kernel.worker.service

    A console with no worker behind it is a window onto a system that is
    not running, so it says so rather than looking healthy and empty.
    """
    runtime = LoopRuntime(DSN)
    # THREADING, not the single-threaded HTTPServer. One slow request
    # blocked every other, and once the background sweep started
    # creating containers inside a pooled connection the whole page
    # went dead — not slow, dead, with the worker logging happily
    # underneath. A debugging window that stops answering while the
    # thing it debugs is busy is a window onto nothing.
    server = ThreadingHTTPServer(("0.0.0.0", port),
                                 partial(Handler, runtime))
    print(f"console on http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    configure(component="worker")
    serve()
