"""An errand's lifecycle must not move while the coding story's does.

THIS TEST IS THE GUARANTEE, not a nicety. The coding story lifecycle is being
changed so a story waits at `awaiting_merge` between push and merge, holding its
pod. Every one of those changes runs through code an ERRAND also runs through:

  * `on_story_done` is ONE function whose errand arm deletes the branch and
    closes the thread,
  * `reclaim.py` releases on the story's signal for both kinds, deliberately --
    "an errand is a story for this purpose",
  * the `acceptance` park and its card are opened by one call site for both,
  * `deliver.py` holds an errand's writable mounts back at the same chokepoint
    the story's push now goes through.

So "errands are unaffected" cannot be an intention. It has to be a thing that
fails loudly, which is what this is. The assertions below are deliberately about
the RULES rather than about one run's rows: a rule that still reads the same way
is the only evidence that survives a refactor of the code underneath it.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_an_errand_still_waits_for_the_person_and_not_the_machine():
    """`reclaim` releases on the STORY's state for both kinds.

    The comment this guards is worth more than the SQL: an errand reaching
    execution-DONE is the machine finishing, and the story reaching done or
    reviewed is a person deciding they have what they need. Releasing on the
    first destroys the tree while somebody is reading the output.
    """
    src = _read("kernel/conductor/reclaim.py")
    assert "an errand is a story for this purpose" in src.lower(), (
        "reclaim no longer states that an errand waits for the human signal; "
        "if that rule moved, an errand's workspace can vanish while its "
        "output is being read")
    assert "s.state IN ('done', 'reviewed')" in src, (
        "reclaim's release predicate changed shape; check an errand still "
        "waits for the story signal and not for e.state = 'DONE'")


def test_an_errands_work_still_never_lands():
    """`deliver.py` is the push chokepoint the coding story's new push goes
    through. An errand must keep being refused there."""
    src = _read("kernel/workspace/deliver.py")
    assert "errand" in src.lower(), (
        "deliver.py no longer mentions errands at all; the carve-out that "
        "stops an errand's working files reaching the forge is gone")


def test_the_errand_arm_of_on_story_done_is_still_there():
    """`on_story_done` has two arms. The story arm is what the new lifecycle
    changes; the errand arm deletes the branch and closes the thread, and a
    coding-story change must not be made by editing the shared path."""
    src = _read("app/services/go_wake_events.py")
    assert "is_errand" in src, (
        "on_story_done no longer branches on is_errand — the errand arm "
        "(branch deletion, thread closure) may have been folded into the "
        "story path")
    assert "skipped_errand" in src, (
        "the review-skip reason for errands is gone; an errand would now "
        "wait for a review that never comes")


def test_an_errand_is_still_excluded_from_landing():
    """`goal_state` reports `landed` for stories and None for errands. Any new
    lifecycle field needs the same carve-out or every finished errand reads as
    waiting for a merge that cannot happen."""
    src = _read("kernel/go/state.py")
    assert re.search(r"errand", src), (
        "goal_state no longer special-cases errands; check `landed` and any "
        "new awaiting-merge field still exclude them")


# Writing the state is the dangerous act. READING or filtering on it is always
# safe: an errand simply never matches, which is the carve-out working rather
# than one somebody has to remember. So this looks only for writers.
_WRITES_THE_STATE = re.compile(
    r"record_state\w*\([^)]*[\"']awaiting_merge[\"']"      # record_state(..., "awaiting_merge"
    r"|SET\s+state\s*=\s*'awaiting_merge'"                  # UPDATE ... SET state = '...'
    r"|\bstate\s*=\s*[\"']awaiting_merge[\"']",             # state = "awaiting_merge"
    re.I,
)

# `state = 'awaiting_merge'` is a write in Python and a FILTER in a SQL WHERE
# clause, and they are the same characters. The keyword in front is what tells
# them apart, so a line that reads the state is dropped before the write check
# rather than being made to carry a discriminator it does not need.
_READS_THE_STATE = re.compile(r"\b(?:WHERE|AND|OR|HAVING|WHEN)\b", re.I)


def test_no_errand_can_reach_awaiting_merge():
    """The new state is for work that LANDS. An errand's never does.

    Enforced at the WRITER, not in the schema, because the state column is
    shared by both kinds. What would not be safe is a writer that PUTS a story
    into the state without first establishing it is a story with somewhere to
    land, so that is what this looks for.
    """
    hits = []
    for path in sorted((REPO / "app").rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "awaiting_merge" not in text:
            continue
        lines = text.splitlines()
        for n, line in enumerate(lines, 1):
            if not _WRITES_THE_STATE.search(line):
                continue
            if _READS_THE_STATE.search(line) and "SET " not in line.upper():
                continue
            window = "\n".join(lines[max(0, n - 15):n + 5])
            if not re.search(r"errand|work_kind|git_node|story_kind", window):
                rel = path.relative_to(REPO).as_posix()
                hits.append(f"{rel}:{n}  {line.strip()[:70]}")
    assert not hits, (
        "these WRITE `awaiting_merge` with no errand / work_kind / git_node / "
        "story_kind discriminator in the surrounding lines, so an errand could "
        "be put into a state that waits for a merge it will never have:\n  "
        + "\n  ".join(hits))


def test_an_errand_is_never_asked_about_a_merge():
    """THE RESEQUENCING MUST NOT REACH ERRANDS.

    A coding story is now asked about the MERGE first and about the
    STORY afterwards. An errand's work never lands, so there is no merge
    to ask about -- it must go straight to the same acceptance card it
    always got, with the same words on it.

    `completion_park.kind_for` is the one place that decides, and it
    decides by asking `owes_a_landing`, which excludes an errand on the
    RUN's own kind. Pinned here because the failure would be quiet: an
    errand parked on a `landing` waits for a merge that cannot happen,
    and nothing would look wrong until the deadline took it.
    """
    import inspect

    from kernel.worker import completion_park
    src = inspect.getsource(completion_park)
    assert "owes_a_landing" in src, (
        "the merge-or-accept choice no longer goes through the one "
        "predicate that excludes errands")
    assert "ACCEPTANCE" in src and 'return ACCEPTANCE' in src, (
        "there is no longer a path back to the plain acceptance ask")


def test_marking_a_story_done_says_the_same_thing_for_both_kinds():
    """An errand and a coding story reach this card at different moments
    -- an errand when its work is done, a story once that work is on
    trunk -- and it has to say the same thing on both. Two labels for
    one decision would teach people that finishing differs by kind."""
    from app.services.kernel_park_cards import build_card

    def _first(kind):
        card = build_card({"kind": kind, "id": 1, "execution_id": 1,
                           "goal_id": "g", "story_id": "s",
                           "question_text": "done?"}, None, "")
        return card["options"][0]["label"]

    assert _first("acceptance") == _first("closeout") == "✓ Mark story done"


def test_the_assistant_and_the_executor_agree_on_the_sequence():
    """TWO PROMPTS DESCRIBING ONE WORKFLOW DRIFT UNLESS SOMETHING READS
    BOTH. The assistant explains the cards to a person; the executor
    lives through them. If they disagree about the ORDER, the assistant
    tells somebody to expect a card that is not coming, or the executor
    waits for one that already passed.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    a = (root / "kernel/assistant/AGENTS.md").read_text(encoding="utf-8")
    e = (root / "profiles/story-coding/prompt.md").read_text(encoding="utf-8")

    # merge first, story after -- on both sides
    for doc, name in ((a, "assistant"), (e, "executor")):
        assert "merge" in doc.lower() and "Mark story done" in doc, (
            f"the {name} prompt does not name both decisions")

    # the assistant must not still promise a card-free wait
    assert "There is no card and" not in a
    # and must say the errand carve-out, or it will explain a merge card
    # to somebody whose errand will never show one
    assert "errand never sees the first one" in a.lower() or \
           "An errand never sees the first one" in a, (
        "nothing tells the assistant that an errand gets no merge card")

    # the executor must know a person merges, not accepts
    assert "asked to merge" in e, (
        "the executor is not told that the first decision after its "
        "claim is a merge, so a wait there reads as a stall")
