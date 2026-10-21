"""Shared helper: upload a file + register as a goal_resource.

Originally inline in `app/routers/goal_resources.py upload_resource_file`.
Extracted so non-HTTP callers (Slack file-share events, future API clients,
CLI) can create goal_resources through the same persistence path.

Contract:
  - `file_bytes` is the raw content to persist.
  - `filename` is used as the storage key and default resource name.
  - `uploader_uid` is the control-panel user uid (not the Slack ID).
  - Returns the new resource_id on success, or None if validation fails
    (unsupported file type, too large, etc.) — the caller is responsible
    for surfacing that to the user.

Side effects:
  - GCS object written at `gs://<bucket>/<resource_id>/<filename>`
  - Rows added to `goal_resources`, `resource_attachments`, `resource_events`
  - Ledger event `GOAL_RESOURCE_REGISTERED` appended
  - Background document scan scheduled
  - go_events.on_resource_registered fires
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import NamedTuple, Optional

import asyncpg

from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import GOAL_RESOURCE_REGISTERED

_log = logging.getLogger(__name__)


class UploadResult(NamedTuple):
    resource_id: str
    is_new: bool  # False when we hit an existing resource with the same (goal_id, kind, name)

# Kept in sync with app/routers/goal_resources.py — single source of truth would
# be nicer but this mirror is small and rarely changes.
ALLOWED_FILE_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "yaml": "application/x-yaml",
    "yml": "application/x-yaml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


class ResourceUploadError(Exception):
    """Raised when validation fails (bad file type, too large, etc.)."""


async def upload_and_register_resource(
    *,
    goal_id: str,
    filename: str,
    file_bytes: bytes,
    uploader_uid: str,
    uploader_actor_id: Optional[str] = None,
    human_context: str = "",
    resource_name: str = "",
) -> UploadResult:
    """Upload + register a file. Returns (resource_id, is_new).

    Idempotent on the goal_resources uq(goal_id, kind, name) constraint —
    concurrent uploads of the same filename (e.g. Slack webhook retries
    firing the handler twice) return the already-existing resource_id
    instead of raising.

    Raises ResourceUploadError on validation failures the caller should
    surface to the end user.
    """
    if not filename:
        raise ResourceUploadError("filename required")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_FILE_TYPES:
        raise ResourceUploadError(f"Unsupported file type: .{ext}")

    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise ResourceUploadError(
            f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024} MB)"
        )
    if not file_bytes:
        raise ResourceUploadError("File is empty")

    # Canonicalise the filename ONCE, here at the upload chokepoint.
    # Same safe form propagates to:
    #   - GCS object path  (gs://bucket/<id>/<filename>)
    #   - goal_resources.name  (used by RESOURCES.md, lookups, refs)
    #   - workspace path        (docs/<filename> on the agent disk)
    # Previously each layer applied its own sanitisation independently
    # — the workspace got underscores, the DB kept spaces, the agent's
    # tool calls failed because RESOURCES.md said one thing and the
    # filesystem said another. The original filename is preserved in
    # `resource_attachments.filename` (and inside this function as
    # `original_filename` for the source-event payload) for audit.
    from app.resource_provisioning import _safe_doc_filename
    original_filename = filename
    safe_filename = _safe_doc_filename(filename) or filename
    filename = safe_filename
    if resource_name:
        # An explicit display-name override: sanitise it too so the
        # canonical guarantee holds.
        resource_name = _safe_doc_filename(resource_name) or resource_name

    name = (resource_name or filename).strip() or filename
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    pool = get_pool()

    from app.storage import upload_resource_file as _upload_to_gcs
    content_type = ALLOWED_FILE_TYPES.get(ext, "application/octet-stream")

    # Look up any existing row with the same (goal_id, kind, name). Behaviour
    # splits on status:
    #   active / consumed → true duplicate; skip the upload and return the id.
    #   archived / deleted → user wants to bring it back. Reuse the same
    #                        resource_id (GCS path is {id}/{filename}), overwrite
    #                        the blob, flip status=active, mark summary_stale
    #                        so the doc scan re-runs.
    existing = await pool.fetchrow("""
        SELECT id, status FROM goal_resources
         WHERE goal_id = $1 AND kind = 'document' AND name = $2
         LIMIT 1
    """, goal_id, name)

    if existing and existing["status"] in ("active", "consumed"):
        _log.info(
            "Resource already registered (pre-check): goal=%s rid=%s name=%r status=%s",
            goal_id, existing["id"], name, existing["status"],
        )
        return UploadResult(resource_id=existing["id"], is_new=False)

    if existing and existing["status"] in ("archived", "deleted"):
        revived_id = existing["id"]
        # Overwrite the blob at the same path as the original upload.
        gcs_uri = await _upload_to_gcs(
            revived_id, filename, file_bytes, content_type=content_type,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    UPDATE goal_resources
                       SET status = 'active',
                           file_size_bytes = $1,
                           -- BOTH, for the reason the insert above
                           -- gives: `uri` is what the kernel reads, and
                           -- a revived row that keeps its old `uri`
                           -- points at an object that is gone.
                           uri = $2,
                           gcs_uri = $2,
                           summary_stale = true,
                           updated_at = now()
                     WHERE id = $3
                """, len(file_bytes), gcs_uri, revived_id)
                await conn.execute("""
                    INSERT INTO resource_attachments
                        (resource_id, goal_id, filename, file_type, file_size_bytes,
                         gcs_uri, uploaded_by, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """, revived_id, goal_id, original_filename, ext, len(file_bytes),
                    gcs_uri, uploader_uid, content_hash)
                await conn.execute("""
                    INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                    VALUES ($1, $2, 'revived', $3, $4)
                """, revived_id, goal_id, uploader_uid,
                    json.dumps({
                        "filename":          name,
                        "original_filename": original_filename,
                        "type":              ext,
                        "size":              len(file_bytes),
                        "previous_status":   existing["status"],
                    }))
        try:
            from app.routers.goal_resources import _background_doc_scan
            asyncio.create_task(
                _background_doc_scan(revived_id, goal_id, gcs_uri, ext, name)
            )
        except Exception as exc:
            _log.warning("Failed to schedule doc scan for revived %s: %s", revived_id, exc)
        try:
            from app.services.go_events import on_resource_registered
            await on_resource_registered(goal_id, revived_id, uploader_uid)
        except Exception as exc:
            _log.warning("on_resource_registered (revived) failed for %s: %s", revived_id, exc)

        try:
            from app.channels.base import relay_lifecycle_event
            asyncio.create_task(relay_lifecycle_event(goal_id, "resource.registered", {
                "resource_id":  revived_id,
                "filename":     name,
                "file_type":    ext,
                "file_size_kb": len(file_bytes) // 1024,
                "uploader_uid": uploader_uid,
            }))
        except Exception as exc:
            _log.debug("relay resource.registered (revived) failed goal=%s: %s", goal_id, exc)

        # NOTE: refresh_all_goal_sessions is intentionally NOT called here —
        # callers handle it AFTER any story_resources linking they do, so the
        # SHCA's story-scoped refresh query sees the new link.

        _log.info(
            "Revived archived resource: goal=%s rid=%s name=%r prev_status=%s size=%d",
            goal_id, revived_id, name, existing["status"], len(file_bytes),
        )
        return UploadResult(resource_id=revived_id, is_new=True)

    # No existing row — fresh upload.
    resource_id = str(uuid.uuid4())[:12]
    gcs_uri = await _upload_to_gcs(
        resource_id, filename, file_bytes, content_type=content_type,
    )

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    event_type=GOAL_RESOURCE_REGISTERED,
                    payload={
                        "resource_id":       resource_id,
                        "kind":              "document",
                        "name":              name,
                        "original_filename": original_filename,
                        "file_type":         ext,
                        "file_size":         len(file_bytes),
                        "source":            "slack" if not uploader_actor_id else "web",
                    },
                    actor_id=uploader_actor_id,
                    context_goal_id=goal_id,
                )
                await conn.execute("""
                    -- `uri` IS THE LOCATOR, and writing only `gcs_uri`
                    -- makes the resource INVISIBLE TO THE KERNEL: no
                    -- mount, no egress host, and nothing raises.
                    -- `test_resource_uri_is_one_column` names this
                    -- exact failure as the one thing its removal of
                    -- `gcs_uri` from kernel/ depends on -- "every
                    -- writer populates `uri`" -- and this writer did
                    -- not. GOAL1 story 4bb5e544: three documents
                    -- uploaded from the resources page, an empty
                    -- `/workspace/docs/`, and a WORKSPACE.md telling
                    -- the executor to read them.
                    INSERT INTO goal_resources
                        (id, goal_id, kind, name, uri, created_by,
                         human_context, file_type, file_size_bytes,
                         gcs_uri, summary_stale)
                    VALUES ($1, $2, 'document', $3, $4, $5, $6, $7, $8, $9,
                            true)
                """, resource_id, goal_id, name, gcs_uri, uploader_uid,
                    human_context, ext, len(file_bytes), gcs_uri)
                # resource_attachments.filename keeps the ORIGINAL (pre-
                # sanitisation) name so the audit trail shows what the
                # human actually uploaded. The canonical safe name lives
                # on goal_resources.name and the GCS path.
                await conn.execute("""
                    INSERT INTO resource_attachments
                        (resource_id, goal_id, filename, file_type, file_size_bytes,
                         gcs_uri, uploaded_by, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """, resource_id, goal_id, original_filename, ext, len(file_bytes),
                    gcs_uri, uploader_uid, content_hash)
                await conn.execute("""
                    INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                    VALUES ($1, $2, 'created', $3, $4)
                """, resource_id, goal_id, uploader_uid,
                    json.dumps({
                        "filename":          name,
                        "original_filename": original_filename,
                        "type":              ext,
                        "size":              len(file_bytes),
                    }))
    except asyncpg.exceptions.UniqueViolationError:
        # Race: two concurrent uploads (likely a Slack webhook retry that
        # fired before our first handler committed). Look up the winner and
        # return its id — the caller treats is_new=False as "duplicate".
        winner = await pool.fetchval("""
            SELECT id FROM goal_resources
             WHERE goal_id = $1 AND kind = 'document' AND name = $2
             LIMIT 1
        """, goal_id, name)
        _log.info(
            "Resource upload race lost: goal=%s my_rid=%s winner=%s name=%r",
            goal_id, resource_id, winner, name,
        )
        if winner:
            return UploadResult(resource_id=winner, is_new=False)
        # Shouldn't happen — constraint hit but no row? Re-raise to surface it.
        raise

    # Background: doc scan + notify GO — both best-effort.
    try:
        from app.routers.goal_resources import _background_doc_scan
        asyncio.create_task(
            _background_doc_scan(resource_id, goal_id, gcs_uri, ext, name)
        )
    except Exception as exc:
        _log.warning("Failed to schedule doc scan for %s: %s", resource_id, exc)

    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(goal_id, resource_id, uploader_uid)
    except Exception as exc:
        _log.warning("on_resource_registered failed for %s: %s", resource_id, exc)

    try:
        from app.channels.base import relay_lifecycle_event
        asyncio.create_task(relay_lifecycle_event(goal_id, "resource.registered", {
            "resource_id":  resource_id,
            "filename":     name,
            "file_type":    ext,
            "file_size_kb": len(file_bytes) // 1024,
            "uploader_uid": uploader_uid,
        }))
    except Exception as exc:
        _log.debug("relay resource.registered failed goal=%s: %s", goal_id, exc)

    # NOTE: refresh_all_goal_sessions is intentionally NOT called here —
    # callers handle it AFTER any story_resources linking they do, so the
    # SHCA's story-scoped refresh query sees the new link. See
    # app/slack/inbound.py _handle_file_uploads and
    # app/routers/goal_resources.py upload_resource_file.

    _log.info(
        "Uploaded resource: goal=%s rid=%s name=%r size=%d type=%s",
        goal_id, resource_id, name, len(file_bytes), ext,
    )
    return UploadResult(resource_id=resource_id, is_new=True)
