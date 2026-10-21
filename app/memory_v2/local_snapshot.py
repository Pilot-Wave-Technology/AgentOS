"""Local Execution Mode — memory handoff (M6, §14.3).

The gateway snapshot/restore (`snapshot.py`) moves the assistant's file-based
memory (`MEMORY.md` + `memory/*.md`) via gateway RPCs. A local story has no
gateway workspace, so these two functions reuse the SAME `agent_workspace_files`
table + queries but exchange the file CONTENT directly with the extension:

  read_local_memory  — CP → local: latest memory snapshot to SEED the checkout
  write_local_memory — local → CP: WRITE BACK memory changed during the story

Same key (the SHCA's `gateway_agent_id`) the gateway uses, so a later gateway
story's provision-time `restore_agent_workspace` sees these writes and vice
versa. Independent of `memory_v2_snapshot_enabled` (that flag gates only the
gateway reap-direction; cross-mode handoff is core to Local Execution Mode).
CP-side dedup skips re-inserting unchanged files.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from app.database import get_pool
from app.memory_v2.snapshot import _is_memory_filename, memory_key  # reuse allow-list + key
from app.services.agent_registry.assistant_queries import get_user_assistant_agent_id_by_uid

_log = logging.getLogger(__name__)


async def _live_gateway_instance_id(story_id: str) -> Optional[str]:
    """The LIVE gateway SHCA instance id for the story's (goal, checkout-user), or None.
    Excludes the synthetic `…-local` instance (it has no gateway workspace)."""
    return await get_pool().fetchval(
        "SELECT ai.gateway_agent_id FROM agent_instances ai "
        "JOIN stories s ON s.goal_id = ai.goal_id "
        "JOIN agents  a ON a.id = ai.template_id "
        "JOIN users   u ON u.actor_id = ai.owner_actor_id "
        "WHERE s.id = $1 AND s.checked_out_to_uid = u.uid "
        "  AND a.type = 'SHCA' AND ai.status = 'active' "
        "  AND ai.gateway_agent_id NOT LIKE '%-local' "
        "ORDER BY ai.created_at DESC LIMIT 1",
        story_id,
    )


async def _pull_live_gateway_memory(story_id: str, store_key: str) -> None:
    """Best-effort: snapshot the LIVE gateway SHCA instance's current memory into the DB
    (under `store_key`) so a local seed gets the FRESHEST web-assistant memory, not just
    the last reap snapshot. No-op if there is no live gateway instance for this
    (goal, user) or the gateway is unreachable."""
    try:
        gateway_id = await _live_gateway_instance_id(story_id)
        if not gateway_id:
            return
        from app.memory_v2.snapshot import snapshot_agent_workspace  # noqa: PLC0415
        await snapshot_agent_workspace(gateway_id, reason="manual", db_key=store_key)
    except Exception:  # noqa: BLE001 — freshness pull is best-effort
        _log.debug("local memory: live-gateway pull skipped for %s", story_id, exc_info=True)


async def _push_live_gateway_memory(story_id: str, store_key: str) -> None:
    """Best-effort reverse of the pull: after a local writeback lands in the DB, push it
    to the LIVE gateway SHCA instance's VM workspace so a RUNNING web/UI assistant sees
    the update immediately. The assistant reads MEMORY.md from its VM file, not the DB —
    the DB→VM restore otherwise only runs at provision time, so a live session would show
    stale memory until its next reset. No-op if there's no live gateway instance."""
    try:
        gateway_id = await _live_gateway_instance_id(story_id)
        if not gateway_id:
            return
        from app.memory_v2.snapshot import restore_agent_workspace  # noqa: PLC0415
        await restore_agent_workspace(gateway_id, db_key=store_key)
    except Exception:  # noqa: BLE001 — live push is best-effort
        _log.debug("local memory: live-gateway push skipped for %s", story_id, exc_info=True)


async def _resolve_key(story_id: str) -> Optional[str]:
    """Canonical per-(goal, user) memory key — the SAME `{agent}-{goal}` key the gateway
    snapshot/restore uses, so local + web memory converge into ONE agent_workspace_files
    row. (Was the per-instance gateway_agent_id — the synthetic `…-local` for local — a
    separate store that forked local memory from the web assistant's.)"""
    row = await get_pool().fetchrow(
        "SELECT goal_id, checked_out_to_uid FROM stories WHERE id = $1", story_id,
    )
    if not row or not row["checked_out_to_uid"]:
        return None
    agent_id = await get_user_assistant_agent_id_by_uid(get_pool(), row["checked_out_to_uid"])
    return memory_key(agent_id, row["goal_id"]) if agent_id else None


