"""The GO carries a person's decision on a parked plan.

WHAT THIS IS NOT. It is not an agent approving. `resolve_request`'s gate
has never been "the caller must be human" -- it is "an approval must
carry a human uid", and the refusal says so: *"approving requires a
human uid and this call supplied none"*. `cancel_story` has relied on
exactly that since it was written, and ends somebody's work on it.

WHAT WAS ACTUALLY MISSING was a verb. An autonomous errand parks on its
plan, the GO is the only agent watching, and the person who approved the
errand has gone away by design. The GO could read the plan, explain it,
and then had nothing to do but tell the person to find the card
themselves -- on a thread they may not have open, for a run they
delegated precisely so they would not have to. The first run of every
autonomous errand stalled there.

THE RULE THIS ENFORCES, and it is the whole safety story: **the GO must
ask a person and be answered before it calls this.** Three checks make
that more than an instruction --

  `human_uid` IS REQUIRED ON ALL THREE VERDICTS, including revise and
  decline, which the kernel would accept from an agent alone. Not
  because a decline is dangerous, but because the rule is "ask first"
  and a verb that needs no name for two of its three values teaches the
  opposite.

  THE UID MUST BE A GOAL ADMIN. Without a check of some kind,
  `human_uid` is a free string an agent fills in -- exactly the hole
  `resolve_request`'s own docstring warns about `answered_by` being.
  `goal_members` is readable from here, so the claim is checkable, and
  an unverifiable name is refused rather than trusted. ADMIN rather
  than member because a plan approval authorises an executor to act
  unattended -- and ANY admin, because waiting for one named person is
  how an errand ends up parked over a weekend.

  `said` IS REQUIRED AND BECOMES THE ANSWER. It is what the person
  actually said, in their words, and it lands in the record as the
  answer the executor reads. A relay with nothing in it is a relay of
  nothing.

AND THE RECORD SAYS IT WAS RELAYED. `answered_by` is `go`, `human_uid`
is the person. A reader can always tell a decision somebody clicked from
one the GO carried, which matters most on the day somebody asks who
approved a plan that turned out badly.

WHAT IT STILL CANNOT DO. Approve anything that is not an autonomous
errand's plan -- `go_may_drive` refuses a story, a hand-started errand
and a revoked grant -- and approve a question that is not this story's.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

VERDICTS = ("approve", "revise", "decline")

# `resolve_request` spells a refusal `declined`; the tool says `decline`
# because that is the word a person uses. Mapped here rather than in the
# prompt, where it would be one more thing to get right under pressure.
_WIRE = {"approve": "approve", "revise": "revise", "decline": "declined"}


async def _is_admin(conn, goal_id: str, uid: str) -> bool:
    """Is this uid a GOAL ADMIN -- somebody who may decide this?

    ADMIN AND NOT MERELY A MEMBER. A plan approval authorises an
    executor to act unattended, and the rule is "any goal admin can
    answer to move it along" -- which cuts both ways: it is not only the
    person who approved the errand, and it is not anybody who happens to
    be in the goal.

    ANY admin, deliberately. Waiting for one named person is how an
    autonomous errand ends up parked over a weekend, which is the
    failure this whole verb exists to remove.
    """
    return bool(await conn.fetchval(
        "SELECT 1 FROM public.goal_members "
        " WHERE goal_id = $1 AND uid = $2 AND role = 'admin'",
        goal_id, uid))


async def _open_question(conn, story_id: str, question_id: int):
    """The question, if it is open and belongs to this story's run."""
    return await conn.fetchrow(
        "SELECT q.id, q.kind, q.status, e.story_id "
        "  FROM questions q JOIN executions e ON e.id = q.execution_id "
        " WHERE q.id = $1 AND e.story_id = $2", question_id, story_id)


