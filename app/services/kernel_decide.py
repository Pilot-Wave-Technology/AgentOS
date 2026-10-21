"""A person's verdict on a parked plan, carried into the kernel.

THE SEAM. `resolve_request` refuses to approve a plan unless the caller
supplies a `human_uid`, and its comment says the argument is "the seam
authentication attaches to, shaped so that attaching it later changes
one argument per call site rather than the control flow". This is that
attachment: the control panel knows who is logged in, so it is the one
caller that can honestly fill it in. Nothing else may -- the assistant
relays a revise or a decline through its own tool and cannot reach
here.

THREE VERDICTS, NOT TWO. `revise` is not `declined`. A person reading a
five-step plan usually thinks *steps 1 and 2 are fine, step 3 should be
X*, and collapsing that into a rejection tells the model to abandon an
approach somebody actually wanted.
"""

from __future__ import annotations

import json
import logging


from app.database import get_pool

log = logging.getLogger(__name__)

VERDICTS = {"approve", "revise", "declined"}


class DecideError(Exception):
    """The verdict could not be applied; the message is for the person."""


async def _rewrite_checks(conn, question_id: int, edited, human_uid: str):
    """Replace the plan's checks with the ones the person approved.

    APPROVING A PLAN YOU HAD TO EDIT FIRST IS STILL APPROVING IT. Before
    this, changing one check meant revising the whole plan -- a full
    re-plan cycle, seven steps re-read, to sharpen one command. So
    nobody did, and a guard that read `test -f README.md` on a file the
    story never touched was approved as written: it passed before the
    work started and would have passed whatever the work broke.

    Written to `questions.checks` BEFORE the question resolves, because
    `adopt_checks` reads that column to build the contract. No new seam:
    the contract is made from the approved plan, and this changes what
    was approved.

    An edited check has no dry-run result -- the run string is new --
    so it adopts with its proposed label and no proof. That is honest
    and already handled: the dry-run has been advisory since F11, and a
    person who rewrote a check is the authority on what it means.
    """
    clean = []
    for c in edited or []:
        run = (c.get("run") or "").strip() if isinstance(c, dict) else ""
        if not run:
            continue                      # a cleared box deletes a check
        label = (c.get("label") or "").strip()
        entry = {"run": run,
                 "label": label if label in ("goal", "guard")
                 else "unlabelled",
                 "edited_by": human_uid}
        # THE DECLARED BUDGET SURVIVES AN EDIT, and it did not until
        # this line -- which makes three writers that rebuilt a check
        # from two fields and dropped the rest.
        #
        #   worker/adopt.py        dropped it at plan approval
        #   console/contract.py    dropped it on the console path
        #   here                   dropped it when a person edited one
        #
        # The first two are fixed and this was missed, which is the
        # argument for `normalise` being the only thing that builds a
        # check: three copies of "a check is {run, label}" is three
        # chances to be wrong about it.
        #
        # EDITING THE COMMAND MUST NOT SILENTLY RETIME IT. A person
        # sharpening `pytest -m slow` into `pytest -m slow -x` did not
        # ask for the forty-minute budget to become the default twenty,
        # and a suite killed at twenty minutes reads as a failing test
        # rather than an edit that changed the clock.
        for key in ("timeout_s", "spec", "expect", "role", "name",
                    "min_bytes"):
            if c.get(key) not in (None, ""):
                entry[key] = c[key]
        clean.append(entry)
    await conn.execute(
        "UPDATE kernel.questions SET checks = $2::jsonb "
        "WHERE id = $1 AND status = 'OPEN'",
        question_id, json.dumps(clean))
    return clean


