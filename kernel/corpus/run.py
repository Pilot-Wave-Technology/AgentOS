"""G3 — run one corpus task as a real story, and grade it.

Not a special path. The task is dispatched through `create_execution`,
`prepare_workspace`, `start` and the ordinary worker tick, and it is
adjudicated by the ordinary adjudicator against the ordinary contract.
A corpus that ran stories a different way would measure the corpus.

THE TASK'S CHECKS ARE THE CONTRACT, AND THE MODEL DOES NOT WRITE THEM.
F11 gives the executor a say in its own criteria, and that is right when
a person approves them — here nobody is watching, and an executor
grading itself is the one thing the whole contract model exists to
prevent. So the contract is built from `task.json` before dispatch, the
way a person would have written it.

The model's own proposed checks are still RECORDED. When it registers a
plan carrying checks they are written into the run's record verbatim — a
signal about what this executor thought a bar worth having looks like —
and then cleared before approval so they cannot affect the grade.

They used to be dry-run first, and that is what made it a measurement
rather than a transcript. E9 retired the dry run (plan 5.6): it was a
full `verify_claim` per approval, 112.5s on execution 208, and the
corpus paid it once per plan for a signal nobody graded on. What is
lost is named here rather than quietly dropped — grading the checks
needs the goal criteria run against the base commit in the warm pod,
which is `discriminate.judge` on a cheaper measurement, and nothing
wires it yet.

WHO APPROVES THE PLAN. The harness does, immediately, and that is a
declared limitation rather than a convenience: what is being measured
here is editing, not the approval conversation. A run blocked at plan
approval would score zero for a reason that has nothing to do with the
tool surface G4–G6 changes. The plan's content is recorded so the
approval can be re-examined later.
"""

from __future__ import annotations

import json
import logging
import os

from kernel.corpus.load import repo_dir
from kernel.corpus.workspace import checkout

logger = logging.getLogger(__name__)

IMAGE = "agentos-kernel/fixture:dev"
# A corpus run that has not finished in this many ticks has told us
# something (it is stuck), and continuing costs model budget to learn
# nothing further. Recorded as `not_attempted` rather than as a failure.
MAX_TICKS = 24


async def ensure_bucket(conn, goal_id: str, kind: str = "story",
                        domain: str = "coding", ceiling: int = 1) -> None:
    """Admission needs a bucket, and nothing creates one for a new goal.

    The first corpus run sat at PENDING for its whole tick budget and
    reported `never got that far` — which was true and said nothing
    about the executor. `dispatch_pass` had no slot to give it.

    Ceiling 1, not the console's 4: corpus runs are measured, and two
    stories sharing a machine contend for CPU in a way that moves
    `tokens_to_first_write` for reasons that have nothing to do with the
    tool surface.
    """
    # SLOTS, not the bucket. Admission claims a slot; the bucket is only
    # the ceiling that describes them. Checking the bucket left a run
    # PENDING for its whole tick budget after a teardown that deleted
    # slots and kept buckets — the bucket existed, this returned early,
    # and there was nothing to claim.
    await conn.execute(
        "INSERT INTO admission_buckets (goal_id, domain, kind, ceiling) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
        goal_id, domain, kind, ceiling)
    slots = await conn.fetchval(
        "SELECT count(*) FROM admission_slots WHERE goal_id = $1 "
        "AND domain = $2 AND kind = $3", goal_id, domain, kind)
    if slots:
        return
    for i in range(ceiling):
        await conn.execute(
            "INSERT INTO admission_slots (goal_id, domain, kind, ceiling, "
            "slot_index) VALUES ($1, $2, $3, $4, $5)",
            goal_id, domain, kind, ceiling, i)


async def make_contract(conn, goal_id: str, task: dict) -> int:
    """The task's checks, as the contract. Written before dispatch."""
    contract_id = await conn.fetchval(
        "INSERT INTO contracts (goal_id, kind, domain, base_image, "
        "workspace_spec, approved_at, approved_by) VALUES "
        "($1, 'story', 'coding', $2, '{}'::jsonb, now(), 'corpus') "
        "RETURNING id", goal_id, IMAGE)
    for c in task["checks"]:
        await conn.execute(
            "INSERT INTO criteria (contract_id, kind, spec, label) "
            "VALUES ($1, 'command', $2::jsonb, $3)",
            contract_id, json.dumps({"run": c["run"]}), c["label"])
    return contract_id


async def approve_open_plan(conn, driver, execution_id: int,
                            record: dict) -> bool:
    """Approve whatever plan is waiting. Returns True if one was.

    The model's proposed checks are recorded and then cleared: they are
    evidence about the executor, and they must not reach the contract
    the executor is graded against.

    `driver` is unused since E9 took the dry run out of this. Kept in the
    signature because the corpus runner threads it through every step
    and a signature that changes shape per step is worse than an
    argument that is not read.
    """
    from kernel.harness.park import resolve_request

    row = await conn.fetchrow(
        "SELECT id, payload::text, checks::text AS checks FROM questions "
        "WHERE execution_id = $1 AND status = 'OPEN' "
        "AND kind = 'authorization' ORDER BY id DESC LIMIT 1", execution_id)
    if row is None:
        return False

    proposed = json.loads(row["checks"] or "[]")
    if proposed:
        record.setdefault("proposed_checks", []).append({
            "checks": proposed})
        # Cleared, in the open, for the reason at the top of this file.
        await conn.execute(
            "UPDATE questions SET checks = '[]'::jsonb WHERE id = $1",
            row["id"])

    record.setdefault("plans", []).append(json.loads(row["payload"]))
    # H8a — a corpus run has no person, and auto-approving is the
    # measurement. Stated in the open, as the check-clearing above is.
    await resolve_request(conn, row["id"], "Approved by the corpus harness.",
                          answered_by="corpus", verdict="approve",
                          human_uid="corpus:auto-approve")
    return True


