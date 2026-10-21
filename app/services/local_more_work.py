"""Asking for more work on a local story, at any step until it is done.

THE CONTROL PLANE DECIDES, NEVER THE AGENT, and the rule is one question:
is the current cycle still open? Open means the intent now being added
belongs to the plan already under discussion; closed means the last
intent was delivered and this is a new one.

| Where the story is            | Mode     |
|-------------------------------|----------|
| no plan yet, planning         | expand   |
| plan parked for approval      | expand   |
| executing, submitting         | expand   |
| awaiting_merge                | new      |
| awaiting_closeout, integrated | new      |
| integrating                   | queued   |
| done / reviewed / cancelled   | refused  |
| any errand                    | expand   |

WHY `awaiting_merge` IS NEW AND `submitting` IS NOT. The SUBMIT is the
cycle-ending event -- it is what pushes and opens the pull request, and
after the 2026-10-14 flow revision there is no diff approval after it. A
story still submitting has not delivered yet, so the plan it is working
to is still the live one.

AN ERRAND HAS NO CYCLES AT ALL. No plan is written for one and none ever
will be, so routing it through the gate would park it on a condition it
cannot meet -- the same defect that kept Mark Done greyed out on every
errand. More work on an errand is one more instruction in the same
errand.
"""

from __future__ import annotations

import logging

from app.database import get_pool
from app.services.local_report import report_message

_log = logging.getLogger(__name__)

CLOSED_STATES = ("done", "reviewed", "cancelled", "rejected")

EXPAND, NEW, QUEUED, CLOSED = "expand", "new", "queued", "closed"

# `integrating` is the catch-up taking the trunk in under a plan the
# PLATFORM wrote. Expanding that plan would put a person's intent inside
# a machine-written one; starting a new cycle would race the integration
# submit. So it waits, and `drain_queued` releases it as a new cycle.
_HOLDING_PHASES = ("integrating",)


async def decide(pool, story_id: str) -> str:
    """Which mode this story's next more-work request takes."""
    from app.services.local_cycles import cycle_is_open  # noqa: PLC0415
    from app.services.local_diff import _is_errand       # noqa: PLC0415

    if await _is_errand(pool, story_id):
        return EXPAND
    phase = await pool.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1", story_id) or ""
    if phase in _HOLDING_PHASES:
        return QUEUED
    # ONCE EVERY LANE HAS MERGED, THE STORY IS OVER.
    #
    # Abhishek, 2026-10-14: "more work should always be done before
    # merging -- that is how work happens everywhere." The merge is the
    # commitment, and asking for more afterwards means reopening work
    # that a person has already accepted onto trunk.
    #
    # IT WAS ALSO THE ONLY PATH THAT NEEDED A BRANCH BROUGHT BACK TO
    # LIFE, and bringing one back is where this went wrong twice: the
    # node is reset, the create pass re-stamps `base_sha` with the live
    # trunk tip, and the branch -- never actually re-cut -- is behind by
    # its own merge commit while the bookkeeping says it is level. No
    # pull request opens and no catch-up is offered. Refusing is not a
    # way around that; it is the rule the mechanism was built to bend.
    if await _every_lane_merged(pool, story_id):
        return CLOSED
    # A PARKED CATCH-UP HOLDS TOO, and missing this is what made the
    # first real use of more-work do something useless. The story sits
    # at `awaiting_plan_approval` like any other parked plan, so it took
    # the EXPAND path -- and told the agent to "revise the plan you
    # registered". It did not register that plan: the PLATFORM wrote it,
    # it is about taking a named trunk commit in, and there is nothing
    # in it for an agent to revise. Worse, expanding moved the phase to
    # `planning`, dropping the catch-up out of the state a person could
    # approve it from.
    #
    # The same hold as `integrating`, for the same reason: this person's
    # intent is new WORK, and it belongs to a cycle that starts after
    # the trunk is in.
    if phase == "awaiting_plan_approval" and await _catch_up_parked(pool, story_id):
        return QUEUED
    if await cycle_is_open(pool, story_id):
        return EXPAND
    # NOTHING DELIVERED MEANS NOTHING TO SUPERSEDE. A story with no plan
    # row at all has not finished a cycle -- it has not started one. Read
    # as "closed", the first request on a freshly checked-out story would
    # answer `new`, number its plan `plan-02.md` and tell the agent the
    # last cycle had landed, on a story where nothing has happened yet.
    from app.services.local_cycles import has_any_plan  # noqa: PLC0415
    return NEW if await has_any_plan(pool, story_id) else EXPAND


