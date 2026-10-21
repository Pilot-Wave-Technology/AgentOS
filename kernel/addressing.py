"""H4 — which story did you mean, and did you mean to move?

A person working in a goal has a FOCUS: the story they are looking at.
They also have ANCHORS: the cards in front of them, each pinned to a
specific story. Those are different things, and conflating them produces
the two failures this module exists to stop.

REPLYING TO A CARD MUST NOT MOVE THE FOCUS. A card is an interruption —
a question from a run that is not the one being worked on. Answering it
and finding yourself somewhere else is how a person loses their place,
and worse, how the NEXT unaddressed instruction lands on the wrong story.
An anchor addresses; only an explicit move relocates.

AND AN AMBIGUOUS REFERENCE IS ASKED ABOUT, NOT GUESSED. "the story" with
three in flight has three answers and picking one is a coin flip whose
loss is silent — the work goes to the wrong place and looks fine. That
is the same judgement F11 makes about criteria and H1's floor makes about
questions: where the harness cannot know, it says so.

WHAT IS NOT HERE. No natural-language parsing. `resolve` takes a hint
that has already been extracted and decides what it addresses; deciding
what the person's words meant is the assistant's job, and putting a
matcher here would create a second place where that judgement lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Anchor:
    """A card in front of the person, pinned to one story."""

    anchor_id: str
    execution_id: int
    label: str = ""


@dataclass(frozen=True)
class Address:
    """Where an instruction lands, and whether the focus moved."""

    execution_id: int
    via: str                    # anchor | focus | explicit
    focus_after: Optional[int]
    note: str = ""


@dataclass(frozen=True)
class Ambiguous:
    """No single answer. Carries what to ask, and the options."""

    question: str
    candidates: tuple
    reason: str

    @property
    def resolved(self) -> bool:
        return False


def resolve(hint: Optional[str], focus: Optional[int],
            anchors: tuple = (), in_flight: tuple = ()) -> object:
    """Where does this instruction go?

    `hint` is an already-extracted reference: an anchor id, an execution
    id as a string, or None for "wherever I am". `focus` is the story the
    person is looking at. `anchors` are the cards on screen. `in_flight`
    is every story that could plausibly be meant, and exists only to make
    an ambiguous reference answerable rather than merely refused.
    """
    by_id = {a.anchor_id: a for a in anchors}

    # ── an anchor: addresses its story, focus UNCHANGED ──────────────
    if hint and hint in by_id:
        anchor = by_id[hint]
        return Address(
            execution_id=anchor.execution_id, via="anchor",
            focus_after=focus,
            note="replying to a card addresses that story and leaves the "
                 "focus where it was")

    # ── an explicit execution: addresses AND moves ───────────────────
    if hint and hint.isdigit():
        target = int(hint)
        return Address(execution_id=target, via="explicit",
                       focus_after=target,
                       note="naming a story explicitly is choosing to go "
                            "there")

    # ── nothing said: the focus, if there is one ─────────────────────
    if not hint:
        if focus is not None:
            return Address(execution_id=focus, via="focus",
                           focus_after=focus,
                           note="unaddressed instructions land where the "
                                "person is looking")
        if len(in_flight) == 1:
            return Address(execution_id=in_flight[0], via="focus",
                           focus_after=in_flight[0],
                           note="one story in flight and no focus set")
        return Ambiguous(
            question="Which story is this for?",
            candidates=tuple(in_flight),
            reason=f"no focus is set and {len(in_flight)} stories are in "
                   f"flight; picking one is a coin flip whose loss is "
                   f"silent")

    # ── a hint that matches nothing ──────────────────────────────────
    matches = [a for a in anchors
               if hint.lower() in (a.label or "").lower()]
    if len(matches) == 1:
        return Address(execution_id=matches[0].execution_id, via="anchor",
                       focus_after=focus,
                       note="matched one card by label")
    return Ambiguous(
        question=f"Which story does {hint!r} mean?",
        candidates=tuple(m.execution_id for m in matches) or tuple(in_flight),
        reason=(f"{len(matches)} cards match that label"
                if matches else
                f"{hint!r} matches no card and is not an execution id"))
