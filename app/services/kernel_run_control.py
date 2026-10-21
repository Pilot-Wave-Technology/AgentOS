"""Pause, resume and cancel the executor working THIS thread's story.

THE THREAD IS THE AUTHORITY, and that is the whole shape of this module. Every
entry point takes a `thread_id` and derives the story from `threads.story_id` —
never a story id the caller supplied. A person pressing Pause in a conversation
means "stop the thing this conversation is about", and accepting an id from the
request body would make the button able to reach any run in the goal, from any
thread, including one somebody else is working. Stage K gave every story its own
thread precisely so this binding exists; this is the first thing to rely on it.

ALMOST NONE OF THIS IS NEW, which is why it can be small. The kernel already
has the whole mechanism:

  pause   `hold` is one of `intake.LOOP_KINDS`, applied at a STEP BOUNDARY —
          the only point where stopping does not split a tool call from its
          result. `park.apply_hold` then parks PARKED_HOT with the sandbox up
          and the journal intact, and opens a `direction` question.
  resume  answering that question un-parks the run, and the answer text is
          carried to the executor by `park._direction_message`, which already
          tells it: you did not ask this, their reply is not permission, and if
          it changes the work then the work needs a new plan.
  cancel  `cancel` is a LOOP_KIND too, and machine v5 has a CANCELLED edge from
          every non-terminal state.

So no new run states, no second resume path, and no way for this to disagree
with the state machine — it reads what the run is in and offers only what the
machine already allows.

WHY PAUSE IS NOT IMMEDIATE. A leg is a model turn and its tool calls. Stopping
one mid-flight would leave a tool call whose result never arrives, which some
providers reject outright and all of them handle differently. So Pause records
an intent and the leg stops itself at its next step boundary — usually seconds,
occasionally the length of one tool. The UI must say "pausing" rather than
"paused" until the state actually moves, because claiming a stop that has not
happened is worse than showing a wait.
"""
from __future__ import annotations

import logging
import uuid

_log = logging.getLogger(__name__)

# What the run must be in for each control to mean anything. Derived from
# machine v5's edges — not a second opinion about them.
PAUSABLE = ("RUNNING",)
RESUMABLE = ("PARKED_HOT", "PARKED_COLD")
TERMINAL = ("DONE", "FAILED", "CANCELLED")

# The question a hold opens. `park.apply_hold` writes this kind, and answering
# it is what resumes the run.
HOLD_KIND = "direction"


class ControlRefused(Exception):
    """A control that cannot apply right now, in words for the person."""


async def _kernel_conn():
    """A connection whose search_path reaches the kernel's unqualified names.

    Same reasoning as `kernel_decide`: the kernel's own functions speak
    unqualified names against the `kernel` schema and the control panel's
    pooled connections do not, so this is passed as a server setting rather
    than mutated on a shared connection.
    """
    from app.services.kernel_conn import kernel_conn

    return await kernel_conn()


async def _thread_story(conn, thread_id: str, goal_id: str) -> tuple[str, str]:
    """(story_id, title) for the story this thread is about.

    Goal-scoped as well as thread-scoped: a thread id is caller-supplied, and
    without the goal check a member of one goal could name a thread in another
    and drive its executor. Mirrors `chat._guard_thread_in_goal`.
    """
    row = await conn.fetchrow(
        "SELECT t.story_id, s.title FROM threads t "
        "LEFT JOIN stories s ON s.id = t.story_id "
        "WHERE t.id = $1 AND t.goal_id = $2", thread_id, goal_id)
    if row is None:
        raise ControlRefused("That conversation is not in this goal.")
    if not row["story_id"]:
        raise ControlRefused(
            "This conversation is not about a story, so there is no executor "
            "to control. Open a story in the assistant first.")
    return row["story_id"], row["title"] or row["story_id"]


async def _live_run(conn, story_id: str) -> dict | None:
    """The story's run that has not finished, or None.

    Newest first: a story amended after finishing goes DONE -> PENDING and gets
    a second execution, and the controls belong to the one that is live.
    """
    row = await conn.fetchrow(
        f"""SELECT id, state, generation, container_id
              FROM kernel.executions
             WHERE story_id = $1 AND state <> ALL($2::text[])
          ORDER BY id DESC LIMIT 1""", story_id, list(TERMINAL))
    return dict(row) if row else None


