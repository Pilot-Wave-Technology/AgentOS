"""chat_module data layer — all Postgres access for the queue + routing.

Uses the shared asyncpg pool (`app.database.get_pool`). No business logic here;
just durable reads/writes. The `messages` table doubles as the queue.

NOTIFY channels (signal-only, payload is small JSON):
  - `chat_msg`      {thread_id, message_id}  → web tier fan-out (Phase 2)
  - `chat_dispatch` {thread_id}              → dispatcher wake (Phase 3)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.database import get_pool

_log = logging.getLogger(__name__)

from .models import (
    Gateway,
    GatewayStatus,
    MessageKind,
    MessageStatus,
    QueuedMessage,
    ThreadContext,
)

CHANNEL_MSG = "chat_msg"
CHANNEL_DISPATCH = "chat_dispatch"


# ── helpers ──────────────────────────────────────────────────────────────────

def _row_to_message(row: asyncpg.Record) -> QueuedMessage:
    return QueuedMessage(
        id=row["id"],
        thread_id=row["thread_id"],
        sender=row["sender"],
        role=row["role"],
        kind=MessageKind(row["kind"]),
        content=row["content"],
        status=MessageStatus(row["status"]),
        idempotency_key=row["idempotency_key"],
        batch_id=row["batch_id"],
        created_at=row["created_at"],
    )


async def _notify(conn: asyncpg.Connection, channel: str, payload: dict) -> None:
    # pg_notify is transaction-aware: it fires on commit.
    await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(payload))


async def notify_dispatch(thread_id: str) -> None:
    """Standalone helper: NOTIFY chat_dispatch for a thread without
    requiring the caller to hold a connection. Used by _AgentSession when
    a compaction-end event arrives so the dispatcher resumes work on the
    deferred thread immediately rather than waiting for the next poll.
    """
    if not thread_id:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        await _notify(conn, CHANNEL_DISPATCH, {"thread_id": thread_id})


async def notify_new_message(thread_id: str, message_id: str,
                             conn: Optional[asyncpg.Connection] = None
                             ) -> None:
    """Signal the web-tier fan-out (CHANNEL_MSG only) that an already-persisted
    message row should be broadcast to browser subscribers on every instance.

    Deliberately does NOT emit CHANNEL_DISPATCH — used by card posters that
    INSERT a human-facing display message (card_data) directly and need
    cross-instance browser delivery WITHOUT waking the agent / burning a turn.

    PASS `conn` WHEN THE INSERT IS IN A TRANSACTION, and the errand
    approval card is why this parameter exists.

    `_notify` says "pg_notify is transaction-aware: it fires on commit",
    and that is true only of the transaction's OWN connection. Acquiring
    a fresh one from the pool fires the notify IMMEDIATELY -- while the
    caller's INSERT is still uncommitted. The listener wakes, runs
    `SELECT ... WHERE id = $1`, finds no row, and returns at its
    `if not row` guard. Silently: no error, no log line, no delivery.

    Measured on ERR09. `errand_proposal_cards` is the one card poster
    that wraps its insert in `conn.transaction()`, and it is the one
    card that never arrived live -- "the approval only shows after a
    refresh", for weeks, while every other card on the same machinery
    worked. Nothing was wrong with the fan-out, the registries or the
    renderer, which is where the search kept going.

    Passed a connection, the notify is queued and delivered by Postgres
    on commit, so the row is always visible by the time the listener
    looks.
    """
    if not thread_id or not message_id:
        return
    payload = {"thread_id": thread_id, "message_id": message_id}
    if conn is not None:
        await _notify(conn, CHANNEL_MSG, payload)
    else:
        pool = get_pool()
        async with pool.acquire() as own:
            await _notify(own, CHANNEL_MSG, payload)

    # AND THE CARD GOES TO SLACK, if it is a card. Every card poster
    # already calls this, which is what makes it the one place a new
    # card kind reaches Slack without anybody adding a call -- and
    # twenty-four of twenty-seven kinds reached it in no other way.
    #
    # The publisher side rather than the fan-out listener: that listener
    # runs in every web instance, so posting from there would post once
    # per instance.
    await _relay_card_if_any(thread_id, message_id, conn)



async def _relay_card_if_any(thread_id: str, message_id: str,
                             conn: Optional[asyncpg.Connection] = None
                             ) -> None:
    """Mirror a card-bearing message to the platforms. Never raises.

    ON THE CALLER'S CONNECTION WHEN THERE IS ONE, for the same reason
    the notify above takes it, and this is the second time that reason
    has had to be learned.

    The relay reads the message row back before it can decide whether
    there is a card to send. Read through `get_pool()`, that is a
    DIFFERENT connection, which cannot see a row still inside the
    caller's open transaction -- so it found nothing and returned, and
    the card reached the panel and never reached Slack. Silently, with
    no error and no log line, exactly as the notify once did.

    Observed on CALY5: the errand proposal card was on the GO thread in
    the UI and absent from its Slack channel. `errand_proposal_cards`
    now notifies after its commit and does not need this, but the next
    poster to wrap an insert in a transaction would have walked into it
    a third time.
    """
    try:
        from app.services.card_fanout import relay_card_for_message
        await relay_card_for_message(conn or get_pool(), thread_id,
                                     message_id)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("card relay skipped for %s: %s", message_id, exc)


# ── thread routing (captured at enqueue, read by dispatcher) ──────────────────

async def upsert_thread_routing(
    *, thread_id: str, goal_id: str, agent_id: str,
    gateway_agent_id: str, agent_type: str, gateway_id: Optional[str],
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO chat_thread_routing
            (thread_id, goal_id, agent_id, gateway_agent_id, agent_type, gateway_id, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (thread_id) DO UPDATE SET
            goal_id          = EXCLUDED.goal_id,
            agent_id         = EXCLUDED.agent_id,
            gateway_agent_id = EXCLUDED.gateway_agent_id,
            agent_type       = EXCLUDED.agent_type,
            gateway_id       = COALESCE(EXCLUDED.gateway_id, chat_thread_routing.gateway_id),
            updated_at       = now()
        """,
        thread_id, goal_id, agent_id, gateway_agent_id, agent_type, gateway_id,
    )


