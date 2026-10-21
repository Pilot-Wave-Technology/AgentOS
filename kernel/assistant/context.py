"""H8 — which story an instruction is for, decided BEFORE the tool runs.

The assistant is scoped per (goal, user) and holds no workspace, so N
stories through one thread is the normal case rather than a stretch. The
failure that matters is therefore not load. It is an instruction landing
on the WRONG story, silently — the work goes somewhere plausible, the
run looks healthy, and nobody finds out until a person reads a diff they
did not ask for.

H4 settled the rule and proved it. This is the part that makes it
STRUCTURAL: every write tool resolves its target here, first, and an
`Ambiguous` result means the tool does not execute at all. It hands back
the question and the candidates instead.

WHY NOT LET THE MODEL DECIDE. Because it will decide. That is precisely
the failure — with three stories in flight and no focus, picking one is a
coin flip whose loss is invisible, and "ask if you are unsure" is advice
in a place where a refusal is available. The same judgement F11 makes
about criteria and H1's evidence floor makes about questions: where the
harness cannot know, it says so rather than guessing well.

WHAT IS NOT HERE. No parsing of what the person said. `resolve` takes a
hint that has already been extracted; turning "the rename one" into a
hint is the assistant's job, and a matcher here would create a second
place where that judgement lives.
"""

from __future__ import annotations

from dataclasses import dataclass

from kernel.addressing import Ambiguous, Anchor, resolve


@dataclass(frozen=True)
class Refused:
    """A typed refusal the assistant can read and act on.

    Deliberately TRUTHY, for the reason `mailbox.schema.Invalid` records
    at length: a sentinel whose falsiness contradicts its meaning
    silently disables every `if refused:` guard written against it. That
    mistake has been made once in this codebase already and it disabled
    an entire validation module.

    Its own type rather than an import of `Invalid`: this is a refusal of
    an ASSISTANT ACTION, not of a malformed payload, and the two travel
    to different places. Sharing the name would make a schema error and
    an ambiguous story read identically at the call site.
    """

    code: str
    message: str
    candidates: tuple = ()


async def in_flight(conn, goal_id: str) -> tuple:
    """Every execution in the goal that could still be meant.

    Terminal is read from each run's OWN machine rather than from a
    constant. A run carried forward to a newer machine may have terminal
    states the one before it did not, and a hardcoded list here would
    quietly start offering finished stories as candidates.
    """
    rows = await conn.fetch(
        """
        SELECT e.id
          FROM executions e
          JOIN run_state_machine m ON m.version = e.machine_version
         WHERE e.goal_id = $1
           AND NOT (m.terminal_states @> to_jsonb(e.state))
         ORDER BY e.id
        """,
        goal_id)
    return tuple(r["id"] for r in rows)


async def anchors_for(conn, goal_id: str) -> tuple:
    """The cards in front of the person, each pinned to one story.

    ONE ANCHOR PER QUESTION, not per card. A batch groups questions
    across a goal for presentation (H5), so a batch of three can span
    three executions — and an anchor carries exactly one. Anchoring on
    the batch would make "reply to that card" ambiguous by construction
    in the ordinary case the batching window was built for.
    """
    rows = await conn.fetch(
        """
        SELECT q.id, q.execution_id, q.question_text
          FROM questions q
          JOIN executions e ON e.id = q.execution_id
         WHERE e.goal_id = $1 AND q.status = 'OPEN'
         ORDER BY q.created_at
        """,
        goal_id)
    return tuple(Anchor(anchor_id=f"q-{r['id']}",
                        execution_id=r["execution_id"],
                        label=(r["question_text"] or "")[:120])
                 for r in rows)


async def target(conn, goal_id: str, hint=None, focus=None):
    """Where does this instruction go? An `Address`, or a `Refused`.

    Returns `Refused` rather than `Ambiguous` so that every write tool
    has one thing to check and one shape to hand back.
    """
    got = resolve(hint, focus,
                  anchors=await anchors_for(conn, goal_id),
                  in_flight=await in_flight(conn, goal_id))
    if isinstance(got, Ambiguous):
        return Refused("E_AMBIGUOUS_STORY",
                       f"{got.question} ({got.reason})",
                       candidates=got.candidates)
    return got


async def owns(conn, goal_id: str, execution_id: int) -> bool:
    """Is this execution in the goal the assistant is scoped to?

    An assistant is per (goal, user). Every write checks this even when
    the id came from `resolve`, because a tool argument can also be typed
    by the model from something it read in its own transcript — and a
    steer that crosses into another goal is the multi-tenant version of
    the wrong-story failure this module exists to stop.
    """
    got = await conn.fetchval(
        "SELECT goal_id FROM executions WHERE id = $1", execution_id)
    return got is not None and got == goal_id