async def _every_lane_merged(pool, story_id: str) -> bool:
    """Has all of this story's work landed on trunk?

    A story with NO lanes answers False: there is nothing to have
    merged, and a docs story or an investigation must still be able to
    take another round. `ABANDONED` counts as settled -- giving up on a
    lane IS the decision about it -- but a story whose every lane was
    abandoned has landed nothing, so it is not finished either.
    """
    row = await pool.fetchrow(
        "SELECT count(*) AS n, "
        "       count(*) FILTER (WHERE gn.state = 'MERGED') AS merged, "
        "       count(*) FILTER (WHERE gn.state IN ('MERGED', 'ABANDONED')) "
        "                                               AS settled "
        "  FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1", story_id)
    return bool(row and row["n"] and row["settled"] == row["n"]
                and row["merged"] > 0)


async def _catch_up_parked(pool, story_id: str) -> bool:
    """Is the plan awaiting approval the platform's catch-up?

    From the CARD, which is where `plan_kind` has always been recorded --
    the same place `approve_local_plan` reads it to decide the phase.
    """
    import json  # noqa: PLC0415

    from app.services.local_report import latest_card  # noqa: PLC0415

    if not await pool.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story_id):
        return False
    card = await latest_card(pool, story_id, "plan_review")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    return (card or {}).get("plan_kind") == "integration"


