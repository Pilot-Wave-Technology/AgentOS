"""No prompt tells a model that a verifier decides, because none does.

THE SWEEP THAT MISSED THREE. A first pass reconciled the assistant, the
GO, story-coding and errand-ops, and stopped there because those were
the files that talked about the state machine. Three more were making
factual claims about verification without ever naming a state:

    answerer/prompt.py     "verification is what says it" -- the LAST
                           line of the prompt, and the answerer is the
                           agent that talks to a person mid-run
    assistant/SOUL.md      "a verifier standing over it"
    story-business         has_verifier = true, so the switch applies to
                           it, and the prompt said nothing at all

WHAT EVERY PROMPT IS GIVEN INSTEAD OF THE SETTING. None of them is told
which way the switch is set -- a prompt saying "verification is off"
would be wrong the moment an admin turns it on, and a leg cannot re-read
its prompt mid-run. Each is given an INVARIANT that holds either way
(the claim met its checks, the work is pushed, a person decides) plus a
tool to ask when the difference actually matters.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

# Every model-facing prompt in the system. A new agent added without a
# line here is the gap this test exists to close.
PROMPTS = (
    "kernel/assistant/AGENTS.md",
    "kernel/assistant/SOUL.md",
    "kernel/go/AGENTS.md",
    "kernel/go/SOUL.md",
    "kernel/answerer/prompt.py",
    "profiles/story-coding/prompt.md",
    "profiles/story-business/prompt.md",
    "profiles/errand-ops/prompt.md",
)

# Claims that are FALSE while the switch is off. Each was live in a
# prompt; none is a hypothetical.
FALSE_WHEN_OFF = (
    "verification is what says it",
    "a verifier standing over it",
    "the same commands the verifier will run",
    "fails verification for reasons",
)


@pytest.mark.parametrize("claim", FALSE_WHEN_OFF)
def test_no_prompt_promises_a_verifier(claim):
    for rel in PROMPTS + ("kernel/harness/selfcheck.py",):
        body = (ROOT / rel).read_text(encoding="utf-8")
        assert claim.lower() not in body.lower(), (
            f"{rel} tells a model {claim!r}, which is false whenever the "
            f"platform verification switch is off -- and it is off by "
            f"default")


def test_the_two_agents_that_describe_states_say_the_switch_exists():
    """The assistant renders the state machine to a person and the GO
    chooses story-vs-errand from it. Both have to know VERIFYING is
    conditional, or they describe a road the run will not take."""
    for rel in ("kernel/assistant/AGENTS.md", "kernel/go/AGENTS.md"):
        body = (ROOT / rel).read_text(encoding="utf-8")
        assert "switch" in body.lower() and "verification" in body.lower(), (
            f"{rel} describes the state machine without mentioning that "
            f"verification is switchable")


def test_the_assistant_gives_an_answer_that_holds_either_way():
    """It is told the INVARIANT rather than the setting, because the
    setting can change under a parked run."""
    body = (ROOT / "kernel/assistant/AGENTS.md").read_text(encoding="utf-8")

    assert "story_progress" in body
    i = body.lower().index("acceptance park always means")
    assert "do not assume" in body[i:i + 700].lower(), (
        "the assistant is left to infer which road a run came by")


def test_the_go_does_not_key_the_story_errand_choice_on_a_verifier():
    """It used to read "story: gets a Verifier / errand: does not". With
    the switch off that reasoning makes every story an errand."""
    body = (ROOT / "kernel/go/AGENTS.md").read_text(encoding="utf-8")

    i = body.index("**errand**")
    assert "whatever the platform verification switch says" in body[i:i + 600], (
        "the errand row does not pin that an errand never gets a "
        "verifier regardless of the switch")


def test_every_executor_profile_says_what_a_claim_actually_does():
    """All three can be parked on an acceptance, so all three need to
    know what `propose_completion` does to their checks. story-business
    was silent about it entirely.

    NOT "does it say sandbox". story-business has no sandbox in its
    vocabulary -- it produces documents, not trees -- and an assertion
    on that word tested this test's own idea of the answer rather than
    the property. What every profile must carry is that something is
    MEASURED at the claim and something is not."""
    for rel in ("profiles/story-coding/prompt.md",
                "profiles/story-business/prompt.md",
                "profiles/errand-ops/prompt.md"):
        body = (ROOT / rel).read_text(encoding="utf-8")
        assert "propose_completion" in body, f"{rel} never names the claim"
        assert "measured" in body.lower(), (
            f"{rel} never says what is measured at the claim, so the "
            f"model cannot tell an artifact check from a command one")


def test_no_executor_profile_is_told_its_checks_are_capped():
    """The cap on a CHECK is gone; a prompt that still implies one would
    have the model trim a suite to fit a clock that no longer exists.

    THE 120 SECONDS ON `bash` IS REAL AND STAYS, which is why this reads
    the sentence rather than banning the number. That ceiling is the
    whole reason `bash_background` exists, and a prompt that hides it
    sends the model to a foreground command that dies mid-suite and
    takes its output with it. What must not appear is a duration
    attached to a check, or one stated without the way past it.
    """
    for rel in ("profiles/story-coding/prompt.md",
                "profiles/errand-ops/prompt.md"):
        body = (ROOT / rel).read_text(encoding="utf-8")
        assert "keep it quick" not in body.lower(), f"{rel} implies a cap"

        for line in body.splitlines():
            low = line.lower()
            if "two minutes" not in low and "120 second" not in low:
                continue
            assert "`bash`" in line, (
                f"{rel} states a duration cap without saying it is "
                f"`bash`'s: {line!r}")
            assert "check" not in low, (
                f"{rel} attaches a clock to a CHECK: {line!r}")

        # And wherever the ceiling IS stated, the escape is stated with
        # it -- a cap with no way past it is a cap.
        if "120 second" in body.lower():
            assert "bash_background" in body and "bash_wait" in body, (
                f"{rel} names the ceiling and not the way past it")


def test_story_coding_tells_it_when_a_new_plan_is_needed_and_when_not():
    """A failed check is NOT a cycle: the claim was never recorded, the
    approval is still live, and re-planning spends a person's attention
    on a question they already answered. A recorded claim IS one."""
    body = (ROOT / "profiles/story-coding/prompt.md").read_text(encoding="utf-8")

    assert "is not a cycle" in body.lower(), (
        "story-coding does not distinguish a refused claim from a "
        "recorded one, so it will re-register a plan it still holds")
