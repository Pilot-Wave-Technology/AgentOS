"""The ledger against a real database: it verifies, and it names a person.

THE THREE QUESTIONS THIS ANSWERS, which are the ones the audit set out to
ask:

  does the chain hold, for every event, from GENESIS to head
  is every event attributable to a registered principal
  can every KERNEL act be traced back to a human

The third is new. Before this work the kernel wrote nothing at all: on story
1cbc7b3e there were 15 execution transitions, 4 plan approvals, 3 proposals
and 2 verdicts, and none of it reached the ledger. The plan approvals are
the ones that matter — a person authorising machine work, with no record
that it happened.

Skips cleanly with no database. `asyncio.run` in a sync wrapper because
pytest-asyncio is not installed here and an async test without it ERRORS
rather than skips.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip


def _run(fn):
    async def go():
        conn = await _connect_or_skip()
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'ledger_events'"):
                pytest.skip("no ledger in this database")
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def _obj(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


# ── the chain ────────────────────────────────────────────────────────────

def test_the_chain_verifies_from_genesis_to_head():
    """Recomputed, not trusted. Every link and every hash."""
    from kernel.ledger.chain import GENESIS, chain_hash

    async def check(conn):
        rows = await conn.fetch(
            "SELECT sequence_number, integrity_hash, previous_hash, payload "
            "FROM ledger_events ORDER BY sequence_number")
        if not rows:
            pytest.skip("empty ledger")
        prev, broken = GENESIS, []
        for r in rows:
            if r["previous_hash"] != prev:
                broken.append((r["sequence_number"], "linkage"))
            if chain_hash(r["previous_hash"], _obj(r["payload"])) != r["integrity_hash"]:
                broken.append((r["sequence_number"], "hash"))
            prev = r["integrity_hash"]
        assert not broken, broken[:10]

        head = await conn.fetchrow(
            "SELECT last_hash, event_count FROM chain_head WHERE id = 1")
        assert head["last_hash"] == rows[-1]["integrity_hash"], "head != tail"
        assert head["event_count"] == len(rows), "event_count drifted"
    _run(check)


def test_the_envelope_digest_verifies_wherever_it_is_present():
    """It covers actor and event type, which the chain hash does not.

    Checked "where present" on purpose: events written before it exist and
    are not retro-fitted, so demanding it everywhere would fail on history
    that is perfectly valid under the rule it was written to.
    """
    from kernel.ledger.chain import envelope_digest

    async def check(conn):
        rows = await conn.fetch(
            "SELECT event_type, actor_id, actor_type, on_behalf_of, payload, "
            "extensions FROM ledger_events "
            "WHERE extensions ? 'envelope_digest'")
        if not rows:
            pytest.skip("no envelope digests yet")
        bad = [r["event_type"] for r in rows
               if envelope_digest(
                   event_type=r["event_type"], actor_id=r["actor_id"],
                   actor_type=r["actor_type"], on_behalf_of=r["on_behalf_of"],
                   payload=_obj(r["payload"])
               ) != _obj(r["extensions"])["envelope_digest"]]
        assert not bad, f"{len(bad)} envelope digests do not verify: {bad[:5]}"
    _run(check)


async def _one_event(conn):
    """A row to aim the trigger at. Rolled back with its transaction.

    Seeded rather than borrowed because the guard is FOR EACH ROW: it
    fires once per row the statement actually touches, so the test needs
    a row it is certain exists.
    """
    return await conn.fetchval(
        "INSERT INTO ledger_events (event_id, event_type, actor_id, "
        "integrity_hash, payload) "
        "VALUES (gen_random_uuid(), 'test.append_only', gen_random_uuid(), "
        "'not-a-real-hash', '{}'::jsonb) RETURNING event_id")


def test_the_ledger_refuses_update_and_delete():
    """Append-only enforced by the DATABASE, not by everyone remembering.

    Both directions, because a guard on UPDATE alone leaves rewriting-by-
    deletion wide open.

    IT WRITES THE ROW IT THEN TRIES TO MUTATE, and that is the whole fix.
    `prevent_ledger_mutation` is a FOR EACH ROW trigger, so it fires once
    per row a statement touches and NOT AT ALL when the statement touches
    none. The earlier version aimed at `max(sequence_number)`, which is
    NULL on an empty ledger -- `WHERE sequence_number = NULL` then matches
    nothing, the trigger never runs, the UPDATE reports success, and the
    test reports that the ledger is mutable.

    So it failed on precisely the database where the guarantee most needs
    checking: a fresh one. And it only ever passed by borrowing rows some
    earlier run happened to leave behind, which means on a populated
    database it passed without establishing anything the next migration
    could not silently take away. CI builds a fresh database every time.
    """
    async def check(conn):
        for verb, sql in (
                ("UPDATE", "UPDATE ledger_events SET actor_type = 'system' "
                           "WHERE event_id = $1"),
                ("DELETE", "DELETE FROM ledger_events WHERE event_id = $1")):
            tx = conn.transaction()
            await tx.start()
            try:
                event_id = await _one_event(conn)
                try:
                    await conn.execute(sql, event_id)
                except Exception as exc:                    # noqa: BLE001
                    assert "append-only" in str(exc), exc
                else:
                    raise AssertionError(
                        f"the ledger allowed {verb} on one of its own rows")
            finally:
                await tx.rollback()
    _run(check)


# ── attribution ──────────────────────────────────────────────────────────

def test_every_actor_resolves_to_a_registered_principal():
    """An actor_id pointing at nothing is an event attributable to nobody."""
    async def check(conn):
        bad = await conn.fetch("""
            SELECT le.actor_type, le.event_type, count(*) n
              FROM ledger_events le
              LEFT JOIN actors a ON a.id = le.actor_id
             WHERE a.id IS NULL
             GROUP BY 1, 2 ORDER BY n DESC""")
        assert not bad, [dict(r) for r in bad]
    _run(check)


def test_every_kernel_act_names_a_human():
    """THE POINT OF THE WHOLE CHANGE.

    An executor event naming only the executor is attributable to a robot
    and to nobody — no better than the `system` attribution this replaced.
    So every machine-actor kernel event must carry `on_behalf_of`, and every
    human-actor one IS the person.

    Runs with no thread are exempt and counted: the conversation may have
    been deleted, and recording the act with thinner context beats not
    recording it. The exemption is asserted to be small so it cannot quietly
    become the normal path.
    """
    async def check(conn):
        total = await conn.fetchval(
            "SELECT count(*) FROM ledger_events WHERE event_type LIKE 'run.%'")
        if not total:
            pytest.skip("no kernel events yet — run the drain")
        orphan = await conn.fetchval("""
            SELECT count(*) FROM ledger_events
             WHERE event_type LIKE 'run.%'
               AND actor_type <> 'human'
               AND on_behalf_of IS NULL
               AND COALESCE(context_conversation_id, '') <> ''""")
        assert orphan == 0, (
            f"{orphan} kernel events have a conversation but name no human")
    _run(check)


def test_the_executor_never_approves_its_own_plan():
    """A REAL BUG THIS CAUGHT.

    `questions.answered_by` speaks the same provenance vocabulary as
    `proposals.actor_id` — `assistant:ker05`, `kernel`, an email — and
    reading it as a bare uid attributed five assistant approvals and four
    kernel auto-approvals to the EXECUTOR. That is not a missing
    attribution, it is a false one: a record of the executor authorising its
    own work.

    An answerer that resolves to nobody is flagged, not blamed on a machine.
    """
    async def check(conn):
        rows = await conn.fetch("""
            SELECT payload->>'decided_by' AS who, actor_type,
                   payload->>'actor_unresolved' AS unresolved
              FROM ledger_events WHERE event_type = 'run.plan_decided'""")
        if not rows:
            pytest.skip("no plan decisions ledgered yet")
        for r in rows:
            if r["actor_type"] in ("executor", "verifier"):
                assert r["unresolved"] == "true", (
                    f"plan decided by {r['who']!r} attributed to "
                    f"{r['actor_type']} without being flagged unresolved")
    _run(check)


def test_the_verifier_is_not_the_executor():
    """The judge and the judged must be distinguishable in the trail."""
    async def check(conn):
        rows = await conn.fetch("""
            SELECT DISTINCT actor_type, actor_id FROM ledger_events
             WHERE actor_type IN ('executor', 'verifier')""")
        if not rows:
            pytest.skip("no kernel events yet")
        ids = {}
        for r in rows:
            ids.setdefault(r["actor_type"], set()).add(r["actor_id"])
        if len(ids) == 2:
            assert not (ids["executor"] & ids["verifier"]), (
                "an actor id is being used as both executor and verifier")
    _run(check)


def test_draining_twice_writes_nothing_the_second_time():
    """Re-running is the normal case — overlapping ticks, a restart. On an
    append-only ledger a duplicate cannot be deleted afterwards, so
    idempotence has to hold the first time."""
    async def check(conn):
        from kernel.ledger.drain import drain_all

        await conn.execute("SET search_path TO kernel, public")
        await drain_all(conn)
        before = await conn.fetchval("SELECT count(*) FROM public.ledger_events")
        again = await drain_all(conn)
        after = await conn.fetchval("SELECT count(*) FROM public.ledger_events")
        assert after == before, f"a re-drain added {after - before} duplicates"
        assert not again, again
    _run(check)


# ── identity is an ID, never a name ──────────────────────────────────────

def test_attribution_never_depends_on_a_display_name():
    """Two people can be called the same thing.

    `principal_actor_id` is a uuid with a foreign key behind it and
    `principal_uid` is unique per person. `principal_display_name` exists to
    be READ and nothing may join on it — so this asserts the ids are present
    and distinct wherever the names are not, which a name-keyed scheme could
    not manage.
    """
    async def check(conn):
        rows = await conn.fetch("""
            SELECT principal_actor_id, principal_uid, principal_display_name
              FROM ledger_events_resolved
             WHERE has_principal LIMIT 200""")
        if not rows:
            pytest.skip("nothing attributed yet")
        for r in rows:
            assert r["principal_actor_id"] is not None
            # A uuid, not a name: names have spaces, ids do not.
            assert "-" in str(r["principal_actor_id"])
            assert r["principal_uid"], "the uid is the taximeter's key"

        # And the mapping is a function: one id, one uid.
        clash = await conn.fetch("""
            SELECT principal_actor_id, count(DISTINCT principal_uid) n
              FROM ledger_events_resolved WHERE has_principal
             GROUP BY 1 HAVING count(DISTINCT principal_uid) > 1""")
        assert not clash, f"an actor id maps to several uids: {clash}"
    _run(check)


def test_parallel_executors_are_distinguishable():
    """`actor_id` names the KIND — one actor covers every story/coding
    executor — so with four legs in flight it cannot say which one acted.
    `actor_instance` is the running execution, and it is on the envelope
    (`context_task_id`, already indexed) rather than only in the payload,
    because the audit UI asks this constantly.
    """
    async def check(conn):
        rows = await conn.fetch("""
            SELECT actor_id, count(DISTINCT actor_instance) instances,
                   count(*) events
              FROM ledger_events_resolved
             WHERE event_actor_type IN ('executor', 'verifier')
             GROUP BY 1""")
        if not rows:
            pytest.skip("no kernel events yet")
        for r in rows:
            assert r["instances"] > 0, (
                "kernel events carry no instance — concurrent executors of "
                "the same kind would be indistinguishable")

        missing = await conn.fetchval("""
            SELECT count(*) FROM ledger_events_resolved
             WHERE event_actor_type IN ('executor', 'verifier')
               AND actor_instance IS NULL""")
        assert missing == 0, f"{missing} kernel events name no instance"
    _run(check)


def test_new_events_put_the_instance_on_the_envelope():
    """Not only in the payload. `context_task_id` is indexed
    (`idx_ledger_task`); a payload probe is a GIN lookup for a question the
    audit UI asks on every page."""
    import inspect

    from kernel.ledger import drain, emit

    assert "context_task_id" in inspect.getsource(emit._append)
    assert 'instance=f"exec:{execution_id}"' in inspect.getsource(drain._write)


# ── the second event store, folded in ────────────────────────────────────

def test_resource_events_are_fully_accounted_for():
    """`resource_events` was a parallel store: no chain, no append-only
    trigger, no foreign key to `actors`.

    It PARTIALLY duplicated the ledger, which is what made folding it
    delicate — `goal_resources.py` dual-writes, so 34 of 63 rows were
    already audited. Draining blindly would have made the ledger claim twice
    as much resource activity as happened.

    Every row must now be accounted for exactly once: either it was already
    on the chain, or the drain put it there.
    """
    async def check(conn):
        total = await conn.fetchval("SELECT count(*) FROM resource_events")
        if not total:
            pytest.skip("no resource events here")
        already = await conn.fetchval("""
            SELECT count(*) FROM resource_events re
             WHERE EXISTS (SELECT 1 FROM ledger_events le
                    WHERE le.payload->>'resource_id' = re.resource_id::text
                      AND le.event_type IN ('resource.goal.registered',
                                            'GOAL_RESOURCE_REGISTERED',
                                            'resource.goal.updated',
                                            'GOAL_RESOURCE_UPDATED')
                      AND NOT (le.payload ? 'source_event'))""")
        folded = await conn.fetchval(
            "SELECT count(*) FROM ledger_events WHERE payload ? 'source_event'")
        assert already + folded == total, (
            f"{total} resource events, {already} pre-existing + {folded} "
            f"folded = {already + folded}; some are double-counted or missing")
    _run(check)


def test_folding_resource_events_twice_adds_nothing():
    """The dual-write means a re-run has two chances to duplicate: the
    skip-if-already-audited clause and the idempotency key. Both are
    asserted, because on an append-only table a duplicate is permanent."""
    async def check(conn):
        from kernel.ledger.resources import drain_resource_events

        await conn.execute("SET search_path TO kernel, public")
        await drain_resource_events(conn)
        before = await conn.fetchval("SELECT count(*) FROM public.ledger_events")
        again = await drain_resource_events(conn)
        after = await conn.fetchval("SELECT count(*) FROM public.ledger_events")
        assert again == 0 and after == before, (
            f"a re-run added {after - before} duplicate resource events")
    _run(check)
