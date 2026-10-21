"""Shared backwards pagination for message threads.

Every thread page in the app (GO thread, plus the read-only council, security
and orchestrator channels) used to render EVERY message in the thread with no
LIMIT. Page load therefore scaled linearly with conversation length: a large
query, a large Jinja render, and a large DOM. Long-running goals made the pages
unusable.

All four now render only the newest window and page backwards on demand. This
module owns the parts that are identical across them so the paging rules exist
in exactly one place.

Paging design (the non-obvious bits):

* **Keyset, not OFFSET.** OFFSET re-scans every skipped row on each page, which
  is precisely the cost being removed. Each page walks the existing
  ``idx_messages_thread (thread_id, created_at)`` index backwards from a cursor
  and stops at the limit, so page 40 costs the same as page 1. No migration was
  needed.

* **The cursor is the tuple (created_at, id), never created_at alone.**
  ``created_at`` is not unique. Two messages sharing a timestamp across a page
  boundary would be silently skipped or duplicated by a scalar cursor.

* **Has-more is a probe row, not a COUNT.** Each query asks for ``limit + 1``
  rows; if the extra row comes back there is older history. The probe row is
  then discarded. A COUNT(*) would re-scan the whole thread on every page.

* **Backwards paging cannot collide with live appends.** They move in opposite
  directions over disjoint sets: a cursor is always pinned to the oldest loaded
  message, while WebSocket arrivals are always newer than any cursor a client
  can hold. The forward catch-up path is therefore untouched by this module.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.config import get_settings
from app.database import get_pool

_log = logging.getLogger(__name__)

_templates = Jinja2Templates(directory="app/templates")

# Page sizes. INITIAL is larger because it is the only fetch most sessions ever
# make; EARLIER trades a smaller payload for a snappier button.
#
# Settings-backed so local dev can exercise the "Load earlier" path against a
# small database — a fresh local stack rarely has a thread anywhere near 100
# messages, which makes the button (correctly) never appear:
#     THREAD_INITIAL_MESSAGE_LIMIT=10  THREAD_EARLIER_PAGE_SIZE=5
_settings = get_settings()
INITIAL_MESSAGE_LIMIT = _settings.thread_initial_message_limit
EARLIER_PAGE_SIZE = _settings.thread_earlier_page_size


def split_probe_row(rows: list, page_size: int) -> tuple[list, bool]:
    """Split a ``page_size + 1`` fetch into (page, has_older).

    Rows must be in ASCENDING time order, so the probe row — the one extra,
    oldest record proving more history exists — is at the front.
    """
    if len(rows) > page_size:
        return list(rows[1:]), True
    return list(rows), False


# ── Read-only agent channels (council / security / orchestrator) ────────────
#
# These three share an identical message shape and filter set, so they share
# these queries. The GO thread has its own (it joins users/agents to resolve a
# display name and excludes 'system' rather than the kind-based filters below),
# and keeps its queries in app/routers/goal_thread.py.

_CHANNEL_COLUMNS = "m.id, m.sender, m.role, m.content, m.created_at, m.card_data"

# Excludes rows that exist purely to drive the agent, not to be read by humans:
# injected context, and bare wake pings that carry no card payload.
_CHANNEL_FILTERS = """m.kind <> 'system_inject'
      AND NOT (m.kind = 'system_wake' AND m.sender = 'system' AND m.card_data IS NULL)"""


def _normalize_channel_rows(rows) -> list[dict]:
    """Parse card_data JSONB so templates can read card fields directly.

    asyncpg hands JSONB back as a string. A malformed payload degrades to a
    plain message rather than breaking the whole page render.
    """
    messages = []
    for row in rows:
        msg = dict(row)
        if isinstance(msg.get("card_data"), str) and msg["card_data"]:
            try:
                msg["card_data"] = json.loads(msg["card_data"])
            except Exception:
                _log.warning("thread-paging: unparseable card_data on message %s", msg.get("id"))
                msg["card_data"] = None
        messages.append(msg)
    return messages


async def fetch_channel_page(pool, thread_id: str) -> tuple[list[dict], bool]:
    """Return (newest window of messages ascending, has_older) for a channel."""
    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_CHANNEL_COLUMNS}
                  FROM messages m
                 WHERE m.thread_id = $1
                   AND {_CHANNEL_FILTERS}
                 ORDER BY m.created_at DESC, m.id DESC
                 LIMIT $2
           ) newest
           ORDER BY created_at ASC, id ASC""",
        thread_id, INITIAL_MESSAGE_LIMIT + 1,
    )
    page, has_older = split_probe_row(rows, INITIAL_MESSAGE_LIMIT)
    return _normalize_channel_rows(page), has_older