async def decide(question_id: int, verdict: str, text: str,
                 human_uid: str, display_name: str = "",
                 checks=None) -> dict:
    """Settle one parked question. Returns what happened, for the UI."""
    verdict = (verdict or "").strip()
    if verdict not in VERDICTS:
        raise DecideError(f"unknown verdict {verdict!r}; "
                          f"expected one of {sorted(VERDICTS)}")
    if not (human_uid or "").strip():
        raise DecideError("no signed-in user; approval is a human act")
    if verdict in ("revise", "declined") and not (text or "").strip():
        # A rejection with no reason restarts the same plan. The button
        # asks for the sentence because the model needs it.
        raise DecideError("say what should change -- a revise or a "
                          "decline with no reason gives the executor "
                          "nothing to act on")

    # A DEDICATED CONNECTION, AND NO OUTER TRANSACTION. Both halves
    # matter, and the second cost an afternoon.
    #
    # The kernel's functions speak unqualified names against the
    # `kernel` schema, so the connection has to carry that search_path;
    # the control panel's pooled connections do not. Passing it as a
    # server setting keeps it off the shared pool.
    #
    # And the caller must NOT open a transaction around
    # resolve_request. THE ORIGINAL REASON IS RETIRED AND THE RULE IS
    # KEPT ANYWAY -- both halves matter, so neither is deleted.
    #
    # It was this: `adopt_checks` raised itself to agentos_verifier
    # with `SET LOCAL ROLE` inside its own `conn.transaction()`. Under
    # asyncpg a nested transaction is a SAVEPOINT, and SET LOCAL is
    # scoped to the enclosing TRANSACTION -- so releasing that savepoint
    # did not put the role back. Wrapped, the verifier role leaked into
    # the next statement, which updates `executions`, and a person
    # clicking Approve got `permission denied for table executions`.
    #
    # R1 deleted that role raise along with `_record_baselines`, the
    # only code that needed it. `adopt_checks` is now a `fetchrow` and
    # a call to `new_version`, neither of which opens a transaction or
    # changes role, so the savepoint hazard can no longer occur.
    #
    # KEPT, because a retired reason is not permission to drop a rule.
    # `resolve_request` writes across `questions`, `step_journal` and
    # `executions`, and whether those want one transaction or several
    # is a question about ATOMICITY that nobody has asked -- the old
    # answer was forced by a role leak rather than chosen. Relaxing it
    # is a deliberate change with its own test, not a tidy-up on the
    # way past. Do not delete this rule by citing the paragraph above.
    from app.services.kernel_conn import kernel_conn

    conn = await kernel_conn()
    try:
        row = await conn.fetchrow(
            "SELECT q.status, q.kind, e.goal_id, e.story_id "
            "FROM kernel.questions q "
            "JOIN kernel.executions e ON e.id = q.execution_id "
            "WHERE q.id = $1", question_id)
        if row is None:
            raise DecideError(f"no question {question_id}")
        if row["status"] != "OPEN":
            raise DecideError(
                f"question {question_id} is already {row['status'].lower()} "
                f"-- someone got there first")

        # BEFORE resolving, because resolve_request calls adopt_checks
        # and adopt_checks reads this column. Only on an approval: a
        # revise sends the plan back for the executor to rewrite, and
        # editing checks on a plan being rejected would put words in the
        # mouth of a plan that is about to be replaced.
        edited = None
        if verdict == "approve" and checks is not None:
            edited = await _rewrite_checks(conn, question_id, checks,
                                           human_uid)

        from kernel.harness.park import ApprovalRefused, resolve_request
        try:
            await resolve_request(
                conn, question_id,
                answer=text or ("approved" if verdict == "approve" else ""),
                answered_by=display_name or human_uid,
                declined=(verdict == "declined"),
                verdict=verdict,
                human_uid=human_uid)
        except ApprovalRefused as exc:
            raise DecideError(str(exc)) from exc
    finally:
        await conn.close()

    # The buttons must not come back on reload. Same stamp the
    # diff-review and recovery cards use. On the control panel's own
    # pool: `messages` is its table, not the kernel's.
    async with get_pool().acquire() as cp:
        await cp.execute(
            "UPDATE messages SET card_data = card_data || "
            "jsonb_build_object('decided', $2::text) "
            "WHERE card_data->>'card_type' = 'kernel_park' "
            "AND card_data->>'question_id' = $1",
            str(question_id), verdict)

    log.info("kernel decide: question %s -> %s by %s",
             question_id, verdict, human_uid)
    out = {"ok": True, "question_id": question_id, "verdict": verdict,
           "goal_id": row["goal_id"], "story_id": row["story_id"]}
    if edited is not None:
        # Said back, because a person who rewrote a check should see the
        # set that was actually adopted rather than trust that the box
        # they typed in was read.
        out["checks"] = edited
        log.info("kernel decide: question %s approved with %s edited "
                 "check(s) by %s", question_id, len(edited), human_uid)
    return out
