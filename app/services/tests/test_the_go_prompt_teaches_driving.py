"""The GO's prompts say what it may now do, and what it still may not.

WHY THIS IS A GATE ON PHASE C RATHER THAN POLISH AFTER IT. The moment
the seven driving tools joined `GO_TOOLS`,
`test_no_prompt_promises_a_tool_its_agent_does_not_hold` went red with
five tools the GO held and was never told about. A capability the prompt
does not mention is one the model will not reach for, and the guard
already knew that.

WHAT THE PROMPTS CONTRADICTED. Three separate places said the GO never
starts a run, written when that was true of everything. An autonomous
errand starts itself on its schedule, so the sentence had to be scoped
to stories rather than deleted -- the GO still never starts a story's
run, and that is the part worth keeping.

WHITESPACE IS COLLAPSED EVERYWHERE BELOW. These files are hard-wrapped
prose, and a guard that depends on where a line breaks fails on a reflow
and tells you nothing about the prompt.
"""
from __future__ import annotations

import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]

GO_AGENTS = _ROOT / "kernel/go/AGENTS.md"
GO_SOUL = _ROOT / "kernel/go/SOUL.md"
ASSISTANT_AGENTS = _ROOT / "kernel/assistant/AGENTS.md"


def _flat(p: pathlib.Path) -> str:
    return " ".join(p.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def go() -> str:
    return _flat(GO_AGENTS) + " " + _flat(GO_SOUL)


# ── it is told what it holds ──────────────────────────────────────────

def test_every_driving_tool_is_named(go):
    for tool in ("story_progress", "story_status", "ask_executor", "steer",
                 "run_control", "cancel_story"):
        assert f"`{tool}`" in go, tool


def test_the_cheap_read_is_first_when_the_question_is_gos_own(go):
    """THE OLD RULE, NARROWED, AND WHY.

    This asserted "first, always" on `story_progress`: it costs nothing,
    answers most questions, and a prompt listing the two as peers gets
    the expensive one by default. That reasoning still holds for a
    question GO asks itself.

    It was WRONG for the scheduled check-in, and that is not a nuance --
    it was the whole reason the check-in never worked. The wake said
    "ask the executor"; the tool table said `story_progress` **first,
    always**; the table is the more general instruction and it won. On
    runs 307 and 308 GO read the journal and then told the thread it had
    "checked in with the executor", which it had not.

    The two are not interchangeable there. The journal is what was
    written down; a run that has gone quiet, or is confidently doing the
    wrong thing, looks healthy in it. The check-in exists to catch
    exactly that, so it has to reach the executor.
    """
    assert "first when the question is yours" in go
    assert "NOT first on a scheduled" in go, (
        "nothing tells GO the check-in is the exception, so the general "
        "rule wins again")
    assert "first on a scheduled check-in" in go, (
        "`ask_executor` is not named as the check-in's first call")


def test_it_is_told_the_tools_refuse_a_story(go):
    assert "E_NOT_AN_ERRAND" in go
    assert "E_NOT_AUTONOMOUS" in go


def test_a_refusal_is_framed_as_the_boundary_working(go):
    """A refusal the prompt walked it into reads as a platform bug, and
    the model reports it to the person as one."""
    assert "boundary working" in go or "boundary, not a bug" in go
    assert "Do not retry it" in go


# ── it is told what is NOT its business ───────────────────────────────

def test_stories_are_named_as_the_assistants(go):
    assert "Stories are not" in go
    assert "assistant whose whole job is that story" in go or \
           "assistant whose entire job is that story" in go


def test_a_hand_started_errand_is_named_too(go):
    """The subtle half: it IS an errand, and that is not enough."""
    assert "started by hand" in go
    assert "Nobody has left" in go or "nobody has left" in go


def test_it_is_told_how_to_help_a_story_it_cannot_touch(go):
    """A boundary with no alternative is one the model argues with."""
    assert "say it in the goal thread" in go or \
           "say so in the goal thread" in go


# ── the contradictions, scoped rather than deleted ────────────────────

def test_it_still_never_starts_a_storys_run(go):
    assert "never START a run" in go or "you never do" in go


def test_but_it_is_told_an_approved_errand_starts_itself(go):
    """THE WORDING LOST "on its schedule" WITH RECURRENCE. An errand
    starts when it is approved, not on a schedule -- there isn't one.
    The property being asserted is unchanged: every "you never start a
    run" statement has to be scoped, because an unscoped prohibition
    beats a scoped permission."""
    assert go.count("starts itself") >= 2, (
        "one of the 'you never start a run' statements was left "
        "unscoped, and an unscoped prohibition beats a scoped permission")


def test_the_never_list_covers_the_new_verbs(go):
    assert "approve, cancel, pause or resume anything on your own" in go


def test_propose_freely_no_longer_contradicts_owning_an_errand(go):
    """SOUL said "act only with approval" flatly, which reads as a
    prohibition on the whole of Phase C."""
    assert "where that stops being the whole story" in go


# ── several threads ───────────────────────────────────────────────────

def test_it_is_told_it_has_more_than_one_thread(go):
    assert "Several threads, one you" in go


def test_it_is_told_it_cannot_remember_the_others(go):
    """The epistemics matter more than the fact: a model that believes
    it has continuity invents it."""
    assert "You cannot remember the other threads" in go
    assert "as I mentioned" in go


def test_memory_is_named_as_the_thing_that_crosses(go):
    assert "MEMORY.md` is shared across every thread" in go


def test_the_three_fixed_channels_are_distinguished(go):
    """They fold into the goal thread. An errand thread does not."""
    assert "Agent Mesh, Goal Review Council" in go
    assert "feed into the goal thread" in go


# ── escalation ────────────────────────────────────────────────────────

def test_escalation_is_a_judgement_with_a_test(go):
    assert "would this person want to have been asked?" in go


def test_it_is_told_where_to_escalate(go):
    """The errand's own thread, where its approver is -- not the goal
    thread, where they may never look."""
    assert "in **the errand's own thread**" in go or \
           "the errand's own thread" in go


def test_the_human_uid_is_not_the_gos(go):
    assert "never yours" in go or "Not you -- a person" in go


# ── the branch is scaffolding ─────────────────────────────────────────

def test_it_may_read_the_errands_branch_and_knows_it_is_discarded(go):
    assert "That branch is scaffolding" in go
    assert "deleted when the errand is done" in go
    assert "quote the artifact, never the branch" in go


# ── pausing ───────────────────────────────────────────────────────────

def test_the_ways_to_stop_are_distinguished(go):
    """Hold it, carry on, end this run, end the story -- distinct acts.

    "A recurring errand cancelled at the RUN comes back tomorrow" was
    the old distinction and it is gone with recurrence: nothing comes
    back. What remains is still worth separating, because cancelling a
    RUN leaves the STORY exactly where it was -- which is the trap
    `cancel_story` sets by its name."""
    assert "Stopping it: three different acts" in go
    assert "Cancelling a RUN is not cancelling the STORY" in go
    assert "comes back tomorrow" not in go, (
        "the prompt still promises a recurrence that cannot happen")


def test_it_is_told_to_use_the_tool_rather_than_relay(go):
    """THE OBSERVED FAILURE. Told to cancel, the GO declined the plan and
    answered a clarification with "stop" -- and an executor can only turn
    an instruction into a plan, so it planned a cancellation and parked
    again. To the person the cancel had done nothing."""
    assert "Do not relay it as a steer or" in go
    assert "arrives as *a plan to stop*" in go


def test_run_control_is_named_as_the_same_thing_as_the_buttons(go):
    """So the GO and the person cannot disagree about whether something
    is paused -- which they could while a draft wrote `execution_pauses`
    and the button submitted a `hold` proposal."""
    assert "same mechanism as the Pause / Resume / Cancel" in go


def test_it_is_told_a_pause_is_not_immediate(go):
    """Otherwise it reports the errand stopped, and the errand is still
    committing a file."""
    assert "Nothing stops mid-step" in go
    assert "can take minutes to look like anything" in go


def test_it_is_told_pause_only_works_on_a_running_leg(go):
    """A run parked on a plan is already waiting for a person; there is
    no leg to stop, and the refusal would read as a broken tool."""
    assert "Pause only works while it is RUNNING" in go


# ── the cap ───────────────────────────────────────────────────────────

def test_the_cap_is_described_without_a_number(go):
    """It was "three answers per run" in prose and 3 in code, and the
    code moved to 10. A number in two places drifts."""
    assert "Three answers per run" not in go
    assert "hitting it is the design working" in go


# ── the assistant's side of the same boundary ─────────────────────────

def test_the_assistant_is_told_the_errand_moves(go):
    a = _flat(ASSISTANT_AGENTS)
    assert "An AUTONOMOUS errand is the GO's, not yours" in a


def test_the_assistant_is_told_it_lost_nothing(a=None):
    """The regression this whole phase must not cause, said in the place
    the model will read it."""
    a = _flat(ASSISTANT_AGENTS)
    assert "You keep everything else" in a
    assert "Nothing you could do yesterday you cannot do today" in a


def test_the_assistant_keeps_the_accountability():
    a = _flat(ASSISTANT_AGENTS)
    assert "You stay the principal on the errand you approved" in a
    assert "What moved is the conversation, not the responsibility" in a


def test_the_assistant_is_told_it_can_still_read_it():
    """Otherwise it answers "that is the GO's now" to a person asking how
    their own errand is going."""
    a = _flat(ASSISTANT_AGENTS)
    assert "all work on an errand of yours" in a


# ── completion always asks, and both agents are told ──────────────────

def test_the_go_is_told_every_completion_parks(go):
    """Not only the first run. A grant used to skip this entirely, so
    the GO's picture of its own errands was that they closed
    themselves."""
    assert "It always asks before it closes" in go
    assert "Not only the first run" in go


def test_the_go_is_told_what_a_grant_actually_buys(go):
    """Otherwise the narrowing reads as a bug to route around."""
    assert "does not mean the work closes without anybody reading it" in go
    assert '"Is this enough?" cannot' in go


def test_the_go_is_told_to_say_what_is_MISSING(go):
    """A summary that lists only what was done invites a rubber stamp,
    and the whole reason this gate exists is that an errand can do
    exactly what its plan said and still leave the next question
    unasked."""
    assert "Say what is missing, not just what is there" in go
    assert "invites a rubber stamp" in go


def test_the_go_is_told_which_verdict_means_more_work(go):
    """`revise` on an acceptance is not "rewrite the plan" -- it is
    "carry on". Getting that wrong closes an errand somebody wanted
    continued."""
    assert "`revise` here means **more work**" in go


def test_the_assistant_is_told_too():
    a = _flat(ASSISTANT_AGENTS)
    assert "An errand always asks before it closes" in a
    assert "yours and the GO's alike, granted or not" in a


def test_neither_prompt_still_promises_an_unattended_finish():
    """The old promise was explicit and appeared in three places -- the
    proposal card, the GO's prompt and the assistant's. A card that
    promises what the system no longer does is worse than no card."""
    both = _flat(GO_AGENTS) + " " + _flat(GO_SOUL) + " " + _flat(ASSISTANT_AGENTS)
    for gone in ("finishing without being accepted",
                 "finishes on its own",
                 "it may finish on its own"):
        assert gone not in both, gone


def test_the_proposal_card_says_starts_not_finishes():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "app/services/errand_proposal_cards.py").read_text(
        encoding="utf-8")
    assert "STARTS without you" in src
    assert "asks you before it closes" in src
    assert "STARTS without you" in src


