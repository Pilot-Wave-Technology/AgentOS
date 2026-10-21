"""What the RECORD says about a claim, and then a person decides.

THE HOLE THIS FILLS, originally. An errand has no Verifier by design --
there is no branch to merge and no tree to rebuild -- so
`propose_completion` was simply IGNORED for it: the worker fell through
to the `terminal_command` branch, `start_story` never sets one, and an
errand that finished perfectly ran out of legs and ended FAILED with
"12 legs without converging". A successful errand reported as a model
failure. With verification off by default, every story now finishes on
this path too.

ARTIFACTS ARE MEASURED; COMMANDS ARE REPORTED. An artifact check is a
question about the manifest -- did this run emit something with that
role, does it parse, has it any bytes -- so it is answered here, from
the record, and the executor could not have faked it without emitting
the thing. A command check is not re-run: it ran as a foreground `bash`
pinned to a 120s ceiling, which meant a suite slower than two minutes
could never pass; and running it uncapped would repeat, in the
executor's own sandbox, a suite the executor had just run as its work.
It reports the four counts against a baseline instead, and a person
reads them. Turning the platform verification switch on is what makes
those checks measured again, in a clean container -- that is the whole
job of the switch.

SO THE SUMMARY SAYS WHAT WAS NOT MEASURED. A person accepting this needs
to know the difference between "three checks passed" and "one check
passed and two were taken on the executor's word". Silence there would
be the same lie as a green tick over an unrun suite.

THE PERSON IS STILL THE LAST WORD. Meeting what can be measured parks
the run on an `acceptance`, exactly as a verified story does. Accept
finishes it; decline resumes it with the reason in the brief.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

async def criteria_for(conn, execution_id: int) -> list[dict]:
    """This execution's criteria, in the order they were approved."""
    rows = await conn.fetch(
        """
        SELECT c.id, c.kind, c.spec::text AS spec, c.label
          FROM criteria c JOIN executions e ON e.contract_id = c.contract_id
         WHERE e.id = $1
         ORDER BY c.id
        """, execution_id)
    import json

    out = []
    for r in rows:
        try:
            spec = json.loads(r["spec"] or "{}")
        except (TypeError, ValueError):
            spec = {}
        out.append({"id": r["id"], "kind": r["kind"] or "command",
                    "spec": spec, "label": r["label"] or "unlabelled"})
    return out


