"""Unread message counter helpers for GO and Assistant channels.

Counter-cache pattern: instead of counting message rows on every sidebar render,
we maintain an atomic unread_count per (thread_id, uid) in thread_participants and
notify affected users over Postgres LISTEN/NOTIFY so the browser badge updates in
real time via the user-scoped WebSocket at /stream/user-events.

Two public functions:
  increment_unread  — called after every message INSERT
  mark_thread_read  — called when a user opens a channel

Both fire a pg_notify so the badge clears/updates instantly for users who are
currently online. Users who are offline see the correct count on next page load
(the sidebar reads thread_participants directly).
"""

import json
import logging

from asyncpg import Pool

_log = logging.getLogger(__name__)

# Postgres channel name for LISTEN/NOTIFY.
# unread_listener.py subscribes to this; any change of name must be updated there.
_PG_CHANNEL = "unread_updates"


async def increment_unread(
    pool: Pool,
    thread_id: str,
    sender_uid: str = "",
) -> None:
    """Increment unread_count for thread participants.

    sender_uid controls the exclusion rule:
      non-empty — increment everyone EXCEPT this uid. Use for web-UI sends
                  where the sender is actively in the UI and their own message
                  shouldn't count as unread for themselves.
      empty     — increment ALL participants. Use for platform-inbound sends
                  (Slack, Notion, …) where the sender was outside the web UI
                  and the badge should reflect all new activity since last visit.

    Works for both channel types:
      GO channel   — sender_uid is human uid (web UI) or empty (Slack/platform)
                     or the GO agent id (agent id never matches a human uid in
                     thread_participants so effectively increments everyone)
      SHCA channel — sender_uid is the agent id (increments the thread owner)

    Fires one pg_notify per affected participant so the unread_listener can
    push the updated count over their user-scoped WebSocket.

    Non-fatal: if the UPDATE touches zero rows (no participants seeded yet),
    the function logs a warning but does not raise — the badge will just not
    appear until the next sidebar load.
    """
    _log.debug(
        "unread: increment thread=%s excluding=%s",
        thread_id[:12], (sender_uid[:12] if sender_uid else "(none — all participants)"),
    )

    # When sender_uid is empty the OR short-circuits and all rows match,
    # so every participant's counter is incremented.
    updated = await pool.execute(
        """
        UPDATE thread_participants
           SET unread_count = unread_count + 1,
               updated_at   = now()
         WHERE thread_id = $1
           AND ($2 = '' OR uid != $2)
        """,
        thread_id, sender_uid,
    )

    # "UPDATE N" — extract the row count from the status string.
    affected = int(updated.split()[-1]) if updated else 0
    if affected == 0:
        _log.warning(
            "unread: no participants found for thread=%s (sender=%s) — "
            "badge will not update until sidebar reloads",
            thread_id[:12], sender_uid[:12] if sender_uid else "(none)",
        )
        return

    _log.debug("unread: incremented %d participant(s) in thread=%s", affected, thread_id[:12])

    # Notify each affected participant so their user-scoped WS updates the badge.
    rows = await pool.fetch(
        """
        SELECT uid, unread_count
          FROM thread_participants
         WHERE thread_id = $1
           AND ($2 = '' OR uid != $2)
        """,
        thread_id, sender_uid,
    )

    for row in rows:
        payload = json.dumps({
            "uid":       row["uid"],
            "thread_id": thread_id,
            "count":     row["unread_count"],
        })
        await pool.execute(
            f"SELECT pg_notify('{_PG_CHANNEL}', $1)",
            payload,
        )
        _log.debug(
            "unread: notified uid=%s thread=%s count=%d",
            row["uid"][:12], thread_id[:12], row["unread_count"],
        )


async def mark_thread_read(
    pool: Pool,
    thread_id: str,
    uid: str,
    last_message_id: str | None = None,
) -> None:
    """Zero the unread counter for this user in this thread.

    Called when the user opens the channel page (GET handler) or connects
    over WebSocket (so the badge clears even if they navigated directly to
    the thread URL). Uses INSERT … ON CONFLICT so it also seeds the row if
    the user was never provisioned (e.g. joined after the backfill migration).

    Fires a pg_notify with count=0 so the badge clears immediately in the
    sidebar of any other browser tab the user has open.
    """
    _log.info(
        "unread: mark read uid=%s thread=%s last_msg=%s",
        uid[:12], thread_id[:12], (last_message_id or "none")[:12],
    )

    await pool.execute(
        """
        INSERT INTO thread_participants
               (thread_id, uid, unread_count, last_read_message_id, updated_at)
        VALUES ($1, $2, 0, $3, now())
        ON CONFLICT (thread_id, uid) DO UPDATE
            SET unread_count         = 0,
                last_read_message_id = EXCLUDED.last_read_message_id,
                updated_at           = now()
        """,
        thread_id, uid, last_message_id,
    )

    # Notify so any other open tab zeroes its badge without waiting for a poll.
    payload = json.dumps({"uid": uid, "thread_id": thread_id, "count": 0})
    await pool.execute(
        f"SELECT pg_notify('{_PG_CHANNEL}', $1)",
        payload,
    )

    _log.debug("unread: mark-read notified uid=%s thread=%s", uid[:12], thread_id[:12])
