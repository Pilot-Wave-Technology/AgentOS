"""G1/G3 — the wake router: what happens in the system reaches GO.

`ask_go` used to write `assistant_requests` and stop. Nothing told GO a
request existed; an assistant's ask was visible only if GO happened to
call `list_requests`, which it had no reason to do. A planner that must
be spoken to before it thinks is not watching anything.

This router is GO's afferent nerve. Sources NOTIFY (fast path); a poll
over undelivered rows is the backstop, so a missed notification delays a
wake rather than losing it — the same NOTIFY+poll shape the chat
dispatcher and the git engine already use.

Delivery is `enqueue_system_wake`: durable, serialized under the
per-thread advisory lock, its own turn. The wake text is SELF-DESCRIBING
— it says how to respond in the message itself — because until go-v2's
prompts land (G7), the live GO has never heard of `[REQUEST]`.

G1 ships the one channel that unblocks the assistant flow
(`assistant_ask`). G3 adds the engine's channels (trunk advance, node
state, PR opened, landings) behind the same loop.
"""

from __future__ import annotations

import asyncio
import json
import logging

# kernel.assistant_requests is named WITH its schema everywhere here.
# The control panel's pool must not carry the kernel schema on its
# search_path -- an unqualified name that resolves differently per pool
# is the checkpoints bug -- so a CP-side service that reads a kernel
# table says so explicitly.
from app.database import get_pool

_log = logging.getLogger(__name__)

POLL_SECONDS = 30


def _request_wake(r: dict) -> str:
    """The ask, as a message GO can act on without any new prompt."""
    story = f" · story {r['execution_id']}" if r["execution_id"] else ""

    # A REPORT ASKS FOR NOTHING, and must not arrive dressed as an ask.
    # The standard text says "this requires a response… an unanswered ask
    # leaves a person blocked", which is false of a completion summary
    # and would have GO closing reports with answer_request and treating
    # a finished story as an outstanding obligation.
    if r["kind"] == "report":
        return (
            f"[STORY COMPLETE] #{r['id']} from {r['actor_id']}{story}\n\n"
            f"{r['body']}\n\n"
            f"No reply is needed and nothing is waiting on you. This is "
            f"the assistant's account of a story that finished, so your "
            f"picture of the goal stays current without reading a diff. "
            f"It is also stored as that story's `result_summary` — read "
            f"it back with `code_state` or `goal_state` whenever you are "
            f"planning work that builds on it."
        )

    return (
        f"[REQUEST] #{r['id']} from {r['actor_id']}{story}\n"
        f"kind: {r['kind']}\n\n"
        f"{r['body']}\n\n"
        f"This requires a response. Act on it (for a resource ask: "
        f"`grant_resource(story=..., resource_id=..., relation=needs|"
        f"produces|modifies)`), then close it with "
        f"`answer_request(request_id={r['id']}, text=...)` — even when "
        f"the answer is no. An unanswered ask leaves a person blocked."
    )


async def route_pending_asks(pool) -> int:
    """Deliver every unrouted ask to its goal's GO thread. Returns count.

    Pure enough to gate: everything it needs is rows in, rows out.
    `routed_at` is stamped only AFTER the enqueue returns an id, so a
    failed delivery stays visible to the next poll instead of being
    marked done — the mailbox lesson, applied here.
    """
    from app.chat_module.system_ingress import enqueue_system_wake

    rows = await pool.fetch(
        """
        SELECT ar.id, ar.goal_id, ar.actor_id, ar.kind, ar.body,
               ar.execution_id, g.go_agent_id, g.go_thread_id
          FROM kernel.assistant_requests ar
          JOIN goals g ON lower(g.id) = lower(ar.goal_id)
         WHERE ar.routed_at IS NULL AND ar.status = 'PENDING'
           AND g.status = 'active'
         ORDER BY ar.created_at
         LIMIT 20
        """)
    routed = 0
    for r in rows:
        if not (r["go_agent_id"] and r["go_thread_id"]):
            # A goal mid-provisioning. Leave the row; the backstop
            # retries once the GO exists — this is a delay, not a drop.
            continue
        msg_id = await enqueue_system_wake(
            thread_id=r["go_thread_id"], goal_id=r["goal_id"],
            agent_id=r["go_agent_id"], gateway_agent_id=r["go_agent_id"],
            agent_type="GO", content=_request_wake(dict(r)))
        if msg_id is None:
            # Queue disabled: do NOT stamp routed_at. Marking delivered
            # what was not delivered is how a request rots invisibly.
            _log.warning("go_wakes: chat queue disabled; ask %s undelivered",
                         r["id"])
            continue
        await pool.execute(
            "UPDATE kernel.assistant_requests SET routed_at = now() "
            "WHERE id = $1",
            r["id"])
        routed += 1
        _log.info("go_wakes: ask %s -> goal %s thread %s",
                  r["id"], r["goal_id"], r["go_thread_id"])
    return routed