def test_it_knows_an_errand_can_be_cancelled_at_any_point(go):
    """The board refuses a STARTED story because its branch is cut and
    commits would be stranded. An errand never merges, so its branch is
    a dead ref either way -- it stays cancellable throughout, and GO
    should point somebody at that rather than at a pause or a run
    cancel that leaves the story standing."""
    assert "An errand can be cancelled at ANY point" in go
    assert "never merges" in go


def test_it_is_not_told_cancel_story_ends_the_story(go):
    """THE TOOL LIES BY ITS NAME. `cancel_story` submits a cancel
    against the EXECUTION and never touches `stories.state` -- traced:
    zero references to `stories` on that path, and `apply_cancellation`
    only does `UPDATE executions`.

    The prompt said it "ends the errand; no further runs", so GO would
    call it on "stop this errand for good", report success, and leave
    the story `in_progress` with nothing running. Worse than the missing
    tool it resembles: there the agent said it could not; here it says
    it did."""
    assert "ends the errand; no further runs" not in go
    assert "DOES NOT END THE STORY" in go


def test_it_knows_which_act_actually_ends_an_errand(go):
    """A correction that does not name the real route just leaves GO
    stuck. The board button is the one that ends it, and GO cannot
    press it -- so the useful thing it CAN do is stop the run and say
    so."""
    assert "ENDING AN ERRAND IS THE BOARD" in go
    assert "You cannot press it" in go


