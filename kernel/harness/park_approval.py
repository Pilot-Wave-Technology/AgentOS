"""What an approval is bound to, and whether one still stands.

SPLIT OUT OF `park.py` (plan 11.5c). Every question in this file is the
same question asked at a different moment: MAY THIS LEG WRITE? The four
functions are the four moments —

    plan_hash           at register time, what the approval will be bound to
    pin_plan_to_grant   at approval time, binding a standing grant to it
    standing_approval   at leg start, does an unattended grant cover this
    plan_is_approved    at leg start, does anything at all cover this

WHY `plan_hash` BELONGS HERE AND NOT WITH THE KINDS. It is not
vocabulary; it is the IDENTITY the other three compare against.
`standing_approval`'s pin and `pin_plan_to_grant`'s write both test
`content_hash`, and that column holds exactly what this function
returned. Separating the producer from its two consumers is how a hash
quietly stops covering something it used to cover.

NO IMPORTS FROM THE OTHER `park_*` MODULES, on purpose: `park_resolve`
imports THIS (for `pin_plan_to_grant`), so an edge back would be a cycle.
"""

from __future__ import annotations

import hashlib
import json

def plan_hash(steps: list[str], rationale: str, checks=None) -> str:
    """Bind an approval to exactly the plan that was shown.

    `approvals` is first-verdict-wins on a hash, so a plan edited after
    approval no longer matches and cannot inherit the decision.

    F11 put the CHECKS inside the hash, because they are now part of
    what is being approved. Left outside, an executor could register a
    plan, have its checks approved, and then re-register the same steps
    with weaker checks and inherit the verdict — approval of the plan
    reading as approval of a definition of done nobody saw.
    """
    body = json.dumps({"steps": steps, "rationale": rationale,
                       "checks": checks or []},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


# The two shapes of waiting, mapped onto the designed vocabulary:

async def standing_approval(conn, execution_id: int) -> dict | None:
    """The live grant covering this run's plan, or None.

    LIVE means: not revoked, not expired, and pinned to the plan this run
    actually registered. The pin is the whole safety property — a grant
    that matched any plan would let an errand re-plan itself into
    something nobody approved and then run it unattended.

    Read fresh per leg, like `approved_calls` and for the same reason: a
    person can revoke between legs and that has to take effect.
    """
    return await conn.fetchrow(
        """
        SELECT s.id, s.approved_by, s.approved_plan_hash
          FROM kernel.errand_schedules s
          JOIN kernel.executions e ON e.goal_id = s.goal_id
         WHERE e.id = $1
           AND s.approved_by IS NOT NULL
           AND s.revoked_at IS NULL
           AND (s.expires_at IS NULL OR s.expires_at > now())
           AND (s.story_id IS NULL OR s.story_id = e.story_id)
           -- THE PIN. The plan this run registered must be the plan the
           -- human approved, hash for hash. A NULL hash on the grant
           -- matches nothing: an unpinned grant is a blank cheque and is
           -- refused here rather than trusted.
           AND s.approved_plan_hash IS NOT NULL
           AND s.approved_plan_hash = (
                 SELECT q.content_hash FROM questions q
                  WHERE q.execution_id = e.id
                    AND q.kind = 'authorization'
                  ORDER BY q.id DESC LIMIT 1)
         LIMIT 1
        """, execution_id)


async def pin_plan_to_grant(conn, request_id: int) -> int | None:
    """Write the plan a person just approved onto the grant waiting for it.

    THE MISSING HALF OF I5f. Approving a proposal says an errand may run
    unattended; it cannot say WHICH plan, because at that moment the
    errand has never run. So the grant is minted with a NULL hash, which
    `standing_approval` refuses — the feature exists and does nothing —
    until the first plan is approved and lands here.

    ONLY ONCE, AND ONLY WHEN NULL. `approved_plan_hash IS NULL` is the
    whole guard: without it, a re-planned errand would re-pin to its new
    plan on the person's next approval, and "a re-planned errand loses
    its autonomy" — the property I5c is built on — would be false. A
    grant is pinned to the FIRST plan a person approved under it, and a
    different one needs a new grant.

    Scoped exactly as `standing_approval` reads it, or it would pin a
    grant that does not cover this run.
    """
    return await conn.fetchval(
        """
        UPDATE kernel.errand_schedules s
           SET approved_plan_hash = q.content_hash
          FROM questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE q.id = $1
           AND q.kind = 'authorization'
           AND q.content_hash IS NOT NULL
           AND s.goal_id = e.goal_id
           AND s.approved_by IS NOT NULL
           AND s.approved_plan_hash IS NULL
           AND s.revoked_at IS NULL
           AND (s.story_id IS NULL OR s.story_id = e.story_id)
        RETURNING s.id
        """, request_id)


async def plan_is_approved(conn, execution_id: int) -> bool:
    """Has a human approved a plan for this run?

    Approval means an ANSWERED plan_approval request — a declined one is a
    decision too, and it is not permission. Read fresh at the start of each
    leg rather than cached: the approval arrives between legs, which is the
    entire point of parking.
    """
    # I5c — A STANDING GRANT IS THE SECOND SOURCE, and it does not weaken
    # the rule that approving is a human act.
    #
    # The grant row carries `approved_by NOT NULL`, written from an
    # authenticated session, so the human uid is RECORDED ONCE by a
    # person rather than synthesised per run. INSERT on
    # `errand_schedules` is `agentos_human` only, so no agent — GO
    # included — can mint one. `resolve_request` is untouched.
    #
    # PINNED TO ONE PLAN. `plan_hash` covers steps + rationale + CHECKS,
    # so a re-planned errand cannot inherit autonomy: it falls through to
    # the question below and asks. Without that pin an errand could
    # quietly weaken its own definition of done and then mark itself
    # finished, with `selfcheck` — measuring checks no human re-read for
    # THIS run — as the only thing in the way.
    # ── READ THE CYCLE FIRST, THEN THE GRANT ─────────────────────────
    #
    # The grant check used to be the FIRST thing here and returned
    # immediately, which put it in front of both rules below: the
    # `replan` withdrawal and the spend. For a grant-backed errand that
    # made cycle one's approval cover the whole run, however many
    # acceptance cycles followed.
    #
    # THE PIN DID NOT PREVENT IT, though the note above says it does.
    # `plan_hash` only stops a grant covering a DIFFERENT plan, and the
    # plan can only differ once something forces a new one -- which is
    # this gate. Grant short-circuits the gate, so no new plan is ever
    # demanded, so the hash never changes, so the pin never bites. The
    # protection was circular and the loop resolved the wrong way.
    #
    # WATCHED ON ERRAND 282. Plan approved at 09:49 for documenting four
    # endpoints. The acceptance was answered `revise` -- more work -- at
    # 09:51, and the next leg ran a test suite with no `register_plan`
    # anywhere in it, judged against a contract derived from the
    # documentation plan. New work, old definition of done.
    #
    # ORDER, NOT REMOVAL. A grant still authorises unattended running;
    # it just cannot resurrect a cycle that has been closed. Once the
    # errand registers its new plan, the grant's pinned hash will not
    # match it and the run asks -- which is what the note above always
    # said would happen.
    row = await conn.fetchrow(
        """
        SELECT status, declined, verdict, answered_at FROM questions
        WHERE execution_id = $1 AND kind = 'authorization'
        ORDER BY id DESC LIMIT 1
        """,
        execution_id,
    )

    # Whether the cycle this run is in has already been closed, by a
    # withdrawal or by its claim being answered. Both are computed
    # against the approval they would close, so with no answered
    # approval there is no cycle to close and the grant stands alone --
    # which is the case a grant exists for.
    closed = bool(row and row["status"] == "ANSWERED"
                  and await _cycle_closed(conn, execution_id,
                                          row["answered_at"]))

    if not closed and await standing_approval(conn, execution_id):
        return True
    # Declined is answered, and it is not permission. Checking only for
    # ANSWERED would treat "no" as a yes.
    # `revise` is answered and is NOT permission: the person wants a
    # different plan, and treating it as approval would let the work
    # proceed on the plan they just sent back.
    if not (row and row["status"] == "ANSWERED" and not row["declined"]
            and (row["verdict"] or "approve") == "approve"):
        return False

    # Both rules live in `_cycle_closed` now, because the grant check
    # above has to consult them too and a rule written twice is a rule
    # that disagrees with itself the first time one copy is edited.
    return not closed


async def _cycle_closed(conn, execution_id: int, approved_at) -> bool:
    """Has the cycle that approval opened already been closed?

    Two ways, and the caller needs the answer BEFORE it honours a
    standing grant as well as after it validates the approval itself.
    """
    # H1a: a `replan` withdraws the approval it came after.
    #
    # Without this the withdrawal lasts exactly one leg — the executor's
    # in-memory flag is flipped, the next leg re-reads the database, finds
    # the old approval still standing, and the run resumes changing things
    # under a plan the assistant has already retracted. Permission has to
    # be revocable in the same place it is granted.
    withdrawn = await conn.fetchval(
        """
        SELECT 1 FROM proposals
        WHERE execution_id = $1 AND kind = 'replan' AND status = 'ACCEPTED'
          AND created_at > $2
        LIMIT 1
        """,
        execution_id, approved_at,
    )
    if withdrawn:
        return True

    # ── A VERDICT SPENDS THE APPROVAL THAT PRECEDED IT ───────────────
    #
    # An approval authorises ONE cycle of work: this plan, up to the
    # claim it leads to. Once that claim has a verdict the cycle is
    # over, whichever way the verdict went -- refused, or passed and
    # sent back by a person asking for more. What happens next is new
    # work, and new work needs a plan somebody has seen.
    #
    # Without this the first approval stood for the life of the run. A
    # story refused at verification returned to RUNNING and simply
    # carried on writing under a plan that had already been tried and
    # found wanting; a story a person sent back for MORE WORK started
    # that work immediately, with no plan and nothing to approve. Both
    # were watched happening: the executor went straight from the
    # person's instruction to editing files, and the person never saw
    # what it intended to do.
    #
    # The executor is not blocked, only re-gated: `requires_plan`
    # refuses its writes until it registers a plan and a human approves
    # it, which is the same gate as the first cycle and the same
    # mechanism.
    # AN ANSWERED ACCEPTANCE SPENDS IT TOO, AND FOR ERRANDS IT IS THE
    # ONLY THING THAT DOES.
    #
    # The verdict test above is the story path: verification runs, a
    # verdict lands, the cycle is over. An ERRAND IS NEVER VERIFIED --
    # zero verdicts across every errand this environment has run -- so
    # `spent` was always false, the first approval stood for the life of
    # the run, and a person answering the acceptance card with "do more"
    # got the extra work done under the plan that had just been declared
    # finished. No new plan, nothing to approve, straight back to
    # proposing completion. Errand 187: two acceptance questions, no
    # verdict between them.
    #
    # The rule the comment above states is right and its test was too
    # narrow. What ends a cycle is the CLAIM BEING ANSWERED -- by a
    # verifier or by a person -- not by which of those happened to run.
    # An acceptance question that has been answered is that, for both
    # kinds, and for a story it lands at the same moment as the verdict
    # so nothing about the story path changes.
    #
    # `settled == 'approve'` is not excluded on purpose: an acceptance
    # answered with "done" finishes the run in `resolve_request`, so
    # there is no next leg for an approval to still be covering. Testing
    # the verdict of the acceptance here would add a condition that can
    # only ever be true.
    #
    # AND A LANDING, through the one definition `planfiles` reads too
    # (`cycles.py`). A story holding its workspace for the merge finishes
    # on a `landing` park, never an acceptance, so its feature plan was
    # never spent and the next piece of work ran under it.
    #
    # STILL ONE STATEMENT, so a story and an errand cannot diverge.
    from kernel.harness.cycles import CYCLE_ENDING_KINDS
    spent = await conn.fetchval(
        """
        SELECT 1 FROM verdicts
         WHERE execution_id = $1 AND created_at > $2
        UNION ALL
        SELECT 1 FROM questions
         WHERE execution_id = $1 AND kind = ANY($3::text[])
           AND status = 'ANSWERED' AND answered_at > $2
        LIMIT 1
        """,
        execution_id, approved_at, list(CYCLE_ENDING_KINDS))
    return bool(spent)


async def plan_scope(conn, execution_id: int):
    """Which tools the live plan approves, or None for an ordinary plan.

    READ PER LEG, beside `plan_is_approved`, and for exactly the same
    reason: the plan changes between legs and a scope remembered from
    the last one would outlive the approval it came from.

    None means "this plan scopes nothing" — every plan a person wrote.
    That is not the same as an empty set, which would mean approved for
    no tools at all; the gate keeps them apart deliberately, because
    conflating them turns a bricked run into an unguarded one.

    THE NEWEST ANSWERED PLAN IS THE LIVE ONE. A scoped platform plan
    registered after a person's plan supersedes it; a person's plan
    registered after the platform's lifts the scope, which is right —
    they read it and said yes to the whole thing.
    """
    from kernel.harness.integration_plan import scope_of

    row = await conn.fetchrow(
        """
        SELECT payload FROM questions
         WHERE execution_id = $1 AND kind = 'authorization'
           AND status = 'ANSWERED' AND NOT COALESCE(declined, false)
         ORDER BY id DESC LIMIT 1
        """, execution_id)
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return scope_of(payload)
