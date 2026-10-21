"""Workspace memory files snapshot/restore for SHCA + GO agents.

Without this, when the SHCA cleanup watchdog calls `agents.delete` on an
idle SHCA, the workspace dir (and all the agent's MEMORY.md + memory/*.md)
is destroyed. Re-spawn would start from a blank slate.

Snapshots happen:
  - `pre_reap` — fired by `_shca_cleanup_watchdog` right before agents.delete
  - `periodic` — every 6 hours for active SHCAs/GOs (safety net)
  - `manual` — admin endpoint (future)

Restore happens at agent provisioning time. If the agent has any prior
snapshot rows, the latest version of each file is pushed back into the
gateway workspace via `agents.files.set`.

Retention: pruned to last 10 snapshots per (agent_id, filename) by a
nightly task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError


_log = logging.getLogger(__name__)

# Memory file paths we snapshot. memory.list returns only allowed paths,
# so this is mostly a safety filter.
_MEMORY_FILE_PREFIXES = ("MEMORY.md", "memory/")


def _is_memory_filename(filename: str) -> bool:
    return any(filename == p or filename.startswith(p) for p in _MEMORY_FILE_PREFIXES)


def memory_key(agent_id: str, goal_id: str) -> str:
    """Canonical `agent_workspace_files.agent_id` for per-(goal, user) assistant memory.
    Shared by the LOCAL path and the gateway snapshot/restore so both read/write ONE
    store, regardless of thread or local-vs-gateway instance. Stable — no thread, no
    synthetic suffix. NB: gateway RPCs still address the LIVE agent by its per-instance
    `gateway_agent_id`; this is the DB key only (translate instance→(agent,goal) at the
    DB boundary). `agent_id` is the abstract assistant `agents.id`."""
    return f"{agent_id}-{goal_id}"


async def _db_key_for(gateway_agent_id: str, db_key: Optional[str] = None) -> str:
    """The canonical `agent_workspace_files.agent_id` for a gateway INSTANCE id. Prefer
    an explicit `db_key` (callers that already know the abstract agent + goal pass it —
    avoids the raw-vs-normalized gateway-id mismatch); else translate the instance id to
    `{agent}-{goal}` via `agent_instances`; else fall back to the raw id so legacy /
    abstract-agent callers still work."""
    if db_key:
        return db_key
    try:
        row = await get_pool().fetchrow(
            "SELECT template_id, goal_id FROM agent_instances "
            "WHERE gateway_agent_id = $1 AND template_id IS NOT NULL AND goal_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            gateway_agent_id,
        )
    except Exception:  # noqa: BLE001
        row = None
    if row and row["template_id"] and row["goal_id"]:
        return memory_key(row["template_id"], row["goal_id"])
    return gateway_agent_id


# ── Snapshot ─────────────────────────────────────────────────────────────────

async def snapshot_agent_workspace(agent_id: str, reason: str, db_key: Optional[str] = None) -> int:
    """Snapshot all memory files in an agent's workspace into PG under the canonical
    `{agent}-{goal}` key (so local + gateway share ONE store). `agent_id` is the gateway
    INSTANCE id used for the RPCs; `db_key` overrides the DB key (else translated).

    Returns count of files snapshotted. Failures are logged and the
    function returns whatever it managed to snapshot — never raises into
    the caller (the watchdog must continue regardless).
    """
    if reason not in ("pre_reap", "periodic", "manual", "pre_gateway_restart"):
        _log.warning("snapshot_agent_workspace: invalid reason=%r — skipping", reason)
        return 0
    store_key = await _db_key_for(agent_id, db_key)

    try:
        list_result = await gateway_admin.call(
            "memory.list", {"agentId": agent_id}, timeout=15,
        )
    except GatewayError as exc:
        _log.warning("snapshot: memory.list failed for %s: %s", agent_id, exc)
        return 0
    except Exception:
        _log.exception("snapshot: memory.list raised for %s", agent_id)
        return 0

    entries = list_result.get("entries") or []
    if not entries:
        _log.info("snapshot: no memory files for %s (reason=%s)", agent_id, reason)
        return 0

    pool = get_pool()
    snapshotted = 0
    for entry in entries:
        filename = entry.get("filename") or ""
        if not _is_memory_filename(filename):
            continue
        # agents.files.get serves only top-level allow-listed files (MEMORY.md); it
        # rejects `memory/` subpaths. The daily memory/*.md logs consolidate into
        # MEMORY.md, so capturing MEMORY.md is sufficient — skip the rest quietly.
        if "/" in filename:
            continue
        try:
            file_result = await gateway_admin.call(
                "agents.files.get",
                {"agentId": agent_id, "name": filename},  # gateway param is `name`, not `filename`
                timeout=30,
            )
        except Exception:
            _log.exception("snapshot: failed to read %s/%s", agent_id, filename)
            continue

        content = (file_result.get("file") or {}).get("content")
        if content is None:
            continue
        bytes_count = len(content.encode("utf-8"))

        try:
            await pool.execute(
                """INSERT INTO agent_workspace_files
                     (agent_id, filename, content, bytes, snapshot_reason)
                   VALUES ($1, $2, $3, $4, $5)""",
                store_key, filename, content, bytes_count, reason,
            )
            snapshotted += 1
        except Exception:
            _log.exception(
                "snapshot: PG insert failed for %s/%s — continuing with next file",
                agent_id, filename,
            )

    _log.info(
        "snapshot: %d files snapshotted for %s (reason=%s)",
        snapshotted, agent_id, reason,
    )
    return snapshotted


# ── Restore ──────────────────────────────────────────────────────────────────

async def has_prior_snapshot(agent_id: str, db_key: Optional[str] = None) -> bool:
    pool = get_pool()
    store_key = await _db_key_for(agent_id, db_key)
    return bool(await pool.fetchval(
        "SELECT 1 FROM agent_workspace_files WHERE agent_id = ANY($1) LIMIT 1",
        list({store_key, agent_id}),  # canonical key, or legacy raw-id rows
    ))


async def restore_agent_workspace(agent_id: str, db_key: Optional[str] = None) -> int:
    """Restore the latest snapshot of each memory file to the agent's workspace via
    `agents.files.set`. Reads under the canonical `{agent}-{goal}` key (so it picks up
    LOCAL writes too), with a fallback to legacy raw-id rows. `agent_id` is the gateway
    INSTANCE id for the RPC.

    Called at agent provisioning time after the per-user template files are seeded but
    before the first session message. Returns count of files restored. Non-fatal.
    """
    pool = get_pool()
    store_key = await _db_key_for(agent_id, db_key)
    rows = await pool.fetch(
        """SELECT DISTINCT ON (filename) filename, content, snapshot_at
             FROM agent_workspace_files
            WHERE agent_id = $1
            ORDER BY filename, snapshot_at DESC""",
        store_key,
    )
    if not rows and store_key != agent_id:
        rows = await pool.fetch(
            """SELECT DISTINCT ON (filename) filename, content, snapshot_at
                 FROM agent_workspace_files
                WHERE agent_id = $1
                ORDER BY filename, snapshot_at DESC""",
            agent_id,
        )
    if not rows:
        return 0

    restored = 0
    for r in rows:
        # agents.files.set (like .get) only accepts top-level allow-listed files and
        # rejects `memory/` subpaths — restore MEMORY.md (the durable, consolidated file).
        if "/" in r["filename"]:
            continue
        try:
            await gateway_admin.call(
                "agents.files.set",
                {
                    "agentId": agent_id,
                    "name": r["filename"],  # gateway param is `name`, not `filename`
                    "content": r["content"],
                },
                timeout=30,
            )
            restored += 1
        except Exception:
            _log.exception(
                "restore: failed to push %s/%s back to workspace",
                agent_id, r["filename"],
            )

    _log.info("restore: %d files restored for %s", restored, agent_id)
    return restored


# ── Periodic snapshot task ──────────────────────────────────────────────────

async def periodic_snapshot_loop(interval_seconds: int = 6 * 3600) -> None:
    """Background task — snapshot all active SHCA + GO workspaces every N
    seconds (default 6h). Started from app/main.py.
    """
    _log.info("memory_v2.snapshot: periodic loop started (interval=%ds)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            pool = get_pool()
            agents = await pool.fetch(
                """SELECT id FROM agents
                    WHERE status = 'active' AND type IN ('SHCA', 'GO')"""
            )
            _log.info("memory_v2.snapshot: periodic run starting for %d agents", len(agents))
            for a in agents:
                try:
                    await snapshot_agent_workspace(a["id"], reason="periodic")
                except Exception:
                    _log.exception("periodic snapshot failed for %s", a["id"])
        except asyncio.CancelledError:
            _log.info("memory_v2.snapshot: periodic loop cancelled")
            raise
        except Exception:
            _log.exception("memory_v2.snapshot: periodic loop iteration crashed")


# ── Retention prune ─────────────────────────────────────────────────────────

async def prune_old_snapshots(keep_per_file: int = 10) -> int:
    """Delete snapshots older than the most-recent `keep_per_file` per
    (agent_id, filename). Returns the number of rows deleted.

    Run periodically (e.g., nightly) to bound the table size.
    """
    pool = get_pool()
    deleted = await pool.fetchval(
        """WITH ranked AS (
              SELECT id, ROW_NUMBER() OVER (
                  PARTITION BY agent_id, filename
                  ORDER BY snapshot_at DESC
              ) AS rn
                FROM agent_workspace_files
           )
           DELETE FROM agent_workspace_files
            WHERE id IN (SELECT id FROM ranked WHERE rn > $1)
           RETURNING (SELECT COUNT(*) FROM ranked WHERE rn > $1)""",
        keep_per_file,
    )
    return int(deleted or 0)
