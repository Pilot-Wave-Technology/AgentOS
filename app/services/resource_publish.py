"""G6, the control panel's half: a pending publish becomes a version.

The executor inserted bytes and NOTIFYed; this finalizes. Versioned
object name (`v<publish_id>-<filename>`), so the OLD object is retained
— nothing is overwritten, ever. The resource repoints, the summary goes
stale, an event records the previous uri, GO is woken, and the document
re-scans. Failure marks the row 'failed' with the reason; the bytes stay
in the row, so a failed finalize loses nothing.

`uploader` is injectable: the gate exercises everything around GCS with
a fake, and production defaults to the real bucket. Same seam pattern as
the verifier's drivers — the network is the only thing faked.
"""

from __future__ import annotations

import json
import logging

from app.database import get_pool

_log = logging.getLogger(__name__)


async def finalize_publish(pool, publish_id: int, uploader=None) -> bool:
    if uploader is None:
        from app.storage import upload_resource_file as uploader

    row = await pool.fetchrow(
        """
        SELECT p.id, p.resource_id, p.goal_id, p.story_id, p.filename,
               p.content, p.note, r.gcs_uri AS old_uri, r.name,
               r.file_type
          FROM resource_publishes p
          JOIN goal_resources r ON r.id = p.resource_id
         WHERE p.id = $1 AND p.status = 'pending'
        """, publish_id)
    if row is None:
        return False

    try:
        new_uri = await uploader(
            row["resource_id"], f"v{publish_id}-{row['filename']}",
            bytes(row["content"]))
    except Exception as exc:                                # noqa: BLE001
        await pool.execute(
            "UPDATE resource_publishes SET status='failed', error=$2 "
            "WHERE id=$1", publish_id, str(exc)[:500])
        _log.exception("finalize_publish: upload failed for %s", publish_id)
        return False

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE goal_resources
               SET gcs_uri = $2, file_size_bytes = $3, summary_stale = true
             WHERE id = $1
            """,
            row["resource_id"], new_uri, len(row["content"]))
        await conn.execute(
            """
            INSERT INTO resource_events
                (resource_id, goal_id, event_type, actor, details)
            VALUES ($1, $2, 'published', $3, $4)
            """,
            row["resource_id"], row["goal_id"],
            f"story:{row['story_id']}",
            json.dumps({"publish": publish_id, "new_uri": new_uri,
                        # v1 retained: the previous object is not touched,
                        # and this event is the pointer back to it.
                        "previous_uri": row["old_uri"],
                        "note": row["note"]}))
        await conn.execute(
            "UPDATE resource_publishes SET status='published', "
            "published_uri=$2, published_at=now() WHERE id=$1",
            publish_id, new_uri)

    # Wake GO — a resource the goal depends on just changed, and the
    # planner should know without being told by a human.
    try:
        from app.services.go_wake_events import _deliver
        await _deliver(
            pool, row["goal_id"], "publish", f"pub:{publish_id}",
            f"[PUBLISHED] {row['name']} has a new version from story "
            f"{row['story_id']} ({row['filename']}, "
            f"{len(row['content'])} bytes)."
            + (f" Note: {row['note']}" if row["note"] else "")
            + " The previous version is retained; resource_brief will "
              "reflect it after the re-scan.")
    except Exception:                                       # noqa: BLE001
        _log.exception("finalize_publish: wake failed for %s", publish_id)

    # Re-scan the document so the brief's identity follows the content.
    try:
        from app.services.resource_scanner import scan_document
        import asyncio
        asyncio.get_event_loop().create_task(scan_document(
            row["resource_id"], row["goal_id"], new_uri,
            row["file_type"] or "", row["name"]))
    except Exception:                                       # noqa: BLE001
        pass
    _log.info("finalize_publish: %s -> %s", publish_id, new_uri)
    return True


async def sweep_pending(pool) -> int:
    """Backstop: finalize what NOTIFY missed. Idempotent via status."""
    rows = await pool.fetch(
        "SELECT id FROM resource_publishes WHERE status = 'pending' "
        "AND created_at < now() - interval '30 seconds' LIMIT 5")
    n = 0
    for r in rows:
        if await finalize_publish(pool, r["id"]):
            n += 1
    return n