CHANNELS = ("assistant_ask", "go_story_done", "go_node_event",
            "story_review_ready",
            "go_trunk_advance", "resource_refresh",
            "resource_published",
            # I5g — an ERRAND parked. "GO watches over the errand" was a
            # poll GO had no reason to run; a card goes to whoever is
            # driving and nothing went to the planner. Errands only: a
            # story's park has an assistant on it already.
            "go_execution_parked")


async def _dispatch(pool, channel: str, payload: str) -> None:
    from app.services import go_wake_events as ev

    data = json.loads(payload) if payload else {}
    if channel == "assistant_ask":
        await route_pending_asks(pool)
    elif channel == "story_review_ready":
        # A reviewer running in an execution pod recorded its verdict and
        # announced it. The row is already written and the story already
        # moved -- this is only the delivery, which is why it reads the
        # text back from the row rather than carrying it in the payload
        # (NOTIFY caps at 8000 bytes and would truncate a long review).
        from app.services.story_review_delivery import deliver_recorded

        await deliver_recorded(data["goal"], data["story"])
    elif channel == "go_story_done":
        await ev.on_story_done(pool, data["goal"], data["story"],
                               int(data["execution"]))
    elif channel == "go_node_event":
        await ev.on_node_event(pool, data["goal"], data["node"],
                               data["state"])
    elif channel == "resource_published":
        from app.services.resource_publish import finalize_publish
        await finalize_publish(pool, int(data["publish"]))
    elif channel == "resource_refresh":
        from app.services.resource_brief import refresh_identity
        await refresh_identity(data["resource"], data["goal"])
    elif channel == "go_execution_parked":
        await ev.on_execution_parked(pool, data["goal"],
                                     int(data["execution"]),
                                     int(data["question"]), data["kind"])
    elif channel == "go_trunk_advance":
        await ev.on_trunk_advance(pool, data["repo"], data["trunk"],
                                  data["sha"])


