"""H1 — the three intent vocabularies, and what makes each one well-formed.

    assistant -> proposals    steer · answer · cancel · replan
    executor  -> questions    clarification · decision · authorization
    Conductor -> directives   dispatch · park · resume · cancel

Three tables have existed since C1b with nothing writing to `directives` or
`questions` at all. This is the validation that makes them usable, and
H1's criterion is exactly it: **schema validation refuses a malformed
proposal.**

THE ASK CONTRACT IS THE INTERESTING ONE. §1.6's rule is "the ask is
evidence, not a claim": a question must carry `attempts[]` — at least three
DISTINCT tool-call signatures — or it is `E_ASK_PREMATURE`, and it must
carry a `proposed` default, which turns a human's interrupt from *compose
an answer* into *yes / no / actually this*.

That floor is not decoration. E6i shipped `ask_human` with no floor at all,
and in the first live story the model asked twice with no way to tell
whether it had tried anything first. A question that costs a human's
attention should have to show it earned it.

Validation returns a typed code rather than raising: these arrive from a
model, and a refusal it can read and correct is worth more than a
traceback.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── vocabularies ─────────────────────────────────────────────────────────
# `question` is the only one that ASKS rather than tells. Every other
# kind here makes the executor do something; a person who merely wanted
# to know something -- why this shape, does it handle X, what did you
# assume -- had no way to reach the work, and phrasing it as a steer
# made it act instead of answer. Asked why it had chosen in-memory
# storage, an executor went and changed the storage.
#
# `hold` is what an ANSWER may cost, and it is the answerer's alone --
# an assistant cannot submit one, because the assistant has not read
# the tree and is not the thing that found the problem. It stops the
# work and puts a decision to a person. Note the direction: answering
# can narrow what the executor may do and can never widen it. There is
# deliberately no kind here that starts work, lifts a gate, or turns a
# question into permission.
PROPOSAL_KINDS = frozenset({"steer", "answer", "cancel", "replan",
                            "question", "ask", "hold"})

# `ask` AND `question` ARE THE SAME INTENT, ROUTED DIFFERENTLY, and the
# difference is where the answer comes from rather than what is wanted.
#
#   ask       -> the EXECUTOR answers, from its own head, at a step
#                boundary. Only meaningful while a leg is live.
#   question  -> the ANSWERER answers, beside the run, from the record
#                and the tree. Works in every askable state.
#
# Neither is an instruction and neither may change the work. `ask` earns
# its place because the answerer reconstructs and the executor knows:
# what was tried and rejected, what comes next, why an error was read
# the way it was -- none of that is written down anywhere.
#
# ROUTE, DO NOT GUESS. `route_question` below picks from the run's
# state, so a caller asks one thing and the platform decides who is in
# a position to answer.

# H8a - how an assistant's `answer` may settle a PLAN approval.
#
# `approve` is ABSENT, and that absence is the guard. S1.3 reserves
# approval to a human uid and says an agent token is "refused at the
# router" - but the mailbox exists precisely so the assistant does not
# hold a connection, so nothing it submits ever passes a router. Rather
# than bolt an actor check onto a vocabulary that can express the act,
# the vocabulary simply cannot express it: the same argument as
# `propose_completion` being absent from the errand surface rather than
# discouraged in a prompt.
#
# Declining and revising stay, and are the point of the relay. A person
# saying "nearly right, change step 3" through the assistant is the
# thing this path is for, and neither is permission.
ANSWER_VERDICTS = frozenset({"revise", "declined"})
# `direction` is the only one the EXECUTOR never asks. It is opened on
# its behalf when answering a person's question turned up something that
# has to be decided before more work happens -- so the run is parked on
# a question it did not write, and resumes the moment somebody answers.
QUESTION_KINDS = frozenset({"clarification", "decision", "authorization",
                            "escalation", "acceptance", "direction",
                            # Ec3b: one destructive CALL, shown concretely.
                            # Its own kind rather than an `authorization`
                            # with a flag, because the executor writes
                            # payloads and a bypass keyed on one would be
                            # a bypass it could forge.
                            "permission",
                            # Waiting for the work to reach trunk.
                            # Opened by the harness, resolved by the
                            # merge, never by a person.
                            "landing",
                            # Merged. Done, or more work?
                            "closeout",
                            # Closed without merging. Now what?
                            "pr_closed"})
DIRECTIVE_KINDS = frozenset({"dispatch", "park", "resume", "cancel"})

# WHICH KINDS A PERSON MUST ANSWER, in one place because it was in five
# and three of them were wrong.
#
# `park.resolve_request` enforces it, `kernel_park_cards.build_card`
# renders buttons from it, the parked-questions listing marks rows with
# it, and the assistant's inbox and notifier both flag cards by it. Those
# last three each carried their own `("authorization", "acceptance")` —
# omitting escalation, direction and, after Ec3b, `permission`. So a
# destructive-command park showed as needing nobody in the inbox, in the
# notifier and in the listing, while the card builder knew better.
#
# It lives HERE rather than in the control panel because it is a fact
# about the question kinds declared six lines up, and a fact about
# kernel data that the kernel does not own is a fact that drifts.
#
# `clarification` is deliberately absent: an assistant may answer one,
# which is what makes `ask_human` cheap. `permission` is present and is
# the sharpest case — an agent able to answer its own "may I run
# terraform destroy" is no gate at all.
HUMAN_ANSWERED_KINDS = frozenset({
    "authorization", "acceptance", "escalation", "direction", "permission",
    # Done-or-more-work, asked once the work is actually on trunk.
    # The same decision `acceptance` protects and for the same
    # reason: it is the one a person deliberately kept.
    "closeout",
    # Closing a PR without merging it is a person's act, so only a
    # person can say what they meant by it. Guessing is expensive in
    # both directions: cancel a story somebody meant to keep, or hold a
    # pod for one they abandoned.
    "pr_closed",
    # THE MERGE CARD, and it is a person's for an unusual reason: the
    # merge itself happens on the forge and resolves this park without
    # anybody touching the card. What the card is FOR is the other
    # answer -- "actually, more work" -- and that one only a person can
    # give. A card with no controls would leave them reading a PR with
    # no way to say no to it.
    "landing"})


def needs_a_person(kind: str, go_driven: bool = False) -> bool:
    """Does this question actually require a HUMAN, here and now?

    THE SET ABOVE IS NO LONGER THE WHOLE ANSWER, and finding that out
    cost a live run. On 2026-09-24 the GO gained the authority to settle
    an AUTONOMOUS errand's plan alone. The authority landed, the prompt
    landed -- and this label did not, so `inbox` still reported
    `needs_human: true` on a plan the GO was now allowed to decide. It
    read its own inbox, believed it, and told the person "the system has
    marked this one as needing a person, so I haven't decided it".

    It was right to. The data said so. That is the failure worth
    remembering: the agent behaved correctly and the platform lied to
    it, which looks exactly like the agent being timid.

    ONLY GO's `inbox` PASSES `go_driven`, and that is deliberate. This
    field is overloaded: it answers "is a person the only one who may
    answer?", and the CARD RENDERER uses it for a different question --
    "should I draw controls?". Passing `go_driven` to a person-facing
    surface therefore strips the buttons off a plan card, so a person
    could no longer approve a plan the GO merely MAY approve. Both must
    be able to. The card builders and the parked-questions route call
    this with the kind alone.

    `go_driven` means an autonomous errand -- work_kind='errand' with a
    live grant, the same predicate `kernel/go/drivable` owns. Callers
    that cannot cheaply establish it pass False, which is the SAFE
    default: the question is reported as a person's, which is what it
    was before this existed.

    ONLY `authorization` IS AFFECTED. An acceptance, a closeout, an
    escalation, a direction and a permission stay a person's on every
    run -- the acceptance and the closeout especially, because
    done-or-more-work is the decision that was deliberately kept.
    """
    if kind == "authorization" and go_driven:
        return False
    return kind in HUMAN_ANSWERED_KINDS

# §1.6: at least three DISTINCT attempts before a question is earned.
MIN_ATTEMPTS = 3


@dataclass(frozen=True)
class Invalid:
    """A typed refusal the sender can act on.

    Deliberately TRUTHY. The first version defined __bool__ returning False
    so that `if not invalid` would read nicely, and that one flourish
    disabled every guard in this module: each caller does
    `bad = validate(...)` then `if bad: return bad`, which silently stopped
    firing. Every malformed input was accepted, and the tests caught it
    only because they asserted on the type rather than on truthiness.

    A sentinel whose falsiness contradicts its meaning is a trap. `None`
    means valid; an Invalid means invalid, and it is true.
    """

    code: str
    message: str


def validate_proposal(actor_id: str, kind: str, payload: dict,
                      idempotency_key: str,
                      execution_id: int | None = None) -> Invalid | None:
    """An assistant's proposal. Returns None when well-formed."""
    if execution_id is None:
        # S1.3 always said "execution_id required" and nothing checked
        # it. `questions` and `directives` both require their execution;
        # this was the one vocabulary that did not, and the gap reached
        # further than a missing column: the idempotency index could not
        # be scoped by story, so one key across three stories was ONE
        # proposal and two of the three calls reported a success they had
        # not performed.
        return Invalid(
            "E_NO_EXECUTION",
            "a proposal must say which story it is about; an intent with "
            "no story cannot be delivered, because every delivery path "
            "selects by execution")
    if not actor_id:
        return Invalid("E_NO_ACTOR", "a proposal must say who is proposing")
    if not idempotency_key:
        return Invalid(
            "E_NO_IDEMPOTENCY_KEY",
            "a proposal must carry an idempotency key — a retried steer that "
            "lands twice is two steers")
    if kind not in PROPOSAL_KINDS:
        return Invalid(
            "E_UNKNOWN_KIND",
            f"{kind!r} is not a proposal kind; expected one of "
            f"{', '.join(sorted(PROPOSAL_KINDS))}")
    if not isinstance(payload, dict):
        return Invalid("E_BAD_PAYLOAD", "payload must be an object")

    if kind == "steer" and not (payload.get("instruction") or "").strip():
        return Invalid("E_EMPTY_STEER",
                       "a steer must carry an instruction; an empty one "
                       "reaches the executor as a message that says nothing")
    if kind in ("question", "ask") and not (
            payload.get("question") or "").strip():
        return Invalid("E_EMPTY_QUESTION",
                       "a question must carry the question; asking nothing "
                       "spends a model call and answers no one")
    if kind == "hold":
        # A HOLD MUST CARRY ITS JUSTIFICATION. That is the rule, and what
        # counts as justification depends on who is holding.
        #
        # From an AGENT it is the answer that caused it: this vocabulary was
        # written for the answerer, where reading the tree found a problem, and
        # a hold with no answer behind it did not. An agent that could stop
        # work and say nothing is a stall with a nicer name.
        #
        # From a PERSON the justification is that a person decided to. They
        # pressed Pause on their own story's conversation; there was no
        # question and there is no answer to carry, and demanding one would
        # force the caller to invent it -- which is the exact dishonesty this
        # check exists to prevent. Requiring `answer` of a human refused every
        # press of the button (E_NO_ANSWER, observed on KER20 run 1657).
        #
        # `decision` is still required of both: stopping work without saying
        # what you stopped it for leaves the next person nothing to act on.
        if not (payload.get("decision") or "").strip():
            return Invalid("E_NOTHING_TO_DECIDE",
                           "a hold must say what the person has to decide; "
                           "stopping work and asking nothing is a stall")
        by_human = str(actor_id or "").startswith("human:")
        if not by_human and not (payload.get("answer") or "").strip():
            return Invalid("E_NO_ANSWER",
                           "a hold carries the answer that caused it; "
                           "without it a person is asked to decide with no "
                           "idea why the work stopped")
    if kind == "answer":
        if not payload.get("question_id"):
            return Invalid("E_NO_QUESTION",
                           "an answer must name the question it answers")
        if not (payload.get("text") or "").strip():
            return Invalid("E_EMPTY_ANSWER",
                           "an answer with no text un-parks a run and tells "
                           "it nothing")
        settle = (payload.get("verdict") or "").strip().lower()
        if settle == "approve":
            return Invalid(
                "E_CANNOT_APPROVE",
                # I5e: "an assistant" was accurate until GO gained
                # `relay_answer`. A refusal that names the wrong actor
                # reads to GO as a rule about somebody else, and the
                # obvious next move is to look for the tool that is
                # GO's — of which there is none, deliberately.
                "no agent may approve; approval is reserved to a "
                "person (S1.3). Relay the plan and let them approve it, or "
                "send verdict='revise' with what they want changed.")
        if settle and settle not in ANSWER_VERDICTS:
            return Invalid(
                "E_BAD_VERDICT",
                f"{settle!r} is not a verdict an answer may carry; expected "
                f"one of {', '.join(sorted(ANSWER_VERDICTS))}")
    if kind == "cancel" and not (payload.get("reason") or "").strip():
        return Invalid("E_NO_REASON",
                       "a cancellation must record why; the run is about to "
                       "stop and the journal is what remains")
    return None