async def controls_for(thread_id: str, goal_id: str) -> dict:
    """What this conversation may do to its executor right now.

    Returned rather than decided in the template so the buttons and the
    endpoints cannot disagree about when something is allowed — the endpoint
    re-checks on submit, because a page can be seconds stale and the run may
    have moved.
    """
    conn = await _kernel_conn()
    try:
        try:
            story_id, title = await _thread_story(conn, thread_id, goal_id)
        except ControlRefused:
            return {"available": False, "reason": "no story on this thread"}
        run = await _live_run(conn, story_id)
        if run is None:
            return {"available": False, "story_id": story_id,
                    "reason": "no run in flight for this story"}
        held = await conn.fetchval(
            "SELECT id FROM kernel.questions WHERE execution_id = $1 "
            "AND kind = $2 AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            run["id"], HOLD_KIND)
        # WAITING FOR A MERGE IS ITS OWN CONTROL, not a kind of
        # resume. `resume` releases a `direction` hold and carries a
        # steer, which says HOW and leaves the definition of done
        # alone. Asking for more work CHANGES what done means, so it
        # withdraws the plan -- a different act, and offering it
        # under the same button would hide that.
        from app.services.kernel_more_work import available as _landing

        landing = await _landing(conn, run["id"])
        return {
            "available": True,
            "story_id": story_id, "story_title": title,
            "execution_id": run["id"], "state": run["state"],
            "can_pause": run["state"] in PAUSABLE,
            "can_resume": run["state"] in RESUMABLE and held is not None,
            "can_cancel": run["state"] not in TERMINAL,
            "can_ask_more_work": (run["state"] in RESUMABLE
                                  and landing is not None),
            "hold_question_id": held,
            "landing_question_id": landing,
        }
    finally:
        await conn.close()


async def _submit(conn, execution_id: int, kind: str, payload: dict,
                  actor_uid: str) -> None:
    """Put an intent in the run's mailbox. The leg takes it at a boundary."""
    from kernel.mailbox.api import submit_proposal

    got = await submit_proposal(
        conn, f"human:{actor_uid}", kind, payload,
        idempotency_key=f"{kind}:{execution_id}:{uuid.uuid4().hex[:12]}",
        execution_id=execution_id)
    if not isinstance(got, int):
        raise ControlRefused(
            f"The kernel refused that: {getattr(got, 'message', got)}")


async def pause(thread_id: str, goal_id: str, actor_uid: str,
                reason: str = "") -> dict:
    """Stop this thread's executor at its next step boundary."""
    conn = await _kernel_conn()
    try:
        story_id, title = await _thread_story(conn, thread_id, goal_id)
        run = await _live_run(conn, story_id)
        if run is None:
            raise ControlRefused(
                f'"{title}" has no run in flight — there is nothing to pause.')
        if run["state"] not in PAUSABLE:
            raise ControlRefused(
                f"That run is {run['state'].lower().replace('_', ' ')}, not "
                f"working, so there is no leg to stop.")

        await _submit(conn, run["id"], "hold",
                      {"decision": reason.strip() or
                       "A person paused this run from the story conversation.",
                       "why_now": "paused from the assistant thread"},
                      actor_uid)
        _log.info("run control: pause requested by %s for story=%s run=%s",
                  actor_uid, story_id, run["id"])
        return {"ok": True, "execution_id": run["id"], "state": "pausing",
                "detail": ("The executor stops at its next step boundary — "
                           "usually seconds. Stopping mid-step would split a "
                           "tool call from its result.")}
    finally:
        await conn.close()


async def cancel(thread_id: str, goal_id: str, actor_uid: str,
                 reason: str = "") -> dict:
    """End this thread's run for good. Works from every non-terminal state."""
    conn = await _kernel_conn()
    try:
        story_id, title = await _thread_story(conn, thread_id, goal_id)
        run = await _live_run(conn, story_id)
        if run is None:
            raise ControlRefused(
                f'"{title}" has no run in flight — there is nothing to cancel.')

        await _submit(conn, run["id"], "cancel",
                      {"reason": reason.strip() or
                       "Cancelled by a person from the story conversation."},
                      actor_uid)
        _log.info("run control: cancel requested by %s for story=%s run=%s",
                  actor_uid, story_id, run["id"])
        return {"ok": True, "execution_id": run["id"], "state": "cancelling",
                "detail": ("A running leg stops at its next step boundary; a "
                           "parked one ends on the next sweep.")}
    finally:
        await conn.close()


async def resume(thread_id: str, goal_id: str, actor_uid: str,
                 message: str = "") -> dict:
    """Release the hold. A message rides along as a STEER, acted on first.

    THE MESSAGE IS AN INSTRUCTION FROM A PERSON TO THE EXECUTOR, and the
    vehicle for that already exists. `steer` is a LOOP_KIND; `standing_steers`
    reloads every accepted steer at the start of every leg and puts it in front
    of the model as "the most recent thing anyone said about what to do"; and
    `take_proposals` drains the mailbox at the TOP of the step loop, before the
    request is built, for a reason its own comment gives: "a steer that lands a
    step late has already paid for a step of work in the direction it was
    trying to change."

    So a steer submitted while the run is parked is processed on the resumed
    leg's FIRST step, before its first model call. That is the ordering this
    needs — read what the person said, then carry on accordingly.

    WHY NOT JUST PUT IT IN THE ANSWER. The first version did, and it was the
    wrong frame. `park._direction_message` exists for a different trigger:
    somebody asked a question, an answerer answered it from the record, and the
    answer raised a decision the person had to settle. There the reply
    genuinely is not an instruction — nobody was addressing the executor. Here
    a person is speaking to it directly. Carrying that as a decision-about-
    direction understates it, and the executor would be told "this is not a new
    instruction" about something that is exactly that.

    SUBMITTED BEFORE THE UN-PARK, or the leg can start and drain an empty
    mailbox before the steer lands in it.

    A steer says HOW, not WHAT — the definition of done is unchanged. If the
    person's message genuinely changes scope, the executor still owes a new
    plan, and that rule lives where it always did rather than being restated
    here.
    """
    conn = await _kernel_conn()
    try:
        story_id, title = await _thread_story(conn, thread_id, goal_id)
        run = await _live_run(conn, story_id)
        if run is None:
            raise ControlRefused(
                f'"{title}" has no run in flight — there is nothing to resume.')
        qid = await conn.fetchval(
            "SELECT id FROM kernel.questions WHERE execution_id = $1 "
            "AND kind = $2 AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            run["id"], HOLD_KIND)
        if qid is None:
            raise ControlRefused(
                f"That run is {run['state'].lower().replace('_', ' ')} and is "
                f"not holding on a pause. If it is parked on a plan, approve "
                f"the plan instead.")

        # THE STEER GOES IN FIRST. The resumed leg drains the mailbox before
        # its first model call, so anything landing after the un-park would be
        # a step late — and the whole point is that this is read before work
        # continues, not after a step of it.
        steered = bool(message.strip())
        if steered:
            await _submit(conn, run["id"], "steer",
                          {"instruction": message.strip()}, actor_uid)
    finally:
        await conn.close()

    # OUTSIDE the connection above, deliberately: `resolve_request` raises
    # itself to another role inside its own transaction, and `kernel_decide`
    # documents at length why it must not be wrapped in one of ours.
    # WHAT THE UN-PARK ITSELF SAYS. The substance is the steer; this only has
    # to release the hold and point at it, so the two messages the executor
    # reads do not say different things about the same text.
    #
    # Never empty: `decide` substitutes the literal "approved" for an empty
    # answer on an approve verdict — right for a plan, actively wrong here,
    # since the executor would read `WHAT THEY SAID: approved` in a message
    # about a pause nobody approved anything in.
    said = ("A person paused this run and left you an instruction. It is in "
            "front of you as a standing steer — read it and act on it before "
            "you carry on."
            if steered else
            "Carry on. Nothing to change — this was a pause, not a decision "
            "about the work.")

    from app.services.kernel_decide import DecideError, decide
    try:
        out = await decide(question_id=qid, verdict="approve",
                           text=said, human_uid=actor_uid,
                           display_name="")
    except DecideError as exc:
        raise ControlRefused(str(exc)) from exc

    _log.info("run control: resume by %s for story=%s run=%s (steer=%s, "
              "%d chars)", actor_uid, story_id, run["id"], steered,
              len(message.strip()))
    return {"ok": True, "execution_id": run["id"], "state": "resuming",
            "question_id": qid, "steered": steered,
            "detail": ("Resuming — your instruction is a standing steer and "
                       "the executor reads it before its next model call."
                       if steered else
                       "Resuming where it stopped.")}


async def activity(thread_id: str, goal_id: str, after_seq: int = 0,
                   after_check: int = 0, seed: int = 0) -> dict:
    """What is happening on THIS thread's story right now. Read only.

    Same derivation as every control here — the thread names the story, the
    story names its live run — so the ticker cannot show one conversation the
    inside of another's executor.

    TWO FEEDS, because the run has two workers. `steps` is the executor's own
    tool calls. `checks` is the adjudicator's verdicts, which arrive in one
    batch at the end of verification and are the only thing there is to show
    while the executor is stopped. A ticker on `steps` alone went blank for
    the whole of VERIFYING.

    `seed` asks for a short backlog instead of a tail cursor, which is what
    switching the ticker on sends: without it the strip stays empty until the
    executor's next call, and between legs that is long enough to look broken.

    Nothing is written. No message, no wake: a person watching costs two
    indexed queries per poll, and the assistant does not learn about it.
    """
    from app.services import executor_feed as feed

    conn = await _kernel_conn()
    try:
        try:
            story_id, _ = await _thread_story(conn, thread_id, goal_id)
        except ControlRefused:
            return _no_activity(after_seq, after_check)
        run = await _live_run(conn, story_id)
        if run is None:
            return _no_activity(after_seq, after_check)

        if seed:
            steps, last_seq = await feed.steps_tail(conn, run["id"], seed)
        else:
            steps, last_seq = await feed.steps_since(conn, run["id"], after_seq)
        checks, last_check = await feed.checks_since(
            conn, run["id"], after_check)

        return {"available": True, "execution_id": run["id"],
                "state": run["state"],
                "criteria": await feed.criteria_count(conn, run["id"]),
                "steps": steps, "last_seq": last_seq,
                "checks": checks, "last_check": last_check}
    finally:
        await conn.close()


def _no_activity(after_seq: int, after_check: int) -> dict:
    """Cursors are echoed back unchanged, never reset.

    A story between runs answers this for a few seconds. Returning 0 would
    make the browser re-read the whole journal of the NEXT run from its
    first row the moment one starts.
    """
    return {"available": False, "steps": [], "checks": [],
            "last_seq": after_seq, "last_check": after_check}