async def read_local_memory(story_id: str) -> dict:
    """Latest version of each memory file for the story's assistant → {filename: content}."""
    key = await _resolve_key(story_id)
    if not key:
        return {"ok": True, "files": {}}
    await _pull_live_gateway_memory(story_id, key)  # freshen from the live web session first
    rows = await get_pool().fetch(
        """SELECT DISTINCT ON (filename) filename, content, snapshot_at
             FROM agent_workspace_files
            WHERE agent_id = $1
         ORDER BY filename, snapshot_at DESC""",
        key,
    )
    # ── `meta` RIDES ALONGSIDE `files`, AND IS WHY ──────────────────
    #
    # The seeder cannot clobber a local file, because a crash-time edit
    # would be lost. That rule is right and its consequence was not: a
    # checkout directory is never deleted on release, so re-opening a
    # story worked months ago plants a months-old MEMORY.md that then
    # BEATS the fresh snapshot -- and write-back pushes it over the live
    # web assistant's workspace wholesale. An unchanged stale file
    # winning against a newer remote is the whole defect.
    #
    # To tell "unchanged since I seeded it" from "edited since", the
    # client needs the identity of what it was given. It gets both a
    # hash and the snapshot time: the hash settles it exactly, and the
    # time is what a checkout seeded before this existed can still be
    # judged by.
    #
    # ADDITIVE. `files` is unchanged, so a client that ignores `meta`
    # behaves exactly as it did.
    meta = {
        r["filename"]: {
            "sha256": hashlib.sha256((r["content"] or "").encode("utf-8")).hexdigest(),
            "snapshot_at": r["snapshot_at"].isoformat() if r["snapshot_at"] else None,
        }
        for r in rows
    }
    return {"ok": True,
            "files": {r["filename"]: r["content"] for r in rows},
            "meta": meta}


async def write_local_memory(story_id: str, files: dict,
                             consolidated: bool = False) -> dict:
    """Write back memory files produced/changed locally.

    Enforces the memory allow-list, and dedups: a file whose latest snapshot
    already equals the submitted content is skipped (no churn on a pure
    coding story).

    `consolidated` IS A CLAIM ABOUT WHAT HAPPENED, and the only reason the
    control plane needs it. When a local story goes terminal while checked
    out, `event_bridge` skips the gateway SHCA's story-complete flush
    because the extension "authors its own" -- but the extension's flush is
    best-effort: the human can cancel it, the model can fail to emit the
    sentinel, and the 180s timeout is indistinguishable from either. A
    skipped local flush met a suppressed gateway flush and the story
    finished with NO assistant checkpoint in either lane.

    So the suppression keys on this stamp rather than on the checkout, and
    the fallback fires exactly when the thing it is a fallback for did not
    happen.

    `skipped: "no_assistant_instance"` IS A FAILURE, and the caller must
    treat it as one. It is returned with ok=True because nothing went wrong
    HERE -- but it means the lease expired and the reaper cleared
    `checked_out_to_uid`, so there is no key to write under and the memory
    is on disk and nowhere else.
    """
    key = await _resolve_key(story_id)
    if not key:
        return {"ok": True, "written": 0, "skipped": "no_assistant_instance"}
    pool = get_pool()
    written = 0
    for filename, content in (files or {}).items():
        if not _is_memory_filename(filename):
            continue
        content = content or ""
        latest = await pool.fetchval(
            "SELECT content FROM agent_workspace_files "
            "WHERE agent_id = $1 AND filename = $2 ORDER BY snapshot_at DESC LIMIT 1",
            key, filename,
        )
        if latest == content:
            continue
        await pool.execute(
            "INSERT INTO agent_workspace_files "
            "(agent_id, filename, content, bytes, snapshot_reason) "
            "VALUES ($1, $2, $3, $4, 'manual')",
            key, filename, content, len(content.encode("utf-8")),
        )
        written += 1
    if written:
        # Propagate to a RUNNING web/UI assistant so it sees the update now, not just at
        # its next provision/reset (it reads MEMORY.md from the VM, not this table).
        await _push_live_gateway_memory(story_id, key)
    # THE STAMP, and only on a real claim. A write-back that carried no
    # consolidation leaves it NULL, which is what makes the gateway flush
    # fire as the fallback.
    if consolidated:
        await get_pool().execute(
            "UPDATE stories SET local_memory_consolidated_at = now() "
            "WHERE id = $1", story_id)

    return {"ok": True, "written": written}