def validate_question(kind: str, question_text: str, payload: dict
                      ) -> Invalid | None:
    """An executor's question. The evidence floor lives here.

    `attempts` must hold at least three DISTINCT tool-call signatures.
    Distinct, not merely three: the same failing call repeated is evidence
    of a loop, not of exhausting the options.

    AUTHORIZATION IS A DIFFERENT SHAPE and gets a different rule rather
    than an exemption. An `authorization` is not "I am stuck" — it is
    "here is what I intend to do, may I". There is nothing to have tried
    yet, so `attempts` would be a floor the caller could only satisfy by
    inventing evidence. What it must carry instead is the plan itself: a
    non-empty `steps`, because an approval of nothing approves anything.
    """
    if kind not in QUESTION_KINDS:
        return Invalid(
            "E_UNKNOWN_KIND",
            f"{kind!r} is not a question kind; expected one of "
            f"{', '.join(sorted(QUESTION_KINDS))}")
    if not (question_text or "").strip():
        return Invalid("E_EMPTY_QUESTION", "a question must ask something")

    if kind == "acceptance":
        # THE HARNESS ASKING TOO: verification passed, the work is
        # pushed, and a person decides done-or-more-work. Like an
        # authorization there is nothing to have "attempted" -- the
        # evidence is the verdict, carried in the payload's summary.
        if not payload.get("summary"):
            return Invalid("E_NO_SUMMARY",
                           "an acceptance must carry the verification "
                           "summary it is asking a person to accept")
        return None

    if kind == "pr_closed":
        # Carries the same evidence an acceptance does, for the same
        # reason: the card asks a person to decide the fate of work
        # they cannot see from here.
        if not payload.get("summary"):
            return Invalid("E_NO_SUMMARY",
                           "a pr_closed park must say which branch and "
                           "PR were closed, or the person is being asked "
                           "about something they cannot identify")
        return None

    if kind == "closeout":
        # SAME SHAPE AS AN ACCEPTANCE: nothing to have "attempted",
        # and the evidence is what is being shown. Here that is what
        # landed, so a closeout with no summary is a card asking
        # somebody to approve they-know-not-what.
        if not payload.get("summary"):
            return Invalid("E_NO_SUMMARY",
                           "a closeout must say what landed, so the "
                           "person deciding can see what they are "
                           "being asked to call finished")
        return None

    if kind == "landing":
        # NO EVIDENCE FLOOR, and not as an exemption. The floor asks
        # "have you tried enough before spending a person's
        # attention" -- and this park spends nobody's. There is
        # nothing to have attempted: the work is pushed and the wait
        # is for an event outside the run entirely.
        #
        # WHAT IT MUST CARRY is the branches it is waiting on, even
        # when the list is empty. A landing park with no `branches`
        # key is a bug in the caller -- it means the harness decided
        # there was something to land and then could not say what.
        if "branches" not in payload:
            return Invalid("E_NO_BRANCHES",
                           "a landing park must name the branches it "
                           "is waiting on, even if the list is empty")
        return None

    if kind == "escalation":
        # F11 — the HARNESS asking, not the executor. A pattern was
        # measured (the same check refusing twice running) and somebody
        # needs to decide whether the bar is wrong.
        #
        # A FLOOR, NOT AN EXEMPTION, and its own kind rather than a flag
        # in the payload: the executor writes payloads, so a bypass
        # keyed on one would be a bypass it could forge. `ask_human`
        # emits `clarification` and nothing on any tool surface emits
        # this. What it must carry is the measurement — which a caller
        # can only satisfy by having actually observed the repetition.
        if int(payload.get("refusals") or 0) < 2:
            return Invalid(
                "E_NOT_YET_STUCK",
                "an escalation reports a repeated failure; one refusal is "
                "the system working and is not a question for anybody")
        if not [c for c in (payload.get("checks") or []) if str(c).strip()]:
            return Invalid(
                "E_NO_CHECK_NAMED",
                "an escalation must name the check that keeps refusing, or "
                "there is nothing for a person to look at")
        return None

    if kind == "permission":
        # Ec3b — NO ATTEMPTS FLOOR, deliberately. `clarification` demands
        # three distinct prior attempts because a model asking on its
        # first difficulty costs a person their attention for nothing.
        # This is the opposite case: the executor is not stuck, it is
        # holding a destructive call and asking whether to run it.
        # Demanding evidence would teach it to try the thing three times.
        #
        # What it must carry instead is the CALL. A card that says "an
        # irreversible tool call needs approval" is unanswerable — a
        # person cannot tell a typo from a production deletion — and the
        # signature is what the verdict attaches to, so an ask without one
        # would be approved and then apply to nothing.
        if not (payload.get("signature") or "").strip():
            return Invalid("E_NO_SIGNATURE",
                           "a permission ask must carry the signature of "
                           "the call it is asking about")
        if not (payload.get("tool") or "").strip():
            return Invalid("E_NO_TOOL",
                           "a permission ask must name the tool it is "
                           "asking about")
        return None

    if kind == "authorization":
        steps = payload.get("steps")
        if not isinstance(steps, list) or not [
                x for x in steps if str(x).strip()]:
            return Invalid(
                "E_EMPTY_PLAN",
                "an authorization must carry the steps it is asking to be "
                "allowed to take; approving an empty plan approves anything")
        return None

    if kind == "direction":
        # NO ATTEMPTS FLOOR, and the floor's own reasoning is why. It
        # exists because an executor interrupting a person should have to
        # show it exhausted its own options first. Nothing here is
        # interrupting anyone: the person is already in the conversation,
        # they asked the question that produced this, and they are owed
        # the consequence of their own question.
        #
        # What it must carry instead is the DECISION. A hold that stops
        # the work and then shows a card with nothing to decide on it is
        # a stall wearing a question's clothes, and leaves the person no
        # way to release it except guessing.
        if not (payload.get("decision") or "").strip():
            return Invalid(
                "E_NOTHING_TO_DECIDE",
                "a direction must say what the person has to decide; "
                "stopping the work without asking anything is a stall, "
                "not a question")
        # THE SAME SPLIT `validate_proposal` makes about a hold, and it has
        # to be made twice because a hold is validated twice: once as the
        # intent, once as the question it opens. Fixing only the first left
        # every human pause dying here instead -- "execution 1656 asked badly
        # (E_NO_ANSWER) and was not parked", observed on KER20 after the
        # first half was fixed.
        #
        # `from` is stamped by `take_proposals` off the proposal ROW, not read
        # from anything the executor wrote, so this is not the forgeable
        # payload flag the escalation rule above rightly refuses.
        if not str(payload.get("from") or "").startswith("human:")                 and not (payload.get("answer") or "").strip():
            return Invalid(
                "E_NO_ANSWER",
                "a direction carries the answer that caused it; without "
                "it the person is asked to decide with no idea why")
        return None

    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        return Invalid(
            "E_ASK_PREMATURE",
            "a question must carry attempts[]: what you already tried. The "
            "ask is evidence, not a claim.")
    distinct = {a.strip() for a in attempts if isinstance(a, str) and a.strip()}
    if len(distinct) < MIN_ATTEMPTS:
        return Invalid(
            "E_ASK_PREMATURE",
            f"only {len(distinct)} distinct attempt(s); at least "
            f"{MIN_ATTEMPTS} are required before interrupting a human. "
            f"Repeating one failing call is evidence of a loop, not of "
            f"having run out of options.")

    if not (payload.get("proposed") or "").strip():
        return Invalid(
            "E_NO_PROPOSED_DEFAULT",
            "a question must carry `proposed`: your best answer if nobody "
            "replies. It turns the human's job from composing an answer "
            "into yes / no / actually-this.")
    if not (payload.get("blocking_on") or "").strip():
        return Invalid("E_NO_BLOCKING_ON",
                       "say what this blocks; a question that blocks nothing "
                       "should not stop the run")
    if not isinstance(payload.get("reversible", True), bool):
        return Invalid("E_BAD_REVERSIBLE", "reversible must be true or false")
    return None


