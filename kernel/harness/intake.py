"""H1a — the assistant's intents reach a running leg, at a step boundary.

H1 built the mailbox and nothing read it. A `steer` submitted while a story
was running sat at PENDING until the run ended, which is the same as not
having a mailbox: the assistant's whole job is to redirect work that is
*in flight*.

WHY A STEP BOUNDARY AND NOWHERE ELSE (H8c). A steer applied mid-step would
land between a tool call and its result. The model's turn would then
contain a tool call whose result arrives after an unrelated user message,
which is a shape some providers reject outright and all of them handle
differently — and the journal would record a leg that did not happen as
written. The boundary between steps is the only point where the message
list is coherent for every provider.

WHAT THE LOOP APPLIES, AND WHAT IT DOES NOT. `steer`, `replan` and `cancel`
are about a leg that is running, so they are applied here. `answer` is
not: a run with an open question has already parked, so there is no leg to
apply it to. Draining those belongs to the worker, between legs. Leaving
them PENDING is the honest outcome — a proposal this layer silently marked
applied without applying would be worse than one that waits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kernel.mailbox.api import mark_proposal, pending_proposals

logger = logging.getLogger(__name__)

# Proposal statuses. The C1b vocabulary, used as C1b defined it: ACCEPTED
# is applied, SUPERSEDED is "overtaken by something later in the same
# drain" — a steer that arrives behind a cancel was written about a run
# that is about to stop.
ACCEPTED = "ACCEPTED"
SUPERSEDED = "SUPERSEDED"

# Kinds this layer owns. Anything else stays PENDING for a layer that does.
#
# `hold` is here and `question` is NOT, and the split is the whole point.
# A question is answered beside the run, from the record and a copy of
# the tree, and never enters the executor's message list -- a question
# arriving where a steer arrives is read as one more thing to do. What
# CAN reach a running leg is the consequence: a hold, which stops it so
# a person can decide.
LOOP_KINDS = frozenset({"steer", "replan", "cancel", "hold", "ask"})

# `ask` IS THE ONE THAT IS NOT AN INSTRUCTION, and it is here despite
# `answerer/deliver.py` giving a good reason why a question should not
# be: "a question landing in that position is read as one more thing to
# do". That reason was measured -- asked "why did you choose in-memory
# storage?", a model changed the storage -- but it was measured on a
# question sent through the STEER channel, framed as an instruction,
# with nothing saying otherwise.
#
# WHAT THE ANSWERER CANNOT DO. It reconstructs. It reads the record, the
# tree and the journal, and infers what the executor was doing. It
# cannot know what the executor TRIED AND REJECTED, what it is about to
# do, or why it read the situation the way it did -- none of which is
# written down anywhere. For "what are you doing and why", the model is
# the only source, and asking it beats reconstructing it.
#
# THE ANSWERER REMAINS, AS THE FALLBACK. Five of the seven askable
# states have no live leg -- PENDING, ADMITTED, DISPATCHING,
# PARKED_HOT, PARKED_COLD -- and a park ends the leg, so there is
# genuinely no mind to ask. `ask` is for a running leg; everything else
# is still answered beside the run.


@dataclass(frozen=True)
class Intake:
    """What the drain produced: messages to inject, and whether to stop."""

    messages: list[dict] = field(default_factory=list)
    stop: str | None = None
    # A replan withdraws permission NOW, not at the next leg. The database
    # is the durable record (park.plan_is_approved); this is what makes the
    # very next tool call in THIS leg see the withdrawal.
    revoke_plan: bool = False
    stop_reason: str = ""
    applied: list[int] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)
    # What a `hold` is stopping the leg FOR: the question a person
    # asked, the answer they were given, and the decision now in front
    # of them. Carried through rather than re-read, because the leg ends
    # here and the park has to be able to say why.
    hold: dict | None = None

    def __bool__(self) -> bool:
        # Truthy when it changed anything. Not a sentinel whose falsiness
        # contradicts its meaning — H1's Invalid taught that lesson.
        return bool(self.messages or self.stop or self.revoke_plan)

    @property
    def held(self) -> bool:
        """Stopping to ask a person, not stopping for good.

        Both end the leg, and the difference is everything downstream:
        a cancelled run is over, a held one is parked and resumes the
        moment somebody answers.
        """
        return self.stop == "held"


def _ask_message(payload: dict) -> dict:
    """A question, framed so it cannot be mistaken for work.

    EVERY LINE HERE IS LOAD-BEARING, because the failure this has to
    avoid is documented and specific: a model forty steps into doing
    what it is told reads anything in the user position as one more
    thing to do, and one asked why it chose in-memory storage CHANGED
    THE STORAGE. A steer says "act on it"; this has to say the opposite
    at least as clearly, and then give the model somewhere to put the
    answer so it does not express itself by working.

    THE RUNNING JOB IS NAMED EXPLICITLY. This question can arrive
    mid-`bash_wait` -- that is the point of the mailbox peek in
    `_wait_for_job` -- and the model has just been pulled out of a wait
    it did not choose to end. Left to infer, the plausible readings are
    "the job failed" and "I was interrupted, so I should start over".
    Both are wrong and both are expensive.
    """
    who = "A person" if str(payload.get("from") or "").startswith("human") \
        else "Your assistant"
    request_id = payload.get("request_id")
    where = (f" Call it with request_id={request_id}."
             if request_id else "")
    return {
        "role": "user",
        "content": (
            f"## {who} has asked you a question\n\n"
            f"{str(payload.get('question') or '').strip()}\n\n"
            "THIS IS NOT AN INSTRUCTION. Nothing about your task, your "
            "plan, your approved checks or your current approach has "
            "changed, and nothing is being asked of you except an "
            "answer. Do not start work because of it, do not revise "
            "your plan, and do not treat the subject of the question "
            "as a problem to fix. If it turns out a change IS needed, "
            "the person will send a steer — that is a different thing "
            "and it will say so.\n\n"
            f"Answer with the `reply_to_human` tool.{where} Then carry "
            "on exactly where you were.\n\n"
            "IF YOU WERE WAITING ON A BACKGROUND JOB, IT IS STILL "
            "RUNNING. It was not stopped and nothing failed — the wait "
            "was ended early only so this could reach you between "
            "steps. After you answer, call `bash_wait` on the same "
            "`job_id` again. Do not restart the job and do not go "
            "looking for what went wrong; nothing did.\n\n"
            "Answer from what YOU know and the record cannot: what you "
            "have tried, what you ruled out and why, what you were "
            "about to do next. Where you do not know, say so — that is "
            "a useful answer and a confident guess is not."
        ),
    }


def _steer_message(payload: dict) -> dict:
    """The instruction, and what to do when carrying it out needs a new plan.

    A STEER IS NOT LIMITED TO *HOW*. It used to be described that way, and the
    description was doing damage: an instruction that changed the scope had
    nowhere to go, so a model either ignored it as out of bounds or acted on it
    and left the approved plan describing different work. Both are worse than
    the third option, which is what this now says — fold it into a new plan and
    have a person approve THAT.

    Plans stay the authority. The steer is authoritative INPUT; the plan is
    still how a change becomes real. Those are not in tension, and saying both
    is what stops the model choosing one.
    """
    who = "a person" if str(payload.get("from") or "").startswith("human") \
        else "your assistant"
    return {
        "role": "user",
        "content": (
            f"## A new instruction has arrived from {who}\n\n"
            f"{payload['instruction'].strip()}\n\n"
            "Act on it. It supersedes earlier guidance where they conflict, "
            "and it supersedes your own earlier reading of the task. Do not "
            "redo work that already satisfies it; carry on from here.\n\n"
            "IF CARRYING IT OUT CHANGES WHAT DONE MEANS — different scope, "
            "work added or dropped, different criteria — then call "
            "`register_plan` with a plan that INCLUDES it and wait for "
            "approval. That is not you refusing the instruction; it is how an "
            "instruction of that size gets made real. What you must not do is "
            "either of the other two: ignore it because it is bigger than a "
            "tweak, or carry it out while the approved plan still describes "
            "different work."
        ),
    }


def _replan_message(payload: dict) -> dict:
    why = (payload.get("reason") or "").strip()
    return {
        "role": "user",
        "content": (
            "## Your approved plan has been withdrawn\n\n"
            + (f"{why}\n\n" if why else "")
            + "The approval no longer stands, so changes are blocked again. "
              "Call register_plan with a revised plan. Reading and searching "
              "stay open while you work it out."
        ),
    }


async def take_proposals(conn, execution_id: int) -> Intake:
    """Drain the mailbox for this execution. Call at a step boundary only.

    One indexed read per step. Cheap next to a model call, and the
    alternative — draining on a timer or a notification — makes the point
    at which an intent lands depend on wall-clock timing rather than on
    the step count in the journal.
    """
    waiting = await pending_proposals(conn, execution_id)
    if not waiting:
        return Intake()

    messages: list[dict] = []
    applied: list[int] = []
    deferred: list[int] = []
    stop: str | None = None
    stop_reason = ""
    revoke_plan = False
    hold: dict | None = None

    for p in waiting:
        kind, payload = p["kind"], p["payload"]
        if kind not in LOOP_KINDS:
            deferred.append(p["id"])
            continue
        # BEHIND A CANCEL, everything is superseded — recorded that way
        # rather than applied, because it was a real intent and it was
        # overtaken. A HOLD does not supersede what follows: it stops the
        # leg so a person can decide, and a cancel arriving behind one is
        # that person deciding. Only a second hold is redundant.
        if stop == "cancelled" or (stop == "held" and kind == "hold"):
            await mark_proposal(conn, p["id"], SUPERSEDED)
            continue

        if kind == "ask":
            messages.append(_ask_message(
                dict(payload or {}, **{"from": p["actor_id"] or "",
                                       "request_id": p["id"]})))
        elif kind == "steer":
            # WHO from the row, not from the payload: `actor_id` is set by
            # `submit_proposal` and cannot be spoofed by whatever wrote the
            # instruction, whereas a payload field could be. A person's
            # instruction and one the assistant relayed carry different
            # weight, and the executor is entitled to know which it has.
            messages.append(_steer_message(
                dict(payload or {}, **{"from": p["actor_id"] or ""})))
        elif kind == "hold":
            stop = "held"
            hold = dict(payload or {})
            # STAMPED FROM THE ROW, overwriting whatever the payload says.
            # `validate_question` lets a person hold without the answer an
            # agent owes, and the escalation rule two branches above says why
            # that must not key on a payload field: "the executor writes
            # payloads, so a bypass keyed on one would be a bypass it could
            # forge". `actor_id` is set by submit_proposal's caller and no
            # executor tool can set it to a human.
            hold["from"] = p["actor_id"] or ""
            # AND ITS OWN IDENTITY. `open_request` refuses a question whose
            # TEXT matches one already answered — right for an executor asking
            # the same thing twice, wrong for a pause. Every pause carries the
            # same words, so the second one on a run was refused as a duplicate
            # (E_ASK_ANSWERED) and the run carried on. Observed on KER20 run
            # 1714: four holds accepted, one park. A pause is an act, not a
            # question with an answer, and two pauses are two acts.
            hold["proposal_id"] = p["id"]
            stop_reason = (hold.get("decision") or "").strip()
        elif kind == "replan":
            messages.append(_replan_message(payload))
            revoke_plan = True
        elif kind == "cancel":
            stop = "cancelled"
            stop_reason = (payload.get("reason") or "").strip()
        await mark_proposal(conn, p["id"], ACCEPTED)
        applied.append(p["id"])

    if applied or stop:
        logger.info("execution %s took %d proposal(s) at a step boundary%s",
                    execution_id, len(applied),
                    f"; stopping: {stop_reason}" if stop else "")
    return Intake(messages=messages, stop=stop, stop_reason=stop_reason,
                  revoke_plan=revoke_plan, applied=applied,
                  deferred=deferred, hold=hold)


async def apply_cancellation(conn, execution_id: int, reason: str) -> None:
    """RUNNING -> CANCELLED. Declared in machine v2; logged by the trigger.

    Guarded on RUNNING: a run that parked or failed in the same step is
    already somewhere the machine has no cancel edge from, and forcing one
    would write a transition the console would then flag as undeclared.
    """
    from kernel.harness.park import close_open_questions

    cancelled = await conn.execute(
        "UPDATE executions SET state = 'CANCELLED' "
        "WHERE id = $1 AND state = 'RUNNING'", execution_id)
    # AND WHATEVER IT WAS ASKING. An open question is what the inbox,
    # the notifier and the control panel read to decide something needs
    # a person, so a cancelled run that leaves one up puts a card in the
    # thread asking for a decision about work that no longer exists --
    # and leaves no way to dismiss it, because approving a dead run is
    # not a thing.
    if cancelled.endswith(" 1"):
        await close_open_questions(conn, execution_id, reason)
    logger.info("execution %s cancelled: %s", execution_id, reason or "no reason given")


def format_steers_for_prompt(steers: list[dict]) -> str:
    """Render standing amendments to the task, oldest first.

    WHO SAID IT IS PART OF IT. This said "from the assistant" of every steer,
    which stopped being true when a person could leave one on the Resume
    button. An instruction a HUMAN typed and one the assistant relayed carry
    different weight, and attributing the first to the second is the kind of
    quiet misreport that makes a model discount it.
    """
    lines = ["## Standing instructions", ""]
    lines.append("These arrived after the task was written and amend it. "
                 "Where they conflict with the original task, they win; "
                 "where they conflict with each other, the later one wins. "
                 "If one of them changes what DONE means, `register_plan` "
                 "with a plan that includes it — an instruction too big for "
                 "the current plan is a reason to write a new plan, not a "
                 "reason to set the instruction aside.")
    lines.append("")
    # A8. An amendment re-closes the plan gate, and a model reading
    # "the plan is no longer approved" while a forty-minute suite is
    # running will conclude the sensible thing is to kill it and start
    # over. That throws away the one measurement nobody can get back
    # cheaply -- and the instruction usually does not touch it. Killing
    # a job is a decision, and it is the person's: they can say so here.
    lines.append("A standing instruction is not by itself a reason to "
                 "stop a job that is already running. Let it finish and "
                 "read its result — you can still watch it with "
                 "`bash_jobs` and `bash_wait` while a new plan is "
                 "waiting for approval. Kill a job only where one of "
                 "these instructions says to, or where its result has "
                 "plainly stopped being worth waiting for.")
    lines.append("")
    for i, s in enumerate(steers, 1):
        src = ("a person" if str(s.get("actor") or "").startswith("human")
               else "your assistant")
        lines.append(f"{i}. (from {src}) {s['text'].strip()}")
    return "\n".join(lines)


async def standing_steers(conn, execution_id: int) -> list[dict]:
    """Every steer already applied to this execution, oldest first.

    WHY A STEER MUST BE RELOADED EVERY LEG. `take_proposals` injects a
    steer into the leg's in-memory message list, and that list does not
    outlive the leg. It was lost two ways, and the second is the one that
    makes this a design fault rather than a crash-handling gap:

      * the leg it landed in died — the list went with it;

      * the leg ended CLEANLY. The next leg rebuilds its messages from
        the task, the brief and any human answer, and the brief is
        model-written prose that does not carry the steer. So a steer
        given in leg 2 was forgotten in leg 3 even when nothing failed.

    Observed live: a person steered "drop is_zero(), stop after
    subtract(n)". The proposal read ACCEPTED, and the run added
    `is_zero()` two legs later and committed it.

    The model was right: nobody had told it. A steer is a standing
    amendment to the task, not a one-shot message, so it is loaded here
    at every leg start and stays in front of the model for the rest of
    the run.
    """
    rows = await conn.fetch(
        """
        SELECT id, payload->>'instruction' AS text, actor_id
        FROM proposals
        WHERE execution_id = $1 AND kind = 'steer' AND status = 'ACCEPTED'
        ORDER BY id
        """,
        execution_id)
    # `actor_id` rides along so the prompt can say WHO. A person's instruction
    # and one the assistant relayed are not the same weight, and reporting the
    # first as the second is how a model learns to discount both.
    return [{"id": r["id"], "text": r["text"], "actor": r["actor_id"] or ""}
            for r in rows if r["text"]]