async def fetch_channel_page_before(
    pool, thread_id: str, before_id: str,
) -> tuple[list[dict], bool]:
    """Return (page of messages older than `before_id` ascending, has_older).

    An unknown ``before_id`` — a stale tab, or a message since removed — yields
    an empty terminal page rather than an error, so the button degrades to
    "beginning of history" instead of breaking.
    """
    anchor = None
    if before_id:
        anchor = await pool.fetchrow(
            "SELECT created_at, id FROM messages WHERE id = $1 AND thread_id = $2",
            before_id, thread_id,
        )
    if anchor is None:
        _log.warning(
            "thread-paging: earlier-page anchor not found thread=%s before_id=%r",
            thread_id, before_id,
        )
        return [], False

    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_CHANNEL_COLUMNS}
                  FROM messages m
                 WHERE m.thread_id = $1
                   AND {_CHANNEL_FILTERS}
                   AND (m.created_at, m.id) < ($2, $3)
                 ORDER BY m.created_at DESC, m.id DESC
                 LIMIT $4
           ) older
           ORDER BY created_at ASC, id ASC""",
        thread_id, anchor["created_at"], anchor["id"], EARLIER_PAGE_SIZE + 1,
    )
    page, has_older = split_probe_row(rows, EARLIER_PAGE_SIZE)
    return _normalize_channel_rows(page), has_older


def register_channel_earlier_route(
    router: APIRouter,
    *,
    channel_slug: str,
    thread_id_column: str,
    accent: str,
    label_mode: str = "sender",
) -> None:
    """Register ``GET /goals/{pid}/<slug>/messages/earlier`` on `router`.

    A factory rather than three copies: council, security and orchestrator need
    byte-identical handlers differing only in which goals column holds the
    thread id and how the rows are styled/labelled. Registering them from one
    place keeps the auth check, the cursor handling and the response shape from
    drifting apart across the three channels.

    Returns rendered HTML rather than JSON so the rows reuse the very same
    ``channels/_message.html`` the full page render uses — a JSON endpoint would
    need a second, client-side copy of the row markup that would inevitably
    diverge.

    Args:
        channel_slug: URL segment and the page's route prefix (e.g. "council").
        thread_id_column: column on `goals` holding this channel's thread id.
        accent: Tailwind colour stem for card chrome and header pills.
        label_mode: "sender" (council/security) or "role" (orchestrator).
    """

    @router.get(f"/goals/{{pid}}/{channel_slug}/messages/earlier", response_class=HTMLResponse)
    async def get_channel_messages_earlier(  # noqa: D401 — registered, not called directly
        pid: str,
        request: Request,
        before_id: str = "",
        user: dict = Depends(get_current_user),
    ):
        role = await get_goal_role(user["uid"], pid)
        if role is None:  # Must be a goal member
            raise HTTPException(status_code=403)

        pool = get_pool()
        # Column name is a literal from this module's own call sites, never user
        # input — no injection surface.
        goal = await pool.fetchrow(
            f"SELECT {thread_id_column} AS thread_id FROM goals WHERE id = $1", pid,
        )
        if not goal or not goal["thread_id"]:
            raise HTTPException(status_code=404, detail=f"{channel_slug} channel not found")

        messages, has_older = await fetch_channel_page_before(
            pool, goal["thread_id"], before_id,
        )
        _log.info(
            "%s-channel: earlier page goal=%s before_id=%s returned=%d has_older=%s",
            channel_slug, pid, before_id, len(messages), has_older,
        )

        return _templates.TemplateResponse(
            "channels/_earlier_messages.html",
            {
                "request": request,
                "pid": pid,
                "messages": messages,
                "has_older": has_older,
                "accent": accent,
                "label_mode": label_mode,
                "load_earlier_url": f"/goals/{pid}/{channel_slug}/messages/earlier",
                # Next cursor: the oldest row in THIS page.
                "before_id": messages[0]["id"] if messages else "",
            },
        )