async def get_thread_context(thread_id: str) -> Optional[ThreadContext]:
    """Resolve routing for a thread. ws_url falls back to the single configured
    gateway when the thread/agent has no explicit gateway mapping yet."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT r.thread_id, r.goal_id, r.agent_id, r.gateway_agent_id,
               r.agent_type, r.gateway_id, g.ws_url AS gw_url
        FROM chat_thread_routing r
        LEFT JOIN gateways g ON g.id = r.gateway_id
        WHERE r.thread_id = $1
        """,
        thread_id,
    )
    if not row:
        return None
    ws_url = row["gw_url"]
    if not ws_url:
        # No explicit gateway mapping — fall back to the single configured one.
        from .config import get_chat_config
        ws_url = get_chat_config().gateway_ws_url
    return ThreadContext(
        thread_id=row["thread_id"],
        goal_id=row["goal_id"],
        agent_id=row["agent_id"],
        gateway_agent_id=row["gateway_agent_id"],
        agent_type=row["agent_type"],
        gateway_id=row["gateway_id"] or "",
        gateway_ws_url=ws_url,
    )


# ── enqueue (Phase 1: ack-on-enqueue) ─────────────────────────────────────────

async def mark_existing_queued(*, message_id: str, kind: MessageKind) -> None:
    """Flip an already-inserted message row to 'queued' and signal fan-out +
    dispatcher. Used when the caller (e.g. the chat router) persisted the row
    itself — avoids double-inserting."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE messages SET status = 'queued', kind = $2
                WHERE id = $1
                RETURNING thread_id
                """,
                message_id, kind.value,
            )
            if not row:
                return
            thread_id = row["thread_id"]
            await _notify(conn, CHANNEL_MSG,
                          {"thread_id": thread_id, "message_id": message_id})
            await _notify(conn, CHANNEL_DISPATCH, {"thread_id": thread_id})