async def answer_open_question(conn, execution_id: int, record: dict) -> bool:
    """A corpus run has nobody to ask, so an ask ENDS it.

    Deliberately not answered with a guess. The traps in G2 exist to
    measure asking, and a harness that invents answers here would both
    corrupt this measurement and destroy that one.
    """
    from kernel.harness.park import resolve_request

    row = await conn.fetchrow(
        "SELECT id, question_text FROM questions WHERE execution_id = $1 "
        "AND status = 'OPEN' AND kind <> 'authorization' "
        "ORDER BY id DESC LIMIT 1", execution_id)
    if row is None:
        return False
    record["asked"] = row["question_text"][:400]
    await resolve_request(
        conn, row["id"],
        "This is a corpus run with no human attached. The question is "
        "recorded and the run stops here.",
        answered_by="corpus", declined=True, verdict="declined")
    return True


async def run_task(conn, driver, task: dict, goal_id: str, scratch: str,
                   tick, max_ticks: int = MAX_TICKS) -> dict:
    """Dispatch, drive to a verdict, and measure. Returns the record."""
    from kernel.corpus.metrics import measure
    from kernel.worker.runner import create_execution, prepare_workspace

    record: dict = {"task": task["id"], "title": task["title"]}
    where = os.path.join(scratch, f"corpus-run-{task['id']}")
    seed = checkout(repo_dir(), where)
    record["seed"] = seed

    await ensure_bucket(conn, goal_id)
    contract_id = await make_contract(conn, goal_id, task)
    eid = await create_execution(
        conn, kind="story", domain="coding", task=task["prompt"],
        goal_id=goal_id, workspace_source=None)
    await conn.execute(
        "UPDATE executions SET contract_id = $2, workspace_path = $3 "
        "WHERE id = $1", eid, contract_id, where)
    await prepare_workspace(conn, eid, "story", "coding", where)
    record["execution_id"] = eid

    ticks = 0
    while ticks < max_ticks:
        ticks += 1
        state = await conn.fetchval(
            "SELECT state FROM executions WHERE id = $1", eid)
        if state in ("DONE", "FAILED", "CANCELLED"):
            break
        if state in ("PARKED_HOT", "PARKED_COLD"):
            if await approve_open_plan(conn, driver, eid, record):
                continue
            if await answer_open_question(conn, eid, record):
                break
        await tick(conn, driver, goal_id)

    # A RUN THE HARNESS HAS STOPPED WATCHING IS OVER, and must say so to
    # admission. The first five-task baseline ran ONE task:
    # `jsonfile-backend` used its tick budget, ended non-terminal, and
    # kept the single admission slot for ever — so the other four sat at
    # PENDING and reported `never got that far`, which was true and had
    # nothing to do with them.
    #
    # Cancelled rather than left: cancel is a terminal state, the h1c
    # trigger releases the slot on the way in, and reconcile's 3b
    # disposition then reaps the container. Leaving it RUNNING would
    # also leak the sandbox.
    # ANY non-terminal exit releases the slot, not only the tick
    # ceiling. The loop also leaves on an unanswerable question, and
    # that run holds the slot just as firmly — which is how the first
    # five-task baseline ran ONE task and starved four, reporting
    # `never got that far` for four runs that had nothing wrong with
    # them.
    #
    # Cancelled rather than left: cancel is terminal, h1c's trigger
    # releases the slot on the way in, and reconcile's 3b disposition
    # then reaps the container. Leaving it would leak the sandbox too.
    final = await conn.fetchval(
        "SELECT state FROM executions WHERE id = $1", eid)
    if final not in ("DONE", "FAILED", "CANCELLED"):
        await conn.execute(
            "UPDATE executions SET state = 'CANCELLED' WHERE id = $1 "
            "AND state = ANY($2::text[])",
            eid, ["RUNNING", "PENDING", "ADMITTED", "DISPATCHING",
                  "VERIFYING", "PARKED_HOT", "PARKED_COLD"])
        record["stopped_unfinished"] = final
        record["stopped_at_tick_ceiling"] = ticks >= max_ticks
        logger.info("corpus %s: left %s after %d tick(s); cancelled to "
                    "release the admission slot", task["id"], final, ticks)

    record["ticks"] = ticks
    record.update(await measure(conn, eid))
    record["task"] = task["id"]
    logger.info("corpus %s: %s in %d tick(s), first_attempt=%s",
                task["id"], record.get("state"), ticks,
                record.get("first_attempt_success"))
    return record
