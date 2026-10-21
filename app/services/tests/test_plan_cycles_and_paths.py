"""Cycles: what opens one, what closes it, and what the plan file is called.

A CYCLE IS ONE AGREED INTENT, not one plan version. A parked plan may be
reworded any number of times while a person reads it, and every one of
those is the same cycle and the same `plan-NN.md`. A new number is taken
only when the last cycle has been SPENT and somebody asks for more.

WHY THIS IS PINNED. The cycle number is the plan's filename, the filename
rides the story's commits into the pull request, and three modules used to
derive "which cycle" with three different queries. The failure mode of a
disagreement is a plan written over another plan.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _approved(h, story_id: str, version: int, *, cycle: int | None = None,
                    kind: str = "work", status: str = "approved") -> None:
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
        "   approved_by) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, '{}'::jsonb, 'local', "
        "        $6, now(), 'person')",
        story_id, version, cycle, kind,
        f"agentos_plans/{story_id}/plan-{(cycle or version):02d}.md", status)


# ── reading where we are ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_story_with_no_plan_is_on_cycle_one():
    """Not zero. The first plan a person approves is cycle 1, so the
    number before it is approved has to be the one it will take."""
    from app.services.local_cycles import current_cycle, next_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        assert await current_cycle(h.conn, story) == 1
        assert await next_cycle(h.conn, story) == 1


@pytest.mark.asyncio
async def test_an_open_cycle_keeps_its_number():
    """Registering a replacement plan mid-cycle is the same intent being
    adjusted. Taking a new number there would leave `plan-01.md` and
    `plan-02.md` describing one piece of work."""
    from app.services.local_cycles import cycle_is_open, next_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1)
        assert await cycle_is_open(h.conn, story) is True
        assert await next_cycle(h.conn, story) == 1


@pytest.mark.asyncio
async def test_a_spent_cycle_moves_the_number_on():
    from app.services.local_cycles import cycle_is_open, next_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1, status="executed")
        assert await cycle_is_open(h.conn, story) is False
        assert await next_cycle(h.conn, story) == 2


@pytest.mark.asyncio
async def test_a_row_written_without_a_cycle_reads_as_its_version():
    """`plan_versions` has six other writers and not one is being edited,
    so a NULL `cycle` has to mean what it meant before the column: the
    version IS the cycle. Every pre-migration row is this shape."""
    from app.services.local_cycles import current_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 3, cycle=None, status="executed")
        assert await current_cycle(h.conn, story) == 3


@pytest.mark.asyncio
async def test_a_catch_up_plan_does_not_consume_a_cycle():
    """Phase 5 registers plans that take the trunk in and re-run checks.
    They are not a new intent, and counting them would renumber the plan
    files underneath a person mid-story."""
    from app.services.local_cycles import current_cycle, next_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1, status="executed")
        await _approved(h, story, 2, cycle=9, kind="catch_up")
        assert await current_cycle(h.conn, story) == 1
        assert await next_cycle(h.conn, story) == 2


# ── the plan file ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_plan_is_plan_01():
    from app.services.local_cycles import cycle_plan_path

    async with local_http() as h:
        _, story = await h.seed_story()
        assert await cycle_plan_path(h.conn, story) == (
            f"agentos_plans/{story}/plan-01.md")


@pytest.mark.asyncio
async def test_more_work_gets_the_next_file():
    from app.services.local_cycles import cycle_plan_path

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1, status="executed")
        assert await cycle_plan_path(h.conn, story) == (
            f"agentos_plans/{story}/plan-02.md")


@pytest.mark.asyncio
async def test_the_number_is_zero_padded_so_the_folder_sorts():
    from app.services.local_cycles import cycle_plan_path

    async with local_http() as h:
        _, story = await h.seed_story()
        for v in range(1, 10):
            await _approved(h, story, v, cycle=v, status="executed")
        assert (await cycle_plan_path(h.conn, story)).endswith("plan-10.md")


# ── spending one ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spending_closes_the_open_cycle():
    from app.services.local_cycles import cycle_is_open, spend_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1)
        assert await spend_cycle(h.conn, story, "submit") == 1
        assert await cycle_is_open(h.conn, story) is False


@pytest.mark.asyncio
async def test_spending_twice_is_a_no_op():
    """Both callers are retried paths — a submit can be re-posted, Mark
    Done can be clicked twice — and neither holds a lock."""
    from app.services.local_cycles import spend_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1)
        await spend_cycle(h.conn, story, "submit")
        assert await spend_cycle(h.conn, story, "submit") == 0


@pytest.mark.asyncio
async def test_spending_leaves_another_storys_cycle_alone():
    from app.services.local_cycles import cycle_is_open, spend_cycle

    async with local_http() as h:
        _, mine = await h.seed_story()
        _, theirs = await h.seed_story()
        await _approved(h, mine, 1, cycle=1)
        await _approved(h, theirs, 1, cycle=1)
        await spend_cycle(h.conn, mine, "submit")
        assert await cycle_is_open(h.conn, theirs) is True


# ── what the panel reads ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_cycle_block_says_none_before_anything_is_approved():
    from app.services.local_cycles import cycle_state

    async with local_http() as h:
        _, story = await h.seed_story()
        assert await cycle_state(h.conn, story) == {
            "number": 1, "plan_path": "", "status": "none", "kind": "work",
            "checks": []}


@pytest.mark.asyncio
async def test_the_cycle_block_tracks_open_then_spent():
    from app.services.local_cycles import cycle_state, spend_cycle

    async with local_http() as h:
        _, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1)
        before = await cycle_state(h.conn, story)
        assert before["status"] == "open"
        assert before["number"] == 1
        assert before["plan_path"].endswith("plan-01.md")
        await spend_cycle(h.conn, story, "submit")
        assert (await cycle_state(h.conn, story))["status"] == "spent"


@pytest.mark.asyncio
async def test_get_state_carries_the_cycle():
    """The contract half: the extension reads these keys by name."""
    async with local_http() as h:
        goal, story = await h.seed_story()
        await _approved(h, story, 1, cycle=1)
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        assert r.status_code == 200, r.text
        cycle = r.json()["cycle"]
        assert set(cycle) == {"number", "plan_path", "status", "kind", "checks"}
        assert cycle["number"] == 1 and cycle["status"] == "open"


# ── one writer, and this is how it stays that way ────────────────────

def test_nothing_else_spends_a_cycle_with_its_own_sql():
    """Three modules used to carry the same UPDATE. The point of the
    module is that there is one, and the reason to pin it is that adding
    a fourth is one line and invisible in review."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "services"
    offenders = [
        p.name for p in root.glob("local_*.py")
        if p.name != "local_cycles.py"
        # The UPDATE, not the word: `local_diff` legitimately READS
        # `status = 'executed'` to tell a spent cycle from an open one.
        and "SET status = 'executed'" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} spend a cycle directly; call `spend_cycle` so the log "
        "records which event closed it")