async def evaluate(conn, execution_id: int) -> dict:
    """Measure what the record can answer. Never raises.

    Returns `{"ok", "results", "summary"}`. `ok` is False only when an
    artifact check FAILED. A command criterion is `unmeasured`, which is
    neither a pass nor a refusal: nothing ran it here, so it has not
    said the work is wrong, and refusing on it would block every story
    the moment verification is off.

    NO DRIVER, NO ROW. It took both to `exec` a command criterion back
    into the executor's sandbox, and that second run is gone -- the
    executor ran its tests as work and reported the numbers. What is
    left is a question for the database.
    """
    from kernel.verifier import artifact_kind

    criteria = await criteria_for(conn, execution_id)
    if not criteria:
        # NOTHING TO MEASURE. Honest rather than convenient: it is not a
        # pass. The person is asked anyway — they are the last word — but
        # the summary says the claim rests on nothing, which is exactly
        # what they need to know before accepting it.
        return {"ok": True, "results": [],
                "summary": "no checks were registered for this run, so the "
                           "claim rests on the executor's word alone"}

    results = []
    for criterion in criteria:
        if criterion["kind"] == "artifact":
            outcome = await artifact_kind.evaluate(conn, execution_id,
                                                   criterion)
            state = outcome.passes
            detail = outcome.detail.get("reason", "")
        else:
            # NOT RUN, AND SAID SO. `unmeasured` is a third value on
            # purpose: `pass` would be a lie, `fail` would refuse a story
            # over a check nobody ran, and `error` would claim something
            # went wrong when nothing did. The switch being off is not a
            # fault.
            state = "unmeasured"
            detail = ("not measured here -- the executor ran this as its "
                      "own work and reported the result; platform "
                      "verification is off, so nothing re-ran it")
        results.append({"id": criterion["id"], "kind": criterion["kind"],
                        "label": criterion["label"], "outcome": state,
                        "check": (criterion.get("spec") or {}).get("run", ""),
                        "detail": detail})

    # I1g — THE RESULTS HAVE TO LAND SOMEWHERE A READER LOOKS. They went
    # only into the acceptance question's payload, so the ticker's check
    # half — which reads `criterion_results` — was dead for every errand,
    # and `story_progress` showed a check with no observation beside it.
    #
    # `written_by` says WHICH judge spoke. A Verifier's row and a
    # self-check's row are not the same evidence: one ran in a fresh
    # container at the contract's digest under a different uid, the other
    # ran in the executor's own live sandbox. Anybody reading this table
    # to decide something must be able to tell them apart, and a column
    # that says so is cheaper than inferring it from the absence of a
    # claim_seq.
    # ONLY WHAT WAS OBSERVED. `criterion_results.passes` is constrained
    # to pass/fail/flake/error, so a row saying `unmeasured` would be
    # refused by the CHECK and swallowed by `_record`'s try/except --
    # the Stage L shape. It is also a contradiction in terms: this table
    # holds observations, and a check nobody ran produced none. The
    # criterion simply has no row beside it, which is the truth.
    await _record(conn, execution_id,
                  [r for r in results if r["outcome"] != "unmeasured"])

    failed = [r for r in results if r["outcome"] == "fail"]
    errored = [r for r in results if r["outcome"] == "error"]
    passed = [r for r in results if r["outcome"] == "pass"]
    unmeasured = [r for r in results if r["outcome"] == "unmeasured"]

    # WHAT A PERSON IS ABOUT TO ACCEPT, counted honestly. "3 checks
    # passed" over a contract of five, two of which nobody ran, is the
    # green tick this whole design is trying not to hand anybody.
    parts = [f"{len(passed)}/{len(results)} checks passed"]
    if failed:
        parts.append("failed: "
                     + "; ".join(f"{r['check']} ({r['detail'][:120]})"
                                 for r in failed))
    if errored:
        parts.append("could not run: "
                     + "; ".join(r["check"] for r in errored))
    if unmeasured:
        parts.append(
            f"{len(unmeasured)} taken on the executor's word (verification "
            f"is off): " + "; ".join(r["check"] for r in unmeasured))
    return {"ok": not failed, "results": results, "summary": ". ".join(parts)}


async def _record(conn, execution_id: int, results: list) -> None:
    """Append the self-check's observations. Never raises past `evaluate`.

    APPEND-ONLY AND IDEMPOTENT, the same as the Verifier's writer: a
    re-proposed completion re-runs the checks and must not fail on the
    rows the last attempt left. `claim_seq = 0` marks a self-check —
    there is no claim, because nothing adjudicates this.
    """
    import json

    for index, result in enumerate(results):
        try:
            await conn.execute(
                """
                INSERT INTO criterion_results
                    (criterion_id, execution_id, claim_seq, passes,
                     detail, run_index, written_by)
                VALUES ($1, $2, 0, $3, $4::jsonb, $5, 'selfcheck')
                ON CONFLICT (criterion_id, execution_id, claim_seq,
                             run_index) DO NOTHING
                """,
                result["id"], execution_id, result["outcome"],
                json.dumps({"reason": result["detail"][:2000],
                            "check": result["check"],
                            "label": result["label"]}),
                index)
        except Exception:                                   # noqa: BLE001
            # A CLAIM THAT CANNOT BE RECORDED IS STILL A CLAIM. The
            # person is asked either way; losing the evidence row is
            # worse than nothing but far better than refusing a finished
            # errand because a projection table would not take a write.
            logger.exception("selfcheck: could not record criterion %s "
                             "for execution %s", result["id"], execution_id)


def corrective(outcome: dict) -> str:
    """What to tell an executor whose claim did not hold up.

    Names the check and what it saw. "your checks failed" teaches nothing
    and the executor has a bounded number of legs left to work it out.
    """
    failed = [r for r in outcome.get("results", [])
              if r["outcome"] == "fail"]
    lines = ["You proposed completion and your own plan's checks do not "
             "agree yet:"]
    for r in failed:
        lines.append(f"  {r['label']} · {r['check']}")
        if r["detail"]:
            lines.append(f"      {r['detail'][:300]}")
    lines.append("")
    lines.append("Fix the work, or emit what is missing, then propose "
                 "completion again. If a check is WRONG rather than "
                 "unmet, say so — do not work around it.")
    return "\n".join(lines)
