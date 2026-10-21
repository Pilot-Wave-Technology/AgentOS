"""Who the kernel is, on the ledger, and which human it acted for.

TWO IDENTITIES PER EVENT, and the distinction is the whole design:

    actor        WHAT ran           executor / verifier, of a given kind
    on_behalf_of WHO it ran for     a person

An executor event naming only the executor is attributable to a robot and to
nobody, which is no better than `system`. Naming the person as well is what
lets every kernel act be traced back to a human -- and it is what the
taximeter needs, since that attributes work and cost to a `uid`.

KIND IS THE ACTOR, NOT THE TYPE. Executors come in kinds -- story/coding,
errand/ops -- and one actor_type per kind would mean a schema change every
time a new one ships. So the type says WHAT it is and the actor row says
WHICH, keyed `executor:story/coding`. The id is a uuid5 of that name, so the
same kind always resolves to the same actor with no lookup table for anybody
to keep in step, and a kind nobody has seen before registers itself the
first time it acts rather than failing the append.

THE PRINCIPAL CHAIN, and every hop is an equi-join on an indexed column:

    execution -> story -> thread -> thread.created_by -> users -> actors

`threads.created_by` rather than `stories.assigned_to`: the thread's creator
is who STARTED this work, which is the question an audit trail asks.
Assignment can change afterwards and would rewrite history.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# A fixed namespace so `executor:story/coding` maps to the same uuid in every
# environment and every process, for ever. Derived from the DNS namespace so
# it needs no registry of its own.
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "agentos.ledger.actors")

EXECUTOR = "executor"
VERIFIER = "verifier"


def actor_name(role: str, kind: str | None, domain: str | None) -> str:
    """`executor:story/coding` — the stable name a uuid5 is taken over."""
    return f"{role}:{kind or 'story'}/{domain or 'general'}"


def actor_uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(_NS, name)


async def kernel_actor(conn, role: str, kind: str | None,
                       domain: str | None) -> tuple[uuid.UUID, str]:
    """(actor_id, actor_type) for an executor or verifier of this kind.

    Registers the actor row on first sight. `ON CONFLICT DO NOTHING` rather
    than a read-then-write: two workers starting the same new kind at the
    same moment is ordinary, and losing that race must not lose the event.
    """
    name = actor_name(role, kind, domain)
    aid = actor_uuid(name)
    try:
        # INSIDE A SAVEPOINT, and this is not defensive dressing. `try`
        # around a failing statement does NOT make it harmless: Postgres
        # marks the whole transaction aborted and every later command in it
        # fails with "current transaction is aborted". Caught here, that
        # would take down the verdict or the leg this event describes --
        # exactly the "an audit trail must not break what it audits" rule,
        # broken by the code meant to keep it. A nested asyncpg transaction
        # is a SAVEPOINT, so only this statement rolls back.
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO actors (id, type, agent_id, display_name)
                   VALUES ($1, 'agent', NULL, $2)
                   ON CONFLICT (id) DO NOTHING""",
                aid, _label(role, kind, domain))
    except Exception as exc:                                # noqa: BLE001
        # An unregistered actor makes the audit WARN about attribution; a
        # raise here would lose the event, and a recorded act with an
        # unresolved actor beats no record at all.
        logger.warning("could not register ledger actor %s: %s", name, exc)
    return aid, role


def _label(role: str, kind: str | None, domain: str | None) -> str:
    """The human-readable name, which is also where the KIND is recorded.

    `actors.agent_id` has a foreign key to `agents`, and a kernel executor is
    not a gateway agent -- there is no row there to point at. So it stays
    NULL, exactly as the `system` actors do, and the kind is carried by the
    deterministic id, this label, and `payload.executor_kind` on every event.
    """
    what = "Executor" if role == EXECUTOR else "Verifier"
    return f"{what} ({kind or 'story'}/{domain or 'general'})"


async def principal_for(conn, execution_id: int) -> dict:
    """Goal, story, thread and the HUMAN an execution is working for.

    One query, because every field is needed together on every event and
    four round-trips per event would put the audit trail on the hot path of
    the leg it is describing.

    Every field is nullable and the caller must cope. A run whose thread has
    been deleted still did something, and the event says so with a thinner
    context rather than not being written -- an act that happened is worth
    recording even when we can no longer say who asked for it.
    """
    row = await conn.fetchrow(
        """SELECT e.goal_id, e.story_id, e.kind, e.domain,
                  t.id           AS thread_id,
                  t.created_by   AS human_uid,
                  t.instance_id  AS assistant_instance,
                  ha.id          AS human_actor_id
             FROM executions e
             LEFT JOIN LATERAL (
                   -- I5b: the DRIVING thread wins when a run has one.
                   -- Without it this takes the story's newest thread,
                   -- so with two people in a goal the principal follows
                   -- whoever imported last rather than whoever is
                   -- responsible. An autonomous errand's principal is
                   -- the human who approved the grant, and that has to
                   -- be stated rather than inferred.
                   SELECT th.id, th.created_by, th.instance_id
                     FROM threads th
                    WHERE th.id = e.driving_thread_id
                       OR (e.driving_thread_id IS NULL
                           AND th.story_id = e.story_id)
                 ORDER BY (th.id = e.driving_thread_id) DESC,
                          th.created_at DESC
                    LIMIT 1
             ) t ON true
             LEFT JOIN actors ha ON ha.uid = t.created_by AND ha.type = 'user'
            WHERE e.id = $1""",
        execution_id)
    if row is None:
        return {}
    return {
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "kind": row["kind"],
        "domain": row["domain"],
        "thread_id": str(row["thread_id"]) if row["thread_id"] else None,
        "human_uid": row["human_uid"],
        "human_actor_id": row["human_actor_id"],
        "assistant_instance": row["assistant_instance"],
    }


