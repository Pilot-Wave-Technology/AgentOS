"""E7b/E7c — dispatch a child agent and bring back a paragraph.

The child is a real leg: same loop, same journal, same predicates. What
differs is data — a narrower envelope (attenuate.py), a budget sliced from
the parent's remainder, and `depth=1` on every row it writes.

THE RETURN IS BOUNDED, AND THAT IS THE POINT (SA7-SA9). A child that
returned its transcript would defeat the reason to delegate: the parent
delegated to keep that work out of its own context, and handing back three
thousand tokens of exploration puts it straight back. So the child's answer
is a paragraph. Over the bound it is truncated with a marker — E7's
artifact store will turn that into an id worth following, and until it
exists a truncation that says so is better than a silent one.

WHAT A CHILD CANNOT DO, enforced by the envelope rather than by asking:
delegate again (depth 1 is the ceiling), claim the story complete, register
a plan, or ask a human. The first is recursion control; the rest are the
parent's to do, and a child doing them would report progress on a story it
cannot see.
"""

from __future__ import annotations

import logging

from kernel.harness.attenuate import (
    AttenuationError,
    assert_attenuated,
    derive_child_envelope,
    slice_budget,
)

logger = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 2000
CHILD_STEP_BUDGET = 25
MAX_DEPTH = 1


class SubagentRefused(Exception):
    """The dispatch is refused before a child exists."""


def build_child_prompt(flavour: str, task: str) -> str:
    """The child's instruction. Deliberately not the parent's prompt.

    A child that inherited the parent's role would try to finish the story;
    it has one errand and no view of the rest.
    """
    if flavour == "investigate":
        shape = (
            "You are investigating one question for another agent. You can "
            "read and search; you cannot change anything, and you have no "
            "shell.\n\n"
            "Answer in a few sentences of prose. Name the files and line "
            "numbers that support your answer. If you could not determine "
            "something, say so plainly — a wrong confident answer costs the "
            "agent that asked more than an honest gap."
        )
    else:
        shape = (
            "You are making one self-contained change for another agent, in "
            "your own worktree. You cannot commit — the agent that dispatched "
            "you will review and merge your work.\n\n"
            "When you are done, describe in a few sentences what you changed "
            "and why, and anything you noticed that the agent should check."
        )
    return f"{shape}\n\n## Your task\n\n{task}\n"


def bound_answer(text: str) -> str:
    """A paragraph, not a transcript."""
    text = (text or "").strip()
    if not text:
        return "(the child returned no answer)"
    if len(text) <= MAX_ANSWER_CHARS:
        return text
    return (
        text[:MAX_ANSWER_CHARS]
        + f"\n\n[... truncated at {MAX_ANSWER_CHARS} characters. The child's "
          "full journal is at depth=1 under this execution.]"
    )


def prepare_dispatch(
    parent_envelope,
    parent_depth: int,
    parent_budget_remaining: int,
    flavour: str,
    task: str,
    child_workspace: str | None = None,
    requested_budget: int | None = None,
) -> dict:
    """Validate and assemble everything a child needs. Raises SubagentRefused.

    Separate from running it so every refusal is testable without a model:
    depth, attenuation and budget are the three ways a dispatch should be
    turned down, and each has to be provable on its own.
    """
    if parent_depth >= MAX_DEPTH:
        raise SubagentRefused(
            f"depth {parent_depth} is already the ceiling ({MAX_DEPTH}); a "
            "sub-agent cannot dispatch another. Do this work yourself."
        )
    if not (task or "").strip():
        raise SubagentRefused("a sub-agent needs a task")

    try:
        child_envelope = derive_child_envelope(
            parent_envelope, flavour, child_workspace)
        assert_attenuated(parent_envelope, child_envelope)
    except AttenuationError as e:
        raise SubagentRefused(f"cannot attenuate the envelope: {e}") from None

    budget = slice_budget(parent_budget_remaining, requested_budget)
    if budget <= 0:
        raise SubagentRefused(
            "no budget left to slice; the parent cannot afford a child and "
            "still read its answer"
        )

    logger.info(
        "subagent dispatch: flavour=%s depth=%d tools=%d budget=%d",
        flavour, parent_depth + 1, len(child_envelope.allowed_tools), budget,
    )
    return {
        "envelope": child_envelope,
        "depth": parent_depth + 1,
        "token_budget": budget,
        "step_budget": CHILD_STEP_BUDGET,
        "messages": [{"role": "user",
                      "content": build_child_prompt(flavour, task)}],
        "flavour": flavour,
    }