async def enqueue(
    *, thread_id: str, sender: str, role: str, kind: MessageKind,
    content: str, idempotency_key: Optional[str] = None,
) -> str:
    """Durably persist an inbound message as `queued`, then signal fan-out +
    dispatcher. Returns the new message id. This is the durability boundary —
    callers may ack the sender once this returns."""
    message_id = str(uuid.uuid4())
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO messages
                    (id, thread_id, sender, role, content, kind, status,
                     idempotency_key, token_count)
                VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7, 0)
                """,
                message_id, thread_id, sender, role, content,
                kind.value, idempotency_key,
            )
            await _notify(conn, CHANNEL_MSG,
                          {"thread_id": thread_id, "message_id": message_id})
            await _notify(conn, CHANNEL_DISPATCH, {"thread_id": thread_id})
    return message_id


# ── dispatcher: claim / batch / complete ──────────────────────────────────────

async def try_claim_thread(conn: asyncpg.Connection, thread_id: str) -> bool:
    """Session-level advisory lock on the GIVEN held connection. One holder per
    thread fleet-wide. Auto-releases if the connection dies (crash safety)."""
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", thread_id,
    ))


async def release_thread(conn: asyncpg.Connection, thread_id: str) -> None:
    await conn.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)", thread_id)


# A SECOND lock, coarser than the thread and taken after it.
#
# The per-thread lock keeps two turns off one conversation. It cannot keep
# two turns off one WORKSPACE, and since Stage K an assistant's threads
# share a home: one `MEMORY.md`, written by a model that reads it and
# writes the whole file back. Two of those interleaved lose the first
# one's lines, silently, in the one turn whose job is to record what
# happened.
#
# Keyed on the GATEWAY AGENT ID, which is exactly the identity of a home.
# For an agent with a thread each that is uncontended by construction —
# every thread has its own gateway id — so this costs those nothing and
# needs no flag to say which kind it is.
#
# Acquisition order is always thread-then-home, so there is no cycle to
# deadlock on: the home is a single resource taken last.

async def try_claim_home(conn: asyncpg.Connection, gateway_agent_id: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1)::bigint)",
        f"memhome:{gateway_agent_id}",
    ))


async def release_home(conn: asyncpg.Connection, gateway_agent_id: str) -> None:
    await conn.execute(
        "SELECT pg_advisory_unlock(hashtext($1)::bigint)",
        f"memhome:{gateway_agent_id}",
    )


async def select_pending_batch(thread_id: str, limit: int) -> list[QueuedMessage]:
    """Return all queued system_inject context messages that precede the first
    user/system_wake turn trigger, plus the trigger itself.

    Using a plain ORDER BY ... LIMIT would cut off at `limit` rows and miss the
    trigger when more than `limit` system_inject messages are queued before it.
    Instead we find the trigger's position first, then fetch everything up to it.
    If there is no trigger (only system_inject rows), we return an empty list —
    the caller will defer until a real turn arrives.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """
        WITH first_trigger AS (
            SELECT id AS trig_id, created_at AS trig_at
            FROM messages
            WHERE thread_id = $1 AND status = 'queued'
              AND kind IN ('user', 'system_wake')
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        SELECT m.id, m.thread_id, m.sender, m.role, m.kind, m.content, m.status,
               m.idempotency_key, m.batch_id, m.created_at
        FROM messages m
        JOIN first_trigger ft ON true
        WHERE m.thread_id = $1 AND m.status = 'queued'
          AND (m.created_at < ft.trig_at
               OR (m.created_at = ft.trig_at AND m.id <= ft.trig_id))
        ORDER BY m.created_at ASC, m.id ASC
        """,
        thread_id,
    )
    return [_row_to_message(r) for r in rows]


async def mark_processing(
    *, message_ids: list[str], batch_id: str, run_id: str, claimed_by: str,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE messages
        SET status = 'processing', batch_id = $2, run_id = $3,
            claimed_by = $4, claimed_at = now()
        WHERE id = ANY($1::text[])
        """,
        message_ids, batch_id, run_id, claimed_by,
    )


async def mark_batch_done(*, message_ids: list[str]) -> None:
    """Mark a batch's inbound messages done. The agent REPLY is created and
    persisted by the session's on_message callback (co-located dispatcher drives
    the existing session), so we don't record it here. Idempotency on re-dispatch
    is handled at the gateway via the deterministic idempotencyKey."""
    pool = get_pool()
    await pool.execute(
        "UPDATE messages SET status = 'done' WHERE id = ANY($1::text[])",
        message_ids,
    )


async def mark_batch_failed(*, message_ids: list[str], error: str) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE messages
        SET status = 'failed', error = $2
        WHERE id = ANY($1::text[])
        """,
        message_ids, error[:2000],
    )


async def reap_stale(timeout_s: int) -> list[str]:
    """Requeue rows stuck in 'processing' past the timeout (their dispatcher
    likely died). Returns the distinct thread_ids to re-signal. Re-dispatch is
    made safe by the idempotency guard, not by this reaper."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        UPDATE messages
        SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
            batch_id = NULL, run_id = NULL
        WHERE status = 'processing'
          AND claimed_at < now() - make_interval(secs => $1)
        RETURNING thread_id
        """,
        timeout_s,
    )
    return list({r["thread_id"] for r in rows})