def test_it_is_not_told_the_approval_tool_cannot_exist(go):
    """THE PROMPT CONTRADICTED ITSELF AND GO BELIEVED THE WRONG HALF.

    One passage said "there is no tool that would let you -- not even by
    relaying". Thirty lines later another said to call `accept_errand`
    and relay. Asked "do you have a tool to approve an errand?", a GO
    that HELD the tool answered "No" -- the absolute sentence won.

    The real constraint survives: GO never approves on its own judgment.
    What is gone is the claim that no mechanism exists."""
    assert "there is no tool that would let you" not in go
    assert "not even by relaying" not in go
    assert "The decision is never yours" in go
    assert "Carrying their decision is not making it" in go


def test_no_blanket_denial_that_a_surface_exists(go):
    """The general shape, so the next contradiction is caught by machine
    and not by a person asking the agent a yes/no question in production.

    Naming what is refused and why is fine. Declaring that no mechanism
    exists is what strands a tool GO is actually holding."""
    assert "there is no tool" not in go
    assert "no tool that" not in go


def test_accept_errand_is_scoped_to_autonomous_errands(go):
    """The tool goes STRAIGHT TO in_progress, which is only right for a
    story whose grant already permits running unattended.

    Pointed at an ordinary story it would collapse accept-then-start
    into one act GO took for somebody else, beginning their work on
    their name at a moment they did not pick. The code refuses it
    (E_NOT_AN_ERRAND, read off `work_kind`, not off the caller) -- the
    prompt has to teach the same boundary or GO spends its turns
    discovering it through refusals."""
    assert "AUTONOMOUS ERRANDS ONLY" in go
    assert "STRAIGHT TO `in_progress`" in go
    assert "E_NOT_AN_ERRAND" in go


def test_it_knows_what_to_do_when_it_is_not_an_errand(go):
    """A boundary with no alternative reads as a dead end, which is how
    GO ended up creating a story by hand on ERR12. Name the real route:
    a person, on the board."""
    assert "it is a person's click on the board" in go