async def uid_of(conn, actor_id):
    """The canonical uid for a registered actor, or None.

    ONE FORM, EVERYWHERE. Identity reaches the kernel as a uid
    (`threads.created_by`), an email (`questions.answered_by`) and a
    prefixed provenance string (`proposals.actor_id`), and an event that
    copies its source verbatim spreads that mess into the ledger — where it
    cannot be cleaned up afterwards, because the ledger is append-only.
    Resolving to the uid at write time is the only chance to make every row
    say it the same way.
    """
    if not actor_id:
        return None
    return await conn.fetchval("SELECT uid FROM actors WHERE id = $1", actor_id)


async def human_actor(conn, who: str | None):
    """The actor row for a person, by uid OR email.

    Both shapes are in use and neither is going away: `threads.created_by`
    holds a uid (`local-dev-admin`) while `questions.answered_by` holds an
    email. Accepting one and silently returning None for the other is how
    plan approvals — the events that matter most here — would have ended up
    attributed to nobody.
    """
    if not who:
        return None
    return await conn.fetchval(
        """SELECT a.id FROM actors a
            WHERE a.type = 'user' AND (a.uid = $1
               OR a.uid = (SELECT u.uid FROM users u WHERE u.email = $1))
            LIMIT 1""", who)


# The pre-seeded platform actor. Used only where the PLATFORM genuinely
# decided — an auto-approval — never as a shrug for "could not resolve".
SYSTEM_ACTOR = "00000000-0000-0000-0000-000000000001"


async def from_provenance(conn, stamp: str | None, ctx: dict,
                          *, machine: str = EXECUTOR):
    """(actor_id, actor_type, resolved) from a kernel provenance string.

    Both `proposals.actor_id` and `questions.answered_by` speak the same
    small vocabulary — `human:<uid>`, `assistant:<instance>`, `verifier`,
    `kernel`, `worker-<execution>` — stamped from the ROW rather than from a
    payload the executor controls. So both are read here, and neither is
    guessed at from surrounding context.

    THE THIRD RETURN VALUE MATTERS. A stamp that resolves to nothing must
    not be quietly attributed to the machine: recording that an executor
    approved its own plan is worse than recording that nobody can be named,
    because it is a specific false claim rather than an admission. `resolved`
    is False there, the caller flags the event, and the raw stamp is kept
    verbatim in the payload so the string survives for whoever investigates.
    """
    s = (stamp or "").strip()
    if s.startswith("human:") or "@" in s:
        got = await human_actor(conn, s.split(":", 1)[1] if ":" in s else s)
        if got:
            return got, "human", True
    elif s.startswith("assistant:"):
        got = await conn.fetchval(
            """SELECT a.id FROM actors a
                WHERE a.type = 'agent'
                  AND (lower(a.agent_id) = lower($1)
                    OR lower(a.display_name) = lower($1))
                LIMIT 1""", s.split(":", 1)[1])
        # The assistant is a real principal even when its actor row has not
        # been registered; `shca` with the platform id still says WHAT
        # decided, which is the part an audit needs.
        return (got or SYSTEM_ACTOR), "shca", bool(got)
    elif s.startswith("verifier"):
        aid, atype = await kernel_actor(conn, VERIFIER, ctx.get("kind"),
                                        ctx.get("domain"))
        return aid, atype, True
    elif s in ("kernel", "system", "worker") or s.startswith("worker-"):
        # The platform itself acted — an auto-approval, a machine step. That
        # is a true `system` attribution rather than a fallback one.
        if s.startswith("worker-"):
            aid, atype = await kernel_actor(conn, machine, ctx.get("kind"),
                                            ctx.get("domain"))
            return aid, atype, True
        return SYSTEM_ACTOR, "system", True

    got = await human_actor(conn, s)
    if got:
        return got, "human", True
    aid, atype = await kernel_actor(conn, machine, ctx.get("kind"),
                                    ctx.get("domain"))
    return aid, atype, False