def validate_directive(kind: str, generation: int, payload: dict
                       ) -> Invalid | None:
    """A Conductor directive. Fenced by generation, like everything else."""
    if kind not in DIRECTIVE_KINDS:
        return Invalid(
            "E_UNKNOWN_KIND",
            f"{kind!r} is not a directive kind; expected one of "
            f"{', '.join(sorted(DIRECTIVE_KINDS))}")
    if not isinstance(generation, int) or generation < 0:
        return Invalid(
            "E_NO_GENERATION",
            "a directive must carry the generation it was issued for, or a "
            "consumer cannot tell a current instruction from a stale one")
    if not isinstance(payload, dict):
        return Invalid("E_BAD_PAYLOAD", "payload must be an object")
    return None


# ── who is in a position to answer ───────────────────────────────────

# THE ONLY STATE WITH A MIND TO ASK. A park ENDS the leg -- the next one
# rebuilds its messages from the task, the brief and the standing
# steers, which is why `standing_steers` reloads them every time -- so
# `PARKED_HOT` looks live and is not. `VERIFYING` has no leg either.
# `PENDING`, `ADMITTED` and `DISPATCHING` have not started one.
#
# So this is a single state, deliberately, and it is not an oversight
# that the list is short: it is the whole reason the answerer stays.
LEG_LIVE_STATES = frozenset({"RUNNING"})


def route_question(state: str) -> str:
    """`ask` or `question` -- who should answer, given the run's state.

    ONE CALLER-FACING INTENT, TWO MECHANISMS. A person asks a question;
    they should not have to know whether a leg happens to be live, and a
    UI that made them choose would be asking them to understand the leg
    model to ask "what are you doing".

    `ask` WHERE THERE IS A MIND. The executor knows what it tried and
    rejected, what it is about to do, and why it read an error the way
    it did -- none of which is in the journal for the answerer to find.

    `question` EVERYWHERE ELSE, WHICH IS MOST OF THE TIME. Parked
    waiting for approval is exactly when people ask, and there is no leg
    then. The answerer is the fallback in the sense that it is not
    preferred, not in the sense that it is rare.
    """
    return "ask" if (state or "").upper() in LEG_LIVE_STATES else "question"
