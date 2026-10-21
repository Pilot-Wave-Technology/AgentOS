"""F11 — approving a plan approves its checks, in one act.

The alternative was two mechanisms: approve the plan here, adopt the
checks somewhere else. That admits a state where the plan is approved
and the checks are not, and the story then works against a definition of
done nobody agreed to — which is the situation F11 exists to end,
arriving by a different road.

So this hangs off `resolve_request`, the single place a park is closed.

WHAT THIS MODULE NO LONGER CLAIMS, AND WHY THE CLAIM HAD TO GO (R1).
It used to say two things happened here and both mattered: the checks
became criteria, AND the dry-run's outcome was written to
`criterion_baselines` so that "a guard that fails at verification is
DEFINITIVELY a regression, because a row says it passed beforehand".

That second sentence is no longer capable of being true, for two
independent reasons, and BOTH have to be repaired before it may be
written again:

  1. NOTHING MEASURES THE PRE-WORK TREE. Plan 5.6 (E9) retired the
     pre-approval dry run — it was a full `verify_claim`, 112.5s on
     execution 208, and execution 203 spent six Jobs on them across five
     generations. `kernel/verifier/dryrun.py` is deleted. Item `park`
     (2e65fcf8) removed the last `blocks_approval` shim. So
     `questions.dryrun` has no writer at all, `json.loads(NULL or "{}")`
     was `{}` on every approval, and `_record_baselines` was inserting
     zero rows — silently, on every plan, for as long as that was true.
  2. NOTHING EVER READ A BASELINE. `baseline_of` lived here with no
     caller anywhere in the tree, and stage-f recorded the same thing
     from the other end: "`criterion_baselines` is written but not yet
     READ by a refusal ... a refusal still says
     `CRITERION_COMMAND_FAILED` for both cases."

So the honest state is: this module turns approved checks into criteria,
and NOTHING here can distinguish "this regressed" from "this never
passed". Execution 211 is what that costs, measured: it reached DONE
with `claim 27 passed` having written no code, because `api/main.py` and
`tests/test_api.py` were already on the branch from execution 210 and
the check passed before the story began. That is exactly the
`decorative` finding — see `kernel/verifier/discriminate.py`, whose
rules survived the phase that ran them.

WHAT WOULD RESTORE IT — and do not restore it any other way. Plan 5.6's
replacement is to run the goal criteria against the BASE COMMIT in the
warm verification pod, which costs seconds once the pod exists. The pod
does not exist: that is item **E19**, unbuilt, and `stage.py` still
calls `build_prepared`. When E19 lands, the writer belongs at
verification time against the base commit, and `discriminate.judge` is
the rule to feed it with. Do NOT reintroduce a pre-approval pass to get
the baseline back — that is precisely the 112.5s-per-approval cost 5.6
measured and refused.

`criterion_baselines` and `questions.dryrun` are deliberately NOT
dropped. Their existing rows are the only surviving record of what the
retired dry runs measured, and E19 wants the same shapes back.
"""

from __future__ import annotations

import json
import logging

from kernel.worker.contracts import new_version

logger = logging.getLogger(__name__)


async def adopt_checks(conn, question_id: int, by: str) -> dict | None:
    """Turn an approved plan's checks into the story's contract.

    Returns None when the plan carried no checks — which is every plan
    registered before F11, and is not an error.

    THE LABEL IS THE ONE A PERSON APPROVED, and now that is the only
    label there is. This used to prefer an `effective_label` the dry run
    settled — a `goal` found already passing was promoted to `guard` and
    the card said so, so the contract had to record what was approved
    rather than what was typed. With no dry run there is no promotion
    and no second opinion: `c["label"]` IS what the person saw on the
    card, because `kernel_park_cards` renders that same field and
    `kernel_decide._rewrite_checks` lets them edit it before approving.

    Do not "simplify" this back into reading `questions.dryrun`. That
    column has no writer (see the module docstring); a reader of it here
    returns `{}` and quietly relabels nothing while looking like it
    still does the work.
    """
    row = await conn.fetchrow(
        "SELECT execution_id, checks::text AS checks "
        "FROM questions WHERE id = $1", question_id)
    if row is None:
        return None
    checks = json.loads(row["checks"] or "[]")
    if not checks:
        return None

    # THE CHECKS GO THROUGH WHOLE, and the two-field rebuild that used to
    # stand here is the reason `timeout_s` never reached a criterion.
    #
    # It read `run` and `label` off each check and dropped everything
    # else, BEFORE `new_version` -- and therefore before `normalise`,
    # which is the function that understands the rest. So for every plan
    # ever approved:
    #
    #   spec        gone, and with it `timeout_s`. Run 262 declared
    #               `timeout_s: 2400`, stored `{"run": ...}`, and its
    #               suite was killed at the 1200s default at 42%.
    #   kind        forced back to `command`. An artifact check became a
    #               shell line that can never pass -- the exact failure
    #               `contracts.normalise` carries a comment block about
    #               having already been paid for once.
    #
    # `normalise` also accepts a check written as a bare STRING, which
    # `c.get` could not, so passing them through is the more forgiving
    # path as well as the correct one.
    #
    # THE LABEL IS STILL THE ONE A PERSON APPROVED: `normalise` reads
    # `c["label"]` and defaults it to `unlabelled` exactly as this did.
    eid = row["execution_id"]
    note = f"checks approved with the plan by {by}"
    contract_id, criterion_ids = await new_version(conn, eid, checks,
                                                   note, by)
    logger.info("execution %s: plan %s approved, contract v%s with %d "
                "check(s), %d criterion(s), no baselines (E19 pending)",
                eid, question_id, contract_id, len(checks),
                len(criterion_ids))
    return {"execution_id": eid, "contract_id": contract_id,
            "checks": len(checks)}
