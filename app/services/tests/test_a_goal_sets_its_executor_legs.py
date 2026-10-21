"""A goal sets its executor's leg size and leg count; the run obeys.

Set on the goal's settings page beside the executor model, frozen onto
the run at dispatch (the pod can read executions, not goals), applied by
the worker to the WORK phase. The review keeps its own budget.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION, migration_text
from kernel.harness.models import leg_choice

# The distribution ships squashed, numbered migrations; the same change is
# `0008_goal_leg_budget` there.
_MIGRATION = "0008_goal_leg_budget" if IS_DISTRIBUTION else "goal_leg_budget"

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the rules ───────────────────────────────────────────────────────────

def test_blank_means_the_defaults():
    assert leg_choice.valid("", None) == (None, None)
    assert leg_choice.valid("  ", "") == (None, None)


def test_values_are_normalised():
    assert leg_choice.valid("200,000", "8") == (200_000, 8)
    assert leg_choice.valid(None, "-1") == (None, -1)


@pytest.mark.parametrize("tokens, legs", [
    ("5000", None),          # below the floor: a leg that cannot orient
    ("2000000", None),       # above any window
    (None, "0"),             # zero legs ends the run before it starts
    (None, "-5"),
    (None, "999"),
    ("lots", None),
])
def test_values_outside_the_rules_are_refused(tokens, legs):
    with pytest.raises(ValueError):
        leg_choice.valid(tokens, legs)


def test_a_leg_is_never_larger_than_its_model_can_hold():
    choice = {"context_tokens": 900_000}
    assert leg_choice.tokens_for(choice, 200_000) == 200_000 - leg_choice.REPLY_HEADROOM
    assert leg_choice.tokens_for(choice, 1_048_576) == 900_000
    assert leg_choice.tokens_for({}, 200_000) is None


# ── wired where it must be ──────────────────────────────────────────────

def test_dispatch_freezes_it():
    src = _read("kernel/conductor/sandbox_spec.py")
    assert "leg_choice.freeze(" in src
    assert "leg_choice.chosen_for_goal(conn, row.get(\"goal_id\"))" in src


def test_the_worker_applies_it_to_the_work_and_not_the_review():
    src = _read("kernel/worker/loop.py")
    assert 'goal_legs = {} if phase == "review" else await leg_choice.frozen(' in src
    assert src.count('goal_legs.get("max_legs") or profile.max_legs or max_legs()') == 2, (
        "the run's ceiling and the errand path must answer to the same limit")
    assert '**({"token_budget": goal_tokens} if goal_tokens else {})' in src


def test_the_settings_page_offers_and_saves_it():
    page = _read("app/templates/goals/model_config.html")
    assert 'name="leg_context_tokens"' in page
    assert 'name="max_legs"' in page
    route = _read("app/routers/goals.py")
    assert "valid_legs(form.get(\"leg_context_tokens\")," in route
    assert "leg_context_tokens = $5" in route and "max_legs = $6" in route


def test_the_migration_is_sequenced():
    seq = _read("migrations/SEQUENCE").split()
    assert _MIGRATION in seq
    sql = migration_text(_MIGRATION)
    assert sql, f"migrations/{_MIGRATION}.sql is missing"
    assert "ADD COLUMN IF NOT EXISTS leg_context_tokens" in sql
    assert "ADD COLUMN IF NOT EXISTS leg_budget JSONB" in sql


# ── against a real database ─────────────────────────────────────────────

async def _connect_or_skip():
    asyncpg = pytest.importorskip("asyncpg")
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL")):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}), 5)
        except Exception:                                   # noqa: BLE001
            continue
        has = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'executions' AND column_name = 'leg_budget'")
        if has:
            return conn
        await conn.close()
    pytest.skip("no database with the leg_budget column")


def test_a_goal_choice_is_frozen_once_and_read_back():
    async def body():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            tag = os.urandom(4).hex()
            goal = f"zz-legs-{tag}"
            await conn.execute(
                "INSERT INTO goals (id, name, created_by, leg_context_tokens, "
                "max_legs) VALUES ($1, 't', 'tester', 150000, 6)", goal)
            eid = await conn.fetchval(
                "INSERT INTO executions (kind, domain, goal_id, state, task) "
                "VALUES ('story', 'coding', $1, 'PENDING', 't') RETURNING id",
                goal)
            choice = await leg_choice.chosen_for_goal(conn, goal)
            assert choice == {"context_tokens": 150_000, "max_legs": 6}
            await leg_choice.freeze(conn, eid, choice)
            assert await leg_choice.frozen(conn, eid) == choice

            # Editing the goal changes the NEXT run, not this one.
            await conn.execute(
                "UPDATE goals SET max_legs = 3 WHERE id = $1", goal)
            await leg_choice.freeze(
                conn, eid, await leg_choice.chosen_for_goal(conn, goal))
            assert await leg_choice.frozen(conn, eid) == choice

            # Nobody chose: nothing is frozen.
            other = await conn.fetchval(
                "INSERT INTO executions (kind, domain, goal_id, state, task) "
                "VALUES ('story', 'coding', $1, 'PENDING', 't') RETURNING id",
                goal)
            await leg_choice.freeze(conn, other, {})
            assert await leg_choice.frozen(conn, other) == {}
            raw = await conn.fetchval(
                "SELECT leg_budget FROM executions WHERE id = $1", other)
            assert raw is None
            json.dumps(choice)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(body())
