"""The numbers in the prompts are the numbers in the code.

WHY THIS IS WORTH A TEST. A prompt is the only description of the system
two of its agents ever read, and a wrong number in one does not fail
anything — it produces a confidently wrong explanation to a person. The
assistant's SOUL.md said "up to four stories in flight at once" for as
long as the ceiling was four, and stayed saying it when the ceiling
became five: nothing broke, and every person who asked "how many can I
run?" got the wrong answer from the thing best placed to know.

These assertions are deliberately about the NUMBERS and not the prose.
Wording is the author's; a ceiling is the platform's, and the two should
not be able to disagree.
"""

from __future__ import annotations

import io
import pathlib
import re

from kernel.conductor.admit import SYSTEM_CEILING
from kernel.conductor.owner import ASSISTANT_CEILING, CEILINGS, GO

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


# ── the assistant is told its own number ─────────────────────────────

def test_the_assistant_soul_states_the_assistant_ceiling():
    words = {5: "five", 10: "ten", 20: "twenty"}[ASSISTANT_CEILING]
    soul = _words(_read("kernel/assistant/SOUL.md"))
    assert words in soul, (
        f"the assistant ceiling is {ASSISTANT_CEILING} and SOUL.md does "
        f"not say {words!r}")


def test_the_assistant_soul_no_longer_says_four():
    """THE STALE NUMBER ITSELF. It survived a ceiling change once
    already, and it read perfectly well the whole time."""
    soul = _words(_read("kernel/assistant/SOUL.md"))
    assert "four stories" not in soul
    assert "own four stories" not in soul


def test_the_assistant_is_told_the_budget_is_per_goal():
    """Five per goal, not five in total — the distinction a person in
    two goals asks about, and the one a wrong answer strands them on."""
    soul = _words(_read("kernel/assistant/SOUL.md"))
    assert "per goal" in soul


# ── GO is told its own number, and the shared one ────────────────────

def test_go_is_told_how_many_errands_it_may_run():
    words = {5: "five", 10: "ten", 20: "twenty"}[CEILINGS[GO]]
    agents = _words(_read("kernel/go/AGENTS.md"))
    assert f"{words} at a time" in agents or f"{words} at once" in agents, (
        f"GO may run {CEILINGS[GO]} autonomous errands per goal and "
        f"AGENTS.md does not say so")


def test_both_agents_are_told_the_platform_ceiling():
    """The one they cannot do anything about — which is exactly why
    they have to be able to name it rather than offer a lever that
    does not exist."""
    words = {16: "sixteen", 20: "twenty", 24: "twenty-four"}[SYSTEM_CEILING]
    for rel in ("kernel/go/AGENTS.md", "kernel/assistant/AGENTS.md"):
        assert words in _words(_read(rel)), f"{rel} never names the {words}"


# ── and what the run itself says when it waits ───────────────────────

def test_a_queued_run_is_not_described_as_stuck():
    """`for_stall` had one fallback for everything it did not recognise:
    "nothing retries this automatically". Admission is level-triggered —
    it runs every tick and admits the row the moment a slot frees — so
    that answer was backwards for the single most common wait there is,
    and acting on it means restarting a run that was about to start."""
    from kernel.assistant.remedies import for_stall

    goal_full = for_stall(
        "admission: assistant:u1 already has 5 running in goal G1, which "
        "is its limit; this one starts as soon as one of them finishes")
    assert goal_full, "a goal-ceiling wait has no remedy at all"
    assert not any("nothing retries this automatically" in r["do"]
                   for r in goal_full)

    system_full = for_stall(
        "admission: the system is at its ceiling of 20 concurrent "
        "executions")
    assert system_full
    assert not any("nothing retries this automatically" in r["do"]
                   for r in system_full)


def test_the_two_waits_get_different_answers():
    """Behind your own budget you have a choice; behind the platform's
    you have none. One answer for both is the wrong answer for one."""
    from kernel.assistant.remedies import for_stall

    mine = for_stall("admission: assistant:u1 already has 5 running in "
                     "goal G1, which is its limit")
    theirs = for_stall("admission: the system is at its ceiling of 20 "
                       "concurrent executions")
    assert [r["do"] for r in mine] != [r["do"] for r in theirs]


def test_the_goal_stall_reason_no_longer_names_domain_and_kind():
    """It said "at its ceiling for coding/story" — the OLD key. With
    every bucket now written 'any'/'any' that sends whoever reads it
    looking for a per-domain limit that does not exist."""
    src = _read("kernel/conductor/admit.py")
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "ceiling for {row['domain']}/{row['kind']}" not in code