async def decide_plan(conn, goal_id: str, story_id: str, question_id: int,
                      verdict: str, human_uid: str,
                      said: str = "") -> dict | Refused:
    """Relay a person's decision on this errand's parked question."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        return Refused("E_UNKNOWN_VERDICT",
                       f"{verdict!r} is not a decision. Use one of: "
                       f"{', '.join(VERDICTS)}.")

    human_uid = (human_uid or "").strip()

    said = (said or "").strip()
    if not said:
        return Refused(
            "E_NOTHING_SAID",
            "`said` is what the person told you, and the executor reads "
            "it as the answer. A relay with nothing in it relays nothing "
            "-- on a revise especially, it is the only thing telling the "
            "executor what to change.")

    # ── whose decision is this? ──────────────────────────────────────
    #
    # TWO MODES, and the caller chooses by supplying a uid or not.
    #
    #   with `human_uid`   a RELAY. Unchanged in every respect: the uid
    #                      must be a goal admin, the record carries both
    #                      names, and this is still the only way to
    #                      settle anything that is not an autonomous
    #                      errand's plan.
    #
    #   without            the GO's OWN decision, allowed only where a
    #                      person has already consented to this errand
    #                      running unattended, and bounded so a re-plan
    #                      loop cannot run for ever. `self_decide` reads
    #                      the grant at the moment of use, so a
    #                      revocation stops this immediately.
    #
    # Dispatch has already refused a story and a hand-started errand
    # (`go_may_drive` in `kernel/assistant/api.py`), so what reaches here
    # without a uid is an autonomous errand or nothing.
    if not human_uid:
        from kernel.go.self_decide import alone_ok

        eid = await conn.fetchval(
            "SELECT execution_id FROM questions WHERE id = $1", question_id)
        why_not = await alone_ok(conn, eid) if eid else "no run to decide on"
        if why_not:
            return Refused("E_MUST_ASK_A_PERSON", why_not)

    if human_uid and not await _is_admin(conn, goal_id, human_uid):
        return Refused(
            "E_NOT_AN_ADMIN",
            f"{human_uid!r} is not a goal admin on {goal_id}. Approving "
            f"a plan authorises an executor to act unattended, so the "
            f"decision has to belong to somebody who may make it -- and "
            f"a uid nobody can check is not a name, it is a guess with a "
            f"person's shape. ANY admin of this goal can answer; it does "
            f"not have to be whoever approved the errand.")

    row = await _open_question(conn, story_id, question_id)
    if row is None:
        return Refused(
            "E_NO_QUESTION",
            f"question {question_id} is not a question of {story_id}. "
            f"Read `inbox` or `story_progress` for the one it is parked "
            f"on.")
    if row["status"] != "OPEN":
        return Refused(
            "E_ALREADY_SETTLED",
            f"question {question_id} is {row['status'].lower()} -- "
            f"somebody has already answered it, very likely the person "
            f"you were talking to.")

    from kernel.harness.park_resolve import ApprovalRefused, resolve_request

    try:
        await resolve_request(
            conn, question_id, said,
            answered_by="go",
            declined=(verdict == "decline"),
            verdict=_WIRE[verdict],
            human_uid=human_uid,
            # NOT A FLAG THE KERNEL TRUSTS. It says "no person is behind
            # this one"; `resolve_request` then re-checks the grant
            # itself before letting an approval through without a uid.
            # A caller asserting its own authority is the hole H8a was
            # written about.
            go_autonomous=not human_uid)
    except ApprovalRefused as exc:
        # The kernel's own gate, reported verbatim. It is narrower than
        # this function in places and must stay the last word.
        return Refused("E_APPROVAL_REFUSED", str(exc))

    logger.info("go decide_plan: q=%s %s %s (story=%s)", question_id,
                verdict, f"for {human_uid}" if human_uid else "ALONE",
                story_id)
    if human_uid:
        return {"question": question_id, "verdict": verdict,
                "on_behalf_of": human_uid, "kind": row["kind"],
                "detail": "recorded as relayed by you on their behalf, so "
                          "the ledger shows both names. The run picks it up "
                          "on its next sweep."}
    return {"question": question_id, "verdict": verdict,
            "decided_by": "go", "kind": row["kind"],
            # SAY IT ON THE THREAD, and the tool says so rather than the
            # prompt, because a rule that lives only in a prompt is a
            # rule that is followed most of the time. The answer itself
            # is posted to the errand's thread by the answer-card poller
            # -- what this asks for is the sentence a person can read
            # without opening the card: what you decided and why.
            "detail": "recorded as YOUR decision -- no person saw this "
                      "plan. Post a short note on the goal thread saying "
                      "what you approved or sent back and why, so somebody "
                      "can disagree before the work is done rather than "
                      "after."}