async def _backstop(pool) -> None:
    """What NOTIFY missed, the poll delivers late rather than never.

    Each sweep finds recent events with no go_wake_log row and replays
    them through the same handlers — the log makes replay idempotent.
    Trunk advances are not swept: a missed one is re-signalled by the
    next advance, and every stale branch is visible in code_state anyway.
    """
    from app.services import go_wake_events as ev

    await route_pending_asks(pool)
    # THE ONE WAKE THAT HAD NO POLL BEHIND IT, which is why it was the
    # only one that could be lost for good.
    #
    # Every other channel here replays from a table, so a missed NOTIFY
    # costs thirty seconds. `story_review_ready` had nothing, so a
    # missed one cost the review. Measured on feature: 67 of 76 reviews
    # undelivered, and the symptom was invisible -- the node and
    # story-done wakes for the SAME story arrived seconds earlier
    # through this very sweep, so everything looked healthy while the
    # GO was never told a single story had been reviewed.
    try:
        from app.services.story_review_delivery import deliver_undelivered
        await deliver_undelivered(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: review sweep failed")
    try:
        from app.services.resource_publish import sweep_pending
        await sweep_pending(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: publish sweep failed")
    # A coding story holds its pod between push and merge. The story
    # row follows the execution here for the same reason `on_story
    # _done` does below -- the execution is what happened, the story
    # row is a projection of it -- and sweeping rather than reacting
    # makes both directions self-healing after a missed signal.
    try:
        from app.services import story_landing
        await story_landing.sweep_entering(pool)
        await story_landing.sweep_landed(pool)
        await story_landing.sweep_resumed(pool)
        await story_landing.sweep_pr_closed(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: landing sweep failed")
    # A story whose branch is behind a trunk that moved catches itself
    # up, in-story, under a pre-approved plan scoped to sync + check +
    # report. Swept rather than signalled: `go_trunk_advance` is a
    # listener with no publisher, so anything built on it would never
    # fire.
    try:
        from app.services import trunk_integration
        await trunk_integration.sweep(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: trunk integration sweep failed")
    # AND THE SAME QUESTION FOR A LOCAL STORY, which the sweep above
    # cannot see: its SQL joins `kernel.executions` and parks the run on
    # a question, and a local story has no execution, no run and no
    # park. Beside its twin rather than in a loop of its own, so the two
    # halves of one decision run at one cadence and nobody has to find
    # out that the local one was registered somewhere that is not
    # started (the shape of `SCHEDULER_LOOPS=0`).
    try:
        from app.services import local_integration
        await local_integration.sweep(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: local catch-up sweep failed")
    # AND A LOCAL STORY WHOSE PULL REQUEST SOMEBODY CLOSED. Detection
    # escalates the node for either lane; only the cloud was ever told,
    # because its sweep joins `kernel.executions`. Beside its twin for
    # the same reason the catch-up is.
    try:
        from app.services import local_pr_closed
        await local_pr_closed.sweep(pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("go_wakes: local pr-closed sweep failed")
    nodes = await pool.fetch(
        """
        SELECT gn.goal_id, gn.id, gn.state FROM git_nodes gn
         WHERE gn.updated_at > now() - interval '24 hours'
           AND gn.state IN ('MERGED','CONFLICTED','ESCALATED',
                            'PROTECTION_BLOCKED')
           AND NOT EXISTS (SELECT 1 FROM go_wake_log w
                            WHERE w.goal_id = gn.goal_id AND w.kind='node'
                              AND w.ref = gn.id || ':' || gn.state)
         LIMIT 10
        """)
    for n in nodes:
        await ev.on_node_event(pool, n["goal_id"], n["id"], n["state"])
    done = await pool.fetch(
        """
        SELECT e.goal_id, e.story_id, e.id FROM kernel.executions e
         WHERE e.story_id IS NOT NULL
           -- A run in its review phase has finished its WORK: the story
           -- is accepted and owed its `done`, even though the execution
           -- stays RUNNING while the reviewer reads. Keyed on the phase
           -- rather than on DONE for the same reason the trigger is.
           AND (e.state = 'DONE'
                OR (e.state = 'RUNNING' AND e.phase = 'review'))
           AND e.updated_at > now() - interval '24 hours'
           AND NOT EXISTS (SELECT 1 FROM go_wake_log w
                            WHERE w.goal_id = e.goal_id
                              AND w.kind = 'story_done'
                              AND w.ref = 'exec:' || e.id)
         LIMIT 10
        """)
    for d in done:
        await ev.on_story_done(pool, d["goal_id"], d["story_id"], d["id"])
    # I5g — parked errands the NOTIFY missed. OPEN only: a question
    # answered in the meantime is not news, and a wake about a settled
    # one teaches GO to stop reading these.
    parked = await pool.fetch(
        """
        SELECT e.goal_id, e.id AS execution_id, q.id AS question_id,
               q.kind
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE q.status = 'OPEN'
           AND e.kind = 'errand'
           AND q.created_at > now() - interval '24 hours'
           AND NOT EXISTS (SELECT 1 FROM go_wake_log w
                            WHERE w.goal_id = e.goal_id
                              AND w.kind = 'errand_parked'
                              AND w.ref = 'q:' || q.id)
         LIMIT 10
        """)
    for p in parked:
        await ev.on_execution_parked(pool, p["goal_id"], p["execution_id"],
                                     p["question_id"], p["kind"])


async def run_forever() -> None:
    """LISTEN on every wake channel + poll backstop. Started at startup."""
    pool = get_pool()
    conn = await pool.acquire()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_notify(_c, _pid, channel, payload) -> None:
        queue.put_nowait((channel, payload))

    for ch in CHANNELS:
        await conn.add_listener(ch, _on_notify)
    _log.info("go_wakes: listening on %s (backstop %ss)",
              ", ".join(CHANNELS), POLL_SECONDS)
    try:
        while True:
            try:
                channel, payload = await asyncio.wait_for(
                    queue.get(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                channel, payload = None, None
            try:
                if channel is None:
                    await _backstop(pool)
                    # THE ERRAND WATCH BELONGS HERE, not with the card
                    # posters. It was wired into `kernel_park_cards`'
                    # loop on the grounds that a separate poller is a
                    # second thing to remember to start -- true, and it
                    # put it on the SCHEDULER service, which has no
                    # `CHAT_QUEUE_ENABLED`. `enqueue_system_wake` there
                    # returns None, `_deliver` releases its claim, and
                    # the wake vanished without an error or a row.
                    #
                    # A card poster writes a row to `messages`; a wake
                    # producer enqueues a TURN. Those need different
                    # things from the process they run in, and this is
                    # where the other wake producers already live.
                    try:
                        from app.services.go_errand_watch import (
                            sweep as _errand_watch)
                        await _errand_watch(pool)
                    except Exception:                       # noqa: BLE001
                        _log.exception("go_wakes: errand watch failed")
                    # AND THE EXECUTOR'S ANSWERS, here for the same
                    # reason and after the same mistake: the first cut
                    # hung this off the answer CARD poster, which runs
                    # in the scheduler's loop where there is no chat
                    # queue, and every wake was dropped with a warning
                    # and no row. Wake producers live beside the other
                    # wake producers.
                    try:
                        from app.services.errand_answer_wake import (
                            sweep as _answer_wake)
                        await _answer_wake(pool)
                    except Exception:                       # noqa: BLE001
                        _log.exception("go_wakes: answer wake failed")
                else:
                    await _dispatch(pool, channel, payload)
            except Exception:                              # noqa: BLE001
                # One bad event must not kill the nerve; the backstop
                # replays anything durable that was missed.
                _log.exception("go_wakes: %s failed", channel or "backstop")
    finally:
        try:
            for ch in CHANNELS:
                await conn.remove_listener(ch, _on_notify)
        finally:
            await pool.release(conn)