# ── gateway registry ──────────────────────────────────────────────────────────

async def upsert_gateway(
    *, gateway_id: str, ws_url: str, name: Optional[str] = None,
    internal_ip: Optional[str] = None,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO gateways (id, name, ws_url, internal_ip, status, last_heartbeat_at, updated_at)
        VALUES ($1, $2, $3, $4, 'active', now(), now())
        ON CONFLICT (id) DO UPDATE SET
            ws_url            = EXCLUDED.ws_url,
            name              = COALESCE(EXCLUDED.name, gateways.name),
            internal_ip       = COALESCE(EXCLUDED.internal_ip, gateways.internal_ip),
            last_heartbeat_at = now(),
            updated_at        = now()
        """,
        gateway_id, name, ws_url, internal_ip,
    )


async def heartbeat_gateway(gateway_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE gateways SET last_heartbeat_at = now(), updated_at = now() WHERE id = $1",
        gateway_id,
    )


async def list_active_gateways() -> list[Gateway]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, ws_url, status, weight, max_agents, current_agents, name, internal_ip
        FROM gateways WHERE status = 'active'
        ORDER BY (current_agents::float / NULLIF(max_agents, 0)) ASC, weight DESC
        """,
    )
    return [
        Gateway(
            id=r["id"], ws_url=r["ws_url"], status=GatewayStatus(r["status"]),
            weight=r["weight"], max_agents=r["max_agents"],
            current_agents=r["current_agents"], name=r["name"],
            internal_ip=r["internal_ip"],
        )
        for r in rows
    ]


async def gateway_id_for_agent(agent_id: str) -> Optional[str]:
    """Which gateway VM hosts this agent (NULL if unmapped → caller falls back
    to the single configured gateway)."""
    pool = get_pool()
    return await pool.fetchval(
        "SELECT gateway_id FROM agent_gateway_bindings WHERE agent_id = $1",
        agent_id,
    )


async def bind_agent_to_gateway(*, agent_id: str, gateway_id: str) -> None:
    """Record the agent→gateway placement and bump the gateway's agent count.
    Assumes an agent_gateway_bindings row already exists (created at provision);
    only sets the gateway_id if not already set."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_gateway_bindings
                SET gateway_id = $2
                WHERE agent_id = $1 AND gateway_id IS DISTINCT FROM $2
                RETURNING agent_id
                """,
                agent_id, gateway_id,
            )
            if updated:
                await conn.execute(
                    "UPDATE gateways SET current_agents = current_agents + 1 WHERE id = $1",
                    gateway_id,
                )


# ── dispatcher support ────────────────────────────────────────────────────────

async def list_pending_threads(limit: int = 500) -> list[str]:
    """Poll fallback: threads with queued work (catches missed NOTIFYs).

    Goal-active guard: only threads whose goal is still active (goal_guard's rule
    — status='active'; deleted/cancelled/completed and hard-removed goals are
    excluded). Without this, queued messages left on DELETED goals' threads get
    re-selected every tick, _ensure_session returns None (no live agent), and the
    dispatcher churns forever (deleted-goal zombie). The INNER JOINs also drop
    threads whose goal row is gone.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT m.thread_id
        FROM messages m
        JOIN threads t ON t.id = m.thread_id
        JOIN goals   g ON g.id = t.goal_id
        WHERE m.status = 'queued' AND g.status = 'active'
        LIMIT $1
        """,
        limit,
    )
    return [r["thread_id"] for r in rows]


async def set_thinking(thread_id: str, run_id: str) -> None:
    """Best-effort: mark the thread 'thinking' in chat_run_state so reloads show
    the indicator. Tolerant of schema differences (wrapped by caller)."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO chat_run_state (thread_id, run_id, status, current_tool, started_at, updated_at)
        VALUES ($1, $2, 'thinking', '', now(), now())
        ON CONFLICT (thread_id) DO UPDATE SET
            run_id = EXCLUDED.run_id, status = 'thinking', updated_at = now()
        """,
        thread_id, run_id,
    )


async def clear_thinking(thread_id: str) -> None:
    pool = get_pool()
    await pool.execute("DELETE FROM chat_run_state WHERE thread_id = $1", thread_id)
