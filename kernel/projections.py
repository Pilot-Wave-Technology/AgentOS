"""H3 — what is happening, and how we know.

An assistant asked "what is happening with the rename story" has three
kinds of answer available and they are not interchangeable:

  observed   a durable row the harness itself wrote. The state, the
             journal, a verdict. If this is wrong the database is wrong.
  derived    arithmetic over observed rows — step counts, elapsed time,
             how many legs. Wrong only if the arithmetic is.
  unproven   everything else. A model said it. A cache holds it. It was
             true twenty minutes ago. It may well be right; nothing here
             checked.

THE WHOLE POINT IS THAT THE THIRD ONE IS SAYABLE. An assistant that
cannot distinguish them reports all three in the same voice, and the
listener has no way to tell "the tests passed" (a verdict row) from "the
tests passed" (the model said so in its last message). The second is the
one that has been wrong all week — every defect this stage found was a
confident statement with nothing behind it.

So a projection is not a dict of values. It is a dict of `Fact`, and a
Fact cannot be built without saying where it came from.

WHY NOT JUST OMIT UNPROVEN FACTS. Because "I don't know" is an answer a
person can act on and silence is not. A story whose last verdict is
twenty minutes stale and whose model claims completion is a story someone
should look at; dropping the claim hides the discrepancy that makes it
interesting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

OBSERVED = "observed"
DERIVED = "derived"
UNPROVEN = "unproven"

_RANK = {OBSERVED: 0, DERIVED: 1, UNPROVEN: 2}


@dataclass(frozen=True)
class Fact:
    """One value and its provenance. Never one without the other."""

    value: Any
    provenance: str
    source: str = ""

    def __post_init__(self):
        if self.provenance not in _RANK:
            raise ValueError(
                f"{self.provenance!r} is not a provenance; expected one of "
                f"{', '.join(_RANK)}. A fact with no provenance is the thing "
                f"this type exists to prevent.")

    @property
    def trustworthy(self) -> bool:
        """Did the harness see this itself?"""
        return self.provenance in (OBSERVED, DERIVED)

    def render(self) -> str:
        """How it should be said out loud."""
        if self.provenance == UNPROVEN:
            return f"{self.value} (unproven — {self.source or 'no source'})"
        return str(self.value)


def observed(value, source: str) -> Fact:
    return Fact(value, OBSERVED, source)


def derived(value, source: str) -> Fact:
    return Fact(value, DERIVED, source)


def unproven(value, source: str) -> Fact:
    return Fact(value, UNPROVEN, source)


def weakest(*facts: Optional[Fact]) -> str:
    """The provenance a statement combining these facts may claim.

    A sentence built from one observed row and one model claim is a model
    claim. Taking the strongest would let a single durable field launder
    everything said alongside it.
    """
    present = [f for f in facts if f is not None]
    if not present:
        return UNPROVEN
    return max((f.provenance for f in present), key=lambda p: _RANK[p])


async def project_execution(conn, execution_id: int) -> dict:
    """Everything durable about one run, each field tagged.

    Reads only rows the harness wrote. Anything a model asserted arrives
    through `claim` below, separately, and never mixes silently.
    """
    row = await conn.fetchrow(
        "SELECT id, kind, domain, state, generation, goal_id, "
        "stall_reason, tokens_to_first_write, context_at_first_write "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        return {"execution_id": unproven(execution_id, "no such row")}

    steps = await conn.fetchval(
        "SELECT count(*) FROM step_journal WHERE execution_id = $1",
        execution_id)
    last_step = await conn.fetchval(
        "SELECT max(ts) FROM step_journal WHERE execution_id = $1",
        execution_id)
    verdict = await conn.fetchrow(
        "SELECT passed, created_at FROM verdicts WHERE execution_id = $1 "
        "ORDER BY id DESC LIMIT 1", execution_id)
    question = await conn.fetchrow(
        "SELECT id, question_text FROM questions WHERE execution_id = $1 "
        "AND status = 'OPEN' ORDER BY id DESC LIMIT 1", execution_id)

    out = {
        "execution_id": observed(row["id"], "executions.id"),
        "state": observed(row["state"], "executions.state"),
        "generation": observed(row["generation"], "executions.generation"),
        "steps": derived(steps or 0, "count(step_journal)"),
        "legs": derived((row["generation"] or 0) + 1,
                        "executions.generation + 1"),
    }
    if row["stall_reason"]:
        out["stalled"] = observed(row["stall_reason"],
                                  "executions.stall_reason")
    if last_step is not None:
        out["last_activity"] = observed(last_step, "max(step_journal.ts)")
    if verdict is not None:
        out["verdict"] = observed(
            "passed" if verdict["passed"] else "refused",
            "verdicts (latest)")
    if question is not None:
        out["waiting_on"] = observed(question["question_text"],
                                     f"questions.{question['id']}")
    return out


def claim(text: str, said_at: str = "the model's last turn") -> Fact:
    """Something a model asserted. Never observed, whatever it says.

    A model reporting "done" is evidence about the model, not about the
    work — which is the distinction `propose_completion` and the Verifier
    exist to enforce, and this is the same rule for prose.
    """
    return unproven(text, said_at)


def render(projection: dict) -> str:
    """One line per field, unproven ones marked as such."""
    lines = []
    for key, fact in projection.items():
        lines.append(f"{key}: {fact.render()}")
    return "\n".join(lines)
