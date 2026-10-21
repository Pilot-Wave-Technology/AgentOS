"""Where a story review goes once it exists. Two surfaces, unchanged.

EXTRACTED FROM `council/dispatcher._deliver_story`, WHICH NOW CALLS IT.
The review has a second producer -- a reviewer running in the execution
pod -- and the requirement was that the review reach the GO exactly as
it does today. The way to keep that true is one function, not two that
agree at the time of writing.

THE TWO SURFACES DO DIFFERENT JOBS, and the comments in the original say
why the split exists:

  1. A card in the goal's ORCHESTRATOR channel. The durable human-facing
     copy, kept OUT of the GO's main goal thread so that thread stays
     readable.
  2. A wake to the GO carrying the FULL review, prefixed `[REVIEW:story]`
     -- the marker that triggers the GO's review-action skill. The wake
     is not rendered in the goal-thread UI, so the human copy above is
     the one a person reads.

NEITHER CAN BLOCK THE TRANSITION. The story reached `reviewed` before
this runs, in the same transaction as the review row. A card that fails
to render is a card; it is not a story stuck at `done`.
"""

from __future__ import annotations

import logging

from app.database import get_pool

log = logging.getLogger(__name__)

# The attribution the orchestrator card renders. It matches no users.uid
# and no agents.id, so the loader falls back to the literal.
#
# STILL THE COUNCIL'S NAME, even though the reviewer now runs in the
# pod. A person reading the board knows this label; changing it would
# make every review before the cutover look like it came from something
# else. What produced it is an implementation detail and is recorded on
# the row (`reviewer_agent_id`), where a person who cares can find it.
SENDER = "Goal Review Council"


async def _release(pool, story_id: str, reviewed_state: str) -> None:
    """Give the claim back, because nothing was delivered under it.

    The claim's whole purpose is "has this ever reached anybody". A
    claim held by a caller that then sent nothing answers that question
    wrongly and permanently, since the backstop only looks for NULL.
    """
    try:
        await pool.execute(
            "UPDATE story_reviews SET delivered_at = NULL "
            " WHERE story_id = $1 AND reviewed_state = $2",
            story_id, reviewed_state)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("story review: could not release the claim on %s: %s",
                    story_id, exc)


async def deliver(goal_id: str, story_id: str, text: str, *,
                  reviewed_state: str = "done") -> bool:
    """Show the review to a person and hand it to the GO.

    Returns whether the orchestrator card landed. Never raises: every
    caller has already committed the review.

    CLAIMED HERE, WHERE EVERY PRODUCER PASSES. The claim went in one
    caller first -- `deliver_recorded`, the NOTIFY path -- which left the
    council's direct call unguarded, so "delivered once" depended on
    which door the review came through. 7c61ade5's review reached the GO
    three times with one claim stamped: whatever sent the other two did
    not pass through that door, and reasoning about which door is exactly
    the thing one function is supposed to make unnecessary.

    PER (STORY, REVIEWED_STATE), because an interim review and the final
    one are different deliveries of different text, and a single flag
    would silence the second.
    """
    pool = get_pool()
    claimed = await pool.fetchval(
        "UPDATE story_reviews SET delivered_at = now() "
        " WHERE story_id = $1 AND reviewed_state = $2 "
        "   AND delivered_at IS NULL "
        " RETURNING 1", story_id, reviewed_state)
    if not claimed:
        log.info("story review: %s (%s) is already delivered — not sending "
                 "it again", story_id, reviewed_state)
        return False

    goal = await pool.fetchrow(
        "SELECT go_agent_id, go_thread_id FROM goals WHERE id = $1", goal_id)
    if not goal or not goal["go_agent_id"] or not goal["go_thread_id"]:
        # CLAIMED AND NOT DELIVERED IS THE WORST OF BOTH. The claim is
        # taken above, before anything is sent, so returning here would
        # leave the row stamped `delivered_at` with nobody ever told --
        # and the backstop sweep looks for NULL, so it would never pick
        # it up either. The review would be lost permanently, and the
        # row would say it had been delivered.
        #
        # A goal whose GO is not provisioned yet is exactly the case
        # that recovers on its own: release the claim and let the next
        # pass try again.
        await _release(pool, story_id, reviewed_state)
        log.warning("story review: goal %s has no GO agent/thread; "
                    "the review is recorded and will be retried", goal_id)
        return False

    kind = "final" if reviewed_state == "done" else f"interim ({reviewed_state})"
    title = f"Story Review — {story_id} ({kind})"

    thread_ok = True
    try:
        from app.services.orchestrator_delivery import (
            persist_review_to_orchestrator)
        thread_ok = await persist_review_to_orchestrator(
            goal_id, "story", title, text, SENDER)
    except Exception as exc:                                # noqa: BLE001
        thread_ok = False
        log.warning("story review: orchestrator card failed for %s/%s: %s",
                    goal_id, story_id, exc)

    # THE MARKER IS THE CONTRACT. `[REVIEW:story]` is what triggers the
    # GO's review-action skill; GO's own AGENTS.md documents the marker
    # table. Changing the prefix would silently stop the GO acting on
    # reviews while every card still rendered.
    woke = True
    try:
        from app.services.go_events import _send_system_message_to_go
        await _send_system_message_to_go(
            goal_id, f"[REVIEW:story] {title}\n\n{text}",
            wake_up=True, mirror_to_mesh=False)
    except Exception as exc:                                # noqa: BLE001
        woke = False
        log.warning("story review: GO wake failed for %s: %s", goal_id, exc)

    # NEITHER SURFACE TOOK IT, so the claim is a lie. One of the two
    # landing is a delivery -- a card with no wake is still readable,
    # and a wake with no card still reaches the GO -- but zero is not,
    # and leaving the stamp on would hide it from the sweep for ever.
    if not thread_ok and not woke:
        await _release(pool, story_id, reviewed_state)
        log.warning("story review: nothing landed for %s/%s; the claim is "
                    "released and the sweep will retry", goal_id, story_id)
        return False
    return thread_ok


