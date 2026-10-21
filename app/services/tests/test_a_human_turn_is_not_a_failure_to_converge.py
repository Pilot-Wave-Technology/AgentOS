"""Answering a park must not spend the run's convergence budget.

THE ARITHMETIC THAT MADE THIS NECESSARY. `_legs_taken` counts DISTINCT
generations against the profile's ceiling (12 for work, 3 for a review),
and every park-and-resume advances the generation. So:

  * Rejecting a plan cost ONE leg per rejection. Twelve revisions and the
    run was FAILED with "12 legs without converging" -- a message about an
    executor that looped, for a reviewer who was being careful.

  * "Ask for more work" cost TWO legs per round, because the acceptance
    answer submits a `replan` that withdraws the plan approval: one leg to
    register the new plan and park, one to do the work and park again.
    Four or five rounds exhausted a coding story -- and that button is on
    the acceptance card, so it is a workflow the product offers.

WHAT THE CEILING IS FOR is a run that will not converge while NOBODY IS
WATCHING. A person answering a park has just read the plan or the result
and said what happens next. That is the opposite of unattended.

WHY THE RESET IS GATED ON `human_uid`. If an agent could trigger it there
would be no ceiling at all: a model could ask a `clarification`, have the
assistant answer it -- which is allowed, and is what makes `ask_human`
cheap -- and buy a fresh twelve legs each time. The GO settling an
autonomous errand's plan alone is the same shape. `answered_by` is a free
string an executor fills in; `human_uid` is the kernel's own
"a person decided this" discriminator and defaults to None.
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the reset ────────────────────────────────────────────────────────

def test_resuming_after_a_person_restarts_the_budget():
    src = _read("kernel/harness/park_resolve.py")
    i = src.index("SET state = 'RUNNING', generation = generation + 1")
    stmt = src[i:i + 400]
    assert "convergence_generation = CASE WHEN $2::bool" in stmt, (
        "answering a park still spends the convergence budget")
    assert "generation + 1 " in stmt


def test_an_agent_cannot_reset_it():
    """The whole safety of the change. Gated on `human_uid`, which an
    executor cannot set, not on the park's kind -- `clarification` is
    assistant-answerable by design and GO may settle an autonomous
    errand's plan alone."""
    src = _read("kernel/harness/park_resolve.py")
    i = src.index("convergence_generation = CASE WHEN $2::bool")
    call = src[i:i + 600]
    assert 'bool((human_uid or "").strip())' in call, (
        "the reset is not gated on a real person")
    assert "answered_by" not in call, (
        "answered_by is a free string an executor fills in; it cannot "
        "gate this")


# ── the ceiling ──────────────────────────────────────────────────────

def test_the_ceiling_counts_from_the_later_mark():
    src = _read("kernel/worker/loop.py")
    assert "async def _budget_mark(" in src
    fn = src[src.index("async def _budget_mark("):]
    fn = fn[:fn.index("async def", 10)]
    assert "phase_generation" in fn and "convergence_generation" in fn
    assert "max(" in fn, "one mark wins outright instead of the later one"


def test_the_mark_is_read_from_the_database_not_the_row():
    """`claimable()` selects nine columns and neither mark is among them,
    so `row.get("phase_generation")` was None on that path. Reading the
    row here costs one cheap query and removes a plumbing rule every
    future caller would have to remember."""
    src = _read("kernel/worker/loop.py")
    fn = src[src.index("async def _budget_mark("):]
    fn = fn[:fn.index("async def", 10)]
    assert "conn.fetchrow(" in fn
    # The ASSIGNMENT, not any mention — the docstring above explains the
    # old form and would match a naive search for it.
    assert 'since = int(row.get("phase_generation")' not in src, (
        "the ceiling still trusts the caller's dict")
    assert "since = await _budget_mark(conn, execution_id)" in src


def test_the_phase_mark_still_works():
    """The review phase's own ceiling was the original reason a mark
    exists. It must survive: a story that took nine legs still gets three
    review legs, not none."""
    src = _read("kernel/worker/loop.py")
    fn = src[src.index("async def _budget_mark("):]
    fn = fn[:fn.index("async def", 10)]
    assert "COALESCE(phase_generation, 0)" in fn
    fin = _read("kernel/worker/finish.py")
    assert "phase_generation = generation + 1" in fin, (
        "the review phase no longer sets its mark")


# ── the column ───────────────────────────────────────────────────────

def test_the_column_exists_and_nullable_means_never():
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, create_table

    if IS_DISTRIBUTION:
        import re

        col = re.search(r"^\s+convergence_generation\s+(.*)$",
                        create_table("kernel.executions"), re.MULTILINE)
        assert col, "kernel.executions has no convergence_generation"
        assert col.group(1).startswith("integer")
        assert "NOT NULL" not in col.group(1), (
            "a NOT NULL DEFAULT 0 would be indistinguishable from 'reset at "
            "generation 0', which is every run's start")
        return
    sql = _read("migrations/convergence_ceiling_excludes_human_turns.sql")
    assert "ADD COLUMN IF NOT EXISTS convergence_generation INTEGER" in sql
    assert "NOT NULL" not in sql.split("ADD COLUMN")[1][:120], (
        "a NOT NULL DEFAULT 0 would be indistinguishable from 'reset at "
        "generation 0', which is every run's start")
    seq = _read("migrations/SEQUENCE")
    assert "convergence_ceiling_excludes_human_turns" in seq