async def request_more_work(goal_id: str, story_id: str, what: str,
                            asked_by_uid: str) -> dict:
    """A person asks for more. Returns `{ok, mode, plan_path, phase}`.

    WHY NOT JUST TYPE IT IN THE SESSION. Because the board would not know.
    The phase is what every other reader -- the panel, the GO, the next
    person to open the story -- uses to tell "finished" from "mid-cycle",
    and a story quietly being worked on while it says `awaiting_merge` is
    one nobody can see the state of.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT current_phase, state, title FROM stories "
        " WHERE id = $1 AND goal_id = $2", story_id, goal_id)
    if story is None:
        return {"ok": False, "reason": "story_not_found", "error": "story not found"}
    if (story["state"] or "") in CLOSED_STATES:
        return {"ok": False, "reason": "story_closed",
                "error": f"this story is {story['state']}; reopen it before asking "
                         "for more work"}
    mode = await decide(pool, story_id)
    if mode == CLOSED:
        return {"ok": False, "reason": "already_merged",
                "error": ("this story's work has been merged, so it is "
                          "finished — more work belongs in a new story. Ask "
                          "for it BEFORE merging the pull request and it "
                          "joins this one.")}
    if mode == EXPAND and (story["current_phase"] or "") == "awaiting_plan_approval":
        # Expand, but say so -- rather than quietly stacking a second
        # plan card behind the one they are reading. `decide` runs FIRST
        # so a parked CATCH-UP is held instead of being "revised".
        return await _expand(pool, story_id, what, asked_by_uid, parked=True)

    if mode == QUEUED:
        return await _queue(pool, story_id, what, asked_by_uid)
    if mode == EXPAND:
        return await _expand(pool, story_id, what, asked_by_uid)
    return await _new_cycle(pool, goal_id, story_id, what, asked_by_uid)


async def _expand(pool, story_id: str, what: str, uid: str,
                  *, parked: bool = False) -> dict:
    """Same cycle, same plan file, a revised plan.

    THE GATE CLOSES AGAIN. Whatever the agent was doing, the definition of
    done has changed and the plan it was working to no longer describes
    it -- so the story goes back to `planning` and source edits are
    refused until the revised plan is approved. This is the local shape of
    what `kernel_more_work` does in a pod, and for the same reason it
    learned it: an executor asked for more work mid-run walked straight
    past an already-open plan gate and judged new work against the old
    plan's definition of done.
    """
    from app.services.local_cycles import cycle_plan_path  # noqa: PLC0415
    from app.services.local_diff import _is_errand         # noqa: PLC0415

    errand = await _is_errand(pool, story_id)
    phase = await pool.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1", story_id) or ""
    plan_path = "" if errand else await cycle_plan_path(pool, story_id)

    # An errand keeps its phase: it has no plan gate to close and no plan
    # to revise. A story in `discovery` keeps its too -- it has not been
    # scoped yet, so this is part of the brief rather than a revision.
    if not errand and phase != "discovery":
        await pool.execute(
            "UPDATE stories SET current_phase = 'planning', updated_at = NOW() "
            " WHERE id = $1", story_id)
        phase = "planning"

    await report_message(story_id, "human", _instruction(what, errand, parked,
                                                         plan_path),
                         sender=uid or "")
    await _record(pool, story_id, EXPAND, uid, plan_path)
    _log.info("local more-work story=%s mode=expand errand=%s by=%s",
              story_id, errand, uid)
    return {"ok": True, "mode": EXPAND, "plan_path": plan_path, "phase": phase}


async def _new_cycle(pool, goal_id: str, story_id: str, what: str,
                     uid: str) -> dict:
    """The last cycle delivered, so this is a new one.

    THE STORY COMES BACK OUT OF ITS WAIT. It is at `awaiting_merge` with a
    pull request open; the new work goes on the same branch and updates the
    same PR, so nothing is withdrawn -- but the story is being worked again
    and `done` must stop being reachable, which is what moving it off
    `awaiting_merge` does.
    """
    from app.services.local_cycles import cycle_plan_path  # noqa: PLC0415

    plan_path = await cycle_plan_path(pool, story_id)

    # ── A LANE WHOSE PULL REQUEST WAS CLOSED COMES BACK TO LIFE ──────
    #
    # Asking for more work IS the decision to carry on, and this is the
    # moment to act on it. The sweep that reported the closed PR left
    # the node `ESCALATED` deliberately: reviving it then would have
    # had the engine open a fresh pull request within a minute of a
    # person deliberately closing one.
    #
    # THE SAME BRANCH, and nothing needs re-cutting -- the work never
    # merged, so the branch still carries it and its base never moved.
    # That is also why this is not `reopen_for_next_cycle`: there is no
    # round to archive and no `base_sha` to re-stamp, which is exactly
    # where reviving a MERGED lane goes wrong.
    #
    # `pr_number` HAS TO GO. `_ensure_pr_open` pins it only when it is
    # NULL, so a node still carrying the closed PR's number would never
    # be given a new one.
    await pool.execute(
        "UPDATE git_nodes SET state = 'IN_PROGRESS', pr_number = NULL, "
        "       last_error = NULL, updated_at = now() "
        " WHERE id IN (SELECT git_node_id FROM git_node_stories "
        "               WHERE story_id = $1) "
        "   AND state = 'ESCALATED' "
        "   AND last_error LIKE 'PR closed unmerged%'", story_id)


    await pool.execute(
        "UPDATE stories SET state = 'in_progress', current_phase = 'planning', "
        "       updated_at = NOW() WHERE id = $1", story_id)
    await report_message(
        story_id, "human",
        (f"More work on this story: {what.strip()}" if (what or "").strip()
         else "More work on this story, please.")
        + "\n\nThe last cycle has landed, so this needs a NEW plan: register "
          f"it with `register_plan` (it becomes `{plan_path}`) and I will "
          "approve it before you start.",
        sender=uid or "")
    await _record(pool, story_id, NEW, uid, plan_path)
    _log.info("local more-work story=%s mode=new path=%s by=%s",
              story_id, plan_path, uid)
    return {"ok": True, "mode": NEW, "plan_path": plan_path, "phase": "planning"}


async def _queue(pool, story_id: str, what: str, uid: str) -> dict:
    """Held until the catch-up finishes.

    RECORDED, NOT REFUSED. The person asked while the platform happened to
    be taking the trunk in; telling them "not now" would make the answer
    depend on a background sweep's timing. The instruction is posted to
    the thread, which is where the session reads it from when the hold
    lifts.
    """
    await report_message(
        story_id, "agent",
        "Noted, and held: the story is taking the trunk in right now. This "
        f"starts a new plan as soon as that finishes.\n\n> {what.strip()}",
        card_data={"kind": "more_work_queued", "instruction": what.strip(),
                   "asked_by": uid or ""})
    await _record(pool, story_id, QUEUED, uid, "")
    _log.info("local more-work story=%s mode=queued by=%s", story_id, uid)
    phase = await pool.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1", story_id) or ""
    return {"ok": True, "mode": QUEUED, "plan_path": "", "phase": phase}


async def drain_queued(pool, goal_id: str, story_id: str) -> int:
    """Release instructions held during a catch-up. Returns how many.

    THE OTHER HALF OF `queued`, AND IT DID NOT EXIST. `_queue` recorded
    the instruction and a comment here said "`drain_queued` releases it
    as a new cycle" -- a function nobody wrote. So an instruction given
    while the platform was taking the trunk in was acknowledged to the
    person, written to the thread, and then silently never acted on.

    CALLED WHEN THE HOLD LIFTS -- the catch-up's own submit, which is
    the moment the story stops integrating. Each held instruction is
    replayed as an ordinary more-work request, so it takes whatever mode
    is right THEN rather than the one that would have applied when it
    was given: by now the catch-up has landed and the cycle is spent, so
    it starts a new plan, which is what the person asked for.

    IDEMPOTENT. The card is marked drained before the request is made,
    so a retried submit does not replay the same instruction twice --
    two plans for one ask is worse than none.
    """
    import json  # noqa: PLC0415

    rows = await pool.fetch(
        "SELECT id, card_data FROM messages WHERE thread_id = "
        "  (SELECT active_thread_id FROM stories WHERE id = $1) "
        "  AND card_data->>'kind' = 'more_work_queued' "
        "  AND COALESCE(card_data->>'drained', '') = '' "
        " ORDER BY created_at", story_id)
    released = 0
    for row in rows:
        card = row["card_data"]
        card = json.loads(card) if isinstance(card, str) else dict(card or {})
        what = (card.get("instruction") or "").strip()
        await pool.execute(
            "UPDATE messages SET card_data = card_data || '{\"drained\": true}'::jsonb "
            " WHERE id = $1", row["id"])
        try:
            out = await request_more_work(goal_id, story_id, what,
                                          card.get("asked_by") or "")
        except Exception:                                   # noqa: BLE001
            _log.exception("local more-work: could not release a held "
                           "instruction on %s", story_id)
            continue
        if out.get("ok"):
            released += 1
            _log.info("local more-work released on %s as %s",
                      story_id, out.get("mode"))
    return released


def _instruction(what: str, errand: bool, parked: bool, plan_path: str) -> str:
    asked = (what or "").strip()
    if errand:
        return f"More on this errand: {asked}" if asked else (
            "More on this errand, please.")
    head = f"More work on this story: {asked}" if asked else (
        "More work on this story, please.")
    if parked:
        return (head + "\n\nThis belongs to the plan you have already "
                f"registered. Revise it with `register_plan` -- same plan "
                f"(`{plan_path}`), covering this too -- and I will approve "
                "the revised one.")
    return (head + "\n\nThis is part of the SAME cycle, so it goes in the "
            f"same plan (`{plan_path}`). Stop, revise the plan with "
            "`register_plan` to cover it, and wait for my approval before "
            "carrying on. Commits you have already made stay.")


async def _record(pool, story_id: str, mode: str, uid: str,
                  plan_path: str) -> None:
    """Provenance, through the ledger's existing phase event.

    NO NEW EVENT TYPE. The vocabulary lives in `agentos_ledger_client`,
    which this plan does not edit, so the mode rides in the payload of the
    event that is already true -- a more-work request always changes or
    pins the phase. `story.more_work.*` as its own type is deferred to
    whenever that package is next opened.
    """
    try:
        from app.ledger.actors import human_actor                # noqa: PLC0415
        from app.ledger.journal import append_event              # noqa: PLC0415
        from app.ledger.schema import STORY_PHASE_CHANGED        # noqa: PLC0415

        actor, actor_type = await human_actor(uid)
        async with pool.acquire() as conn:
            await append_event(
                conn, actor_id=actor, actor_type=actor_type,
                event_type=STORY_PHASE_CHANGED,
                payload={"story_id": story_id, "more_work": mode,
                         "plan_path": plan_path, "actor_uid": uid})
    except Exception:                                            # noqa: BLE001
        # BEST EFFORT, LOUDLY. Losing the audit line must not lose the
        # person's instruction, which is already written by the time we
        # get here.
        _log.exception("local more-work: ledger write failed for %s", story_id)