async def deliver_recorded(goal_id: str, story_id: str) -> bool:
    """Deliver the review already stored for this story.

    The in-pod reviewer writes the row and announces it; the row is the
    source of truth, so this reads it back rather than carrying the text
    through a NOTIFY payload — which is bounded at 8000 bytes and would
    have silently truncated a long review.
    """
    # THE CLAIM IS IN `deliver`, NOT HERE. NOTIFY is a BROADCAST --
    # `story_review_ready` reaches every listening connection, so each
    # control-plane instance runs this -- but the council calls `deliver`
    # directly and never comes through here at all. Guarding one door
    # made "delivered once" depend on which door was used, and 7c61ade5
    # went out three times with a single claim stamped.
    text = await get_pool().fetchval(
        "SELECT review_text FROM story_reviews "
        " WHERE story_id = $1 AND reviewed_state = 'done'", story_id)
    if not text:
        log.warning("story review: nothing recorded for %s to deliver",
                    story_id)
        return False
    return await deliver(goal_id, story_id, text)


# ── the backstop ─────────────────────────────────────────────────────

# How long a review may sit undelivered before the sweep takes it. Long
# enough that the NOTIFY path, which is almost always the one that
# works, is not raced on every pass.
_GRACE_SECONDS = 120

# How far back the sweep will reach. A review older than this was not
# "missed minutes ago" -- it belongs to work that is over, and handing
# it to the GO now makes it act on a story nobody is thinking about.
# It stays in the table, undelivered and visible, which is the honest
# record of what happened.
_MAX_AGE_HOURS = 24


async def deliver_undelivered(pool=None, *, limit: int = 50) -> int:
    """Deliver every review the NOTIFY path missed. Returns how many.

    THE SWEEP `review_finish` ALREADY PROMISED. Its comment on the
    announce says "the panel's own sweep finds a review nobody
    delivered" -- and there was no such sweep. `delivered_at` appeared
    in exactly one file, the claim in `deliver`, and nothing ever looked
    for a row where it was still NULL.

    SO A MISSED NOTIFY WAS PERMANENT. LISTEN/NOTIFY has no redelivery
    and no acknowledgement: if the listening connection is down for the
    instant the notify fires -- a dropped connection, an instance going
    away, a reconnect in progress -- the message is gone, and nothing
    anywhere records that it was. Observed on feature with story
    3a1095ea: the review was written at 15:45:32 with verdict `approve`,
    the story moved to `reviewed`, and the review never reached the
    Goal Orchestrator. `delivered_at` was NULL, no error was logged,
    and the only way to find out was to go looking at the row.

    A story reaching `reviewed` without its review reaching the GO is
    the failure that matters: the GO plans the next story from reviews
    it never saw.

    `deliver` holds the claim, so this is safe to run beside the NOTIFY
    path and beside another instance running the same sweep.
    """
    from app.database import get_pool as _gp
    pool = pool or _gp()
    try:
        rows = await pool.fetch(
            f"""
            SELECT sr.story_id, sr.goal_id
              FROM story_reviews sr
              JOIN goals g ON g.id = sr.goal_id
             WHERE sr.delivered_at IS NULL
               AND sr.reviewed_at < now() - interval '{int(_GRACE_SECONDS)} s'
               -- A DELETED GOAL GETS NOTHING. Goal delete is soft and
               -- does not cascade, so every review of every story in
               -- every dead goal is still sitting here undelivered --
               -- and waking a deleted goal's GO is the zombie this
               -- codebase already carries eight guards against.
               AND g.status = 'active'
               -- AND NOT A REVIEW FROM MONTHS AGO. This sweep is a
               -- backstop for an announce that was missed minutes ago,
               -- not a migration of everything that was ever missed.
               -- Without this it back-delivered 37 reviews reaching to
               -- June the first time it ran, which is not a recovery,
               -- it is a flood -- and the GO acts on every one.
               AND sr.reviewed_at > now() - interval '{int(_MAX_AGE_HOURS)} h'
             ORDER BY sr.reviewed_at
             LIMIT {int(limit)}
            """)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("story review sweep: could not list: %s", exc)
        return 0

    sent = 0
    for r in rows:
        try:
            if await deliver_recorded(r["goal_id"], r["story_id"]):
                sent += 1
                log.info("story review sweep: delivered %s late — the "
                         "announce was missed", r["story_id"])
        except Exception as exc:                               # noqa: BLE001
            # One undeliverable review must not strand the rest.
            log.warning("story review sweep: %s failed: %s",
                        r["story_id"], exc)
    return sent


INTERVAL_SECONDS = 300


async def run_loop() -> None:
    import asyncio

    log.info("story review delivery backstop started (every %ds)",
             INTERVAL_SECONDS)
    while True:
        try:
            await deliver_undelivered()
        except Exception:                                      # noqa: BLE001
            log.exception("story review sweep: pass failed")
        await asyncio.sleep(INTERVAL_SECONDS)
