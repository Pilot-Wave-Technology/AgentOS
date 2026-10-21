"""Goal CRUD, membership, and join-request routes."""
import html as _html
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

_log = logging.getLogger(__name__)


# Goal ID format: 3-5 uppercase letters / digits, first char must be a letter.
# Drives Slack channel naming (channel = goal_id.lower(), e.g. AB1 → #ab1).
_GOAL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{2,4}$")


def _validate_goal_id(raw: str) -> str:
    """Normalise + validate a user-entered goal id.

    Returns the cleaned id (uppercased, stripped). Raises HTTPException
    with a 400 describing the rule if the input is invalid.
    """
    cleaned = (raw or "").strip().upper()
    if not _GOAL_ID_RE.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                "Goal ID must be 3-5 characters, uppercase letters and "
                "digits only, starting with a letter. "
                "Examples: ABC, WEB2, PROJ1, AB1X2."
            ),
        )
    return cleaned

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin, require_editor, get_goal_role
from app.roles import SystemRole, GoalRole, SYSTEM_ROLE_LABELS, GOAL_ROLE_LABELS
from app.database import get_pool, resolve_goal_id
from app.flash import flash_redirect
from app.models.goal import GoalCreate
from app.ledger.journal import append_event, generate_event_id
from app.ledger.schema import GOAL_CREATED, GOAL_MEMBER_ADDED, GOAL_MEMBER_ROLE_CHANGED, GOAL_COMPLETED, AGENT_PROVISIONING_FAILED
from app.ledger.actors import SYSTEM_ACTOR_ID
from app.services.go_agent import create_go_for_goal

router = APIRouter(prefix="/goals")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["SYSTEM_ROLE_LABELS"] = SYSTEM_ROLE_LABELS
templates.env.globals["GOAL_ROLE_LABELS"] = GOAL_ROLE_LABELS


# -- helpers ------------------------------------------------------------------

async def _get_user_goals(uid: str, is_admin: bool = False) -> list[dict]:
    """Get goals the user is a member of. Pinned goals first, most-recently-pinned at top."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT p.*, pm.pinned_at
           FROM goals p
           JOIN goal_members pm ON pm.goal_id = p.id
           WHERE pm.uid = $1 AND p.status = 'active'
             AND p.id <> '__platform__'   -- Platform council goal: admin-panel only
           ORDER BY pm.pinned_at DESC NULLS LAST, p.created_at DESC""",
        uid,
    )
    return [dict(r) for r in rows]


def _unread_badge(tid: str, count: int) -> str:
    """Small counter overlaid on a sidebar thread icon.

    Kept in sync live by the WS handler in base.html (initUserStream), which
    looks up spans by `data-thread-badge` and rewrites textContent/hidden —
    so the attribute name and the "hidden" class here must match exactly.
    """
    hidden = " hidden" if count == 0 else ""
    return f'<span data-thread-badge="{tid}" class="thread-unread-badge{hidden}">{count}</span>'


# -- sidebar data (HTMX partial) -----------------------------------------------

@router.get("/sidebar-data", response_class=HTMLResponse)
async def sidebar_goals(
    request: Request,
    user: dict = Depends(get_current_user),
    active_goal: str = "",
    current_path: str = "",
    current_thread: str = "",
):
    """Return Notion-style sidebar tree: goals > conversations + nav items."""
    pool = get_pool()
    goals = await _get_user_goals(
        user["uid"], user.get("system_role") == SystemRole.PLATFORM_ADMIN
    )
    if not goals:
        # admin/editor already have the "New Goal" button directly above this
        # tree (base.html) — no need for a second, redundant call to action here.
        empty_hint = (
            "" if user.get("system_role") in SystemRole.PRIVILEGED
            else '<p class="text-2xs text-gray-700 mt-1">Ask a platform admin or editor to add you to one.</p>'
        )
        return HTMLResponse(
            '<div class="px-2 py-6 text-center">'
            '<p class="text-2xs text-gray-600">No goals yet</p>'
            f'{empty_hint}'
            '</div>'
        )

    html = []

    for g in goals:
        gid = g["id"]
        color = g.get("color_tag", "#6366f1")
        is_active = gid == active_goal
        name = g["name"]

        # ── Goal row: pin btn + icon+chevron (toggle) + name (navigate) ──
        icon_active_cls = "bg-surface-hover" if is_active else ""
        chevron_rotate = "rotate-90" if is_active else ""
        name_cls = "text-gray-100 bg-surface-hover font-medium" if is_active else "text-gray-400 hover:text-gray-200 hover:bg-surface-hover"
        is_pinned = g.get("pinned_at") is not None
        pin_fill = "currentColor" if is_pinned else "none"
        pin_cls = "goal-pin-btn goal-pin-active" if is_pinned else "goal-pin-btn"
        pin_title = "Unpin from top" if is_pinned else "Pin to top"
        pin_url = f"/goals/sidebar-data/pin/{gid}?active_goal={active_goal}&current_path={current_path}&current_thread={current_thread}"
        name_esc = _html.escape(name)
        html.append(f'''<div class="mb-0.5">
  <div class="flex items-center gap-0.5 group relative">
    <button onclick="toggleGoalTree('{gid}')"
            title="{'Collapse' if is_active else 'Expand'}"
            class="goal-icon-btn row-toggle-btn flex-shrink-0 h-6 px-1 flex items-center justify-center gap-0.5 rounded hover:bg-surface-hover transition-colors {icon_active_cls}">
      <svg id="chevron-{gid}" class="w-2.5 h-2.5 flex-shrink-0 text-gray-600 transition-transform {chevron_rotate}"
           fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
      </svg>
      <svg class="w-3.5 h-3.5 flex-shrink-0" style="color:{color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 3v1.5M3 21v-6m0 0l2.77-.693a9 9 0 016.208.682l.108.054a9 9 0 006.086.71l3.114-.732a48.524 48.524 0 01-.005-10.499l-3.11.732a9 9 0 01-6.085-.711l-.108-.054a9 9 0 00-6.208-.682L3 4.5M3 15V4.5"/>
      </svg>
    </button>
    <a href="/goals/{gid}"
       data-tooltip-title="{name_esc}"
       class="goal-link flex items-center gap-1.5 flex-1 min-w-0 px-1.5 py-1.5 pr-5 rounded-md text-xs transition-colors {name_cls}">
      <svg class="rail-nav-icon w-3.5 h-3.5" style="color:{color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 3v1.5M3 21v-6m0 0l2.77-.693a9 9 0 016.208.682l.108.054a9 9 0 006.086.71l3.114-.732a48.524 48.524 0 01-.005-10.499l-3.11.732a9 9 0 01-6.085-.711l-.108-.054a9 9 0 00-6.208-.682L3 4.5M3 15V4.5"/>
      </svg>
      <span class="sidebar-label truncate">{name}</span>
    </a>
    <button hx-post="{pin_url}"
            hx-target="#sidebar-goals"
            hx-swap="innerHTML"
            class="{pin_cls} absolute right-1 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center rounded hover:bg-surface-hover"
            title="{pin_title}">
      <svg class="w-3 h-3" fill="{pin_fill}" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z"/>
      </svg>
    </button>
  </div>''')

        # ── Sub-tree: always rendered, hidden for non-active goals ──
        tree_cls = "" if is_active else "hidden"
        loaded_attr = 'data-loaded="true"' if is_active else ""
        html.append(f'  <div id="goal-tree-{gid}" class="goal-subtree ml-5 mt-0.5 space-y-0.5 border-l border-border pl-2 {tree_cls}" {loaded_attr}>')

        if is_active:
            # Find user's SHCA and all SHCA IDs for the goal (needed to bucket shared threads)
            shca_row = await pool.fetchrow("""
                SELECT a.id, a.name FROM agents a
                JOIN agent_contexts ac ON a.id = ac.agent_id
                WHERE ac.goal_id = $1 AND a.type = 'SHCA' AND a.status = 'active'
                  AND a.owner_actor_id = (SELECT actor_id FROM users WHERE uid = $2)
                LIMIT 1
            """, gid, user["uid"])
            if not shca_row:
                shca_row = await pool.fetchrow("""
                    SELECT a.id, a.name FROM agents a
                    JOIN agent_contexts ac ON a.id = ac.agent_id
                    WHERE ac.goal_id = $1 AND a.type = 'SHCA' AND a.status = 'active'
                    LIMIT 1
                """, gid)
            shca_id = shca_row["id"] if shca_row else ""
            shca_name = shca_row["name"] if shca_row else "assistant"

            # All SHCA agent IDs for this goal — used to identify assistant threads
            all_shca_rows = await pool.fetch("""
                SELECT a.id FROM agents a
                JOIN agent_contexts ac ON a.id = ac.agent_id
                WHERE ac.goal_id = $1 AND a.type = 'SHCA' AND a.status = 'active'
            """, gid)
            shca_ids = {r["id"] for r in all_shca_rows}

            # Fetch threads early so we can split assistant vs conversation groups
            goal_threads = await pool.fetchrow(
                """SELECT go_thread_id, orchestrator_thread_id,
                          council_thread_id, security_thread_id,
                          go_agent_id
                   FROM goals WHERE id = $1""",
                gid,
            )
            go_thread = goal_threads["go_thread_id"] if goal_threads else "none"
            # The GO's own agent id, for its errand threads further down.
            go_agent = (goal_threads["go_agent_id"] or "") if goal_threads else ""
            orch_thread = goal_threads["orchestrator_thread_id"] if goal_threads else "none"
            # Excluded below alongside go_thread/orch_thread so these dedicated
            # channels (rendered as GO-thread sub-rows, not plain conversations)
            # don't also leak into the generic "Conversations" list via their
            # threads.title (see council_security_channel_threads.sql).
            council_thread = goal_threads["council_thread_id"] if goal_threads else "none"
            security_thread = goal_threads["security_thread_id"] if goal_threads else "none"

            # Find the user's SHCA thread_id so we can look up their unread count.
            # SHCA threads are per-(user, agent) — the most recent active thread wins.
            shca_thread_id = None
            if shca_id:
                # Prefer the GENERAL thread — the one with no story on it. Story
                # threads are their own rows below, and letting one of them win
                # here put a story's unread count on the assistant channel and
                # sent a click on "# assistant" into whichever story happened to
                # be newest.
                shca_thread_row = await pool.fetchrow(
                    """SELECT id FROM threads
                       WHERE agent_id = $1 AND created_by = $2 AND goal_id = $3
                         AND (status IS NULL OR status IN ('active', 'open'))
                       ORDER BY (story_id IS NULL OR story_id = '') DESC,
                                created_at DESC
                       LIMIT 1""",
                    shca_id, user["uid"], gid,
                )
                shca_thread_id = shca_thread_row["id"] if shca_thread_row else None

            threads = await pool.fetch("""
                SELECT t.id, t.title, t.agent_id, t.participants, a.name AS agent_name,
                       (t.created_by = $2) AS is_own
                FROM threads t
                LEFT JOIN agents a ON a.id = t.agent_id
                WHERE t.goal_id = $1 AND t.task_id IS NULL
                  AND (t.status IS NULL OR t.status = '' OR t.status IN ('active', 'open'))
                  AND t.id != $3 AND t.id != $4 AND t.id != $5 AND t.id != $6
                  -- GO THREADS STAY OUT OF THIS LIST. An autonomous
                  -- errand's thread belongs to the GO, and this list
                  -- links to `/chat/{agent}` -- the assistant page,
                  -- which would relay the wrong agent's history. They
                  -- render under the GO row instead, beside the three
                  -- fixed channels. See `sidebar_threads.go_errand_threads`.
                  AND (t.agent_id IS NULL OR t.agent_id = '' OR NOT t.agent_id LIKE 'go-%')
                  AND (t.created_by = $2 OR $2 = ANY(COALESCE(t.participants, '{}')))
                ORDER BY (t.created_by = $2) DESC, t.created_at DESC
                LIMIT 20
            """, gid, user["uid"], go_thread, orch_thread, council_thread, security_thread)

            # AND THE ERRAND THREADS, which are different in kind from the
            # three fixed channels under the GO. Each of those folds into
            # the GO's MAIN session and context; an errand thread comes
            # and goes with its errand and has a gateway session of its
            # own. They share a parent row because they share an agent.
            #
            # FETCHED HERE, RENDERED FURTHER DOWN, so their ids reach the
            # unread-count and preview lookups below -- a row that can
            # never show a badge cannot tell you an errand is waiting on
            # you, which is the only thing a collapsed list is for.
            from app.services.sidebar_threads import (  # noqa: PLC0415
                errand_thread_row, go_errand_threads,
            )
            try:
                _errand_rows = await go_errand_threads(pool, gid, go_agent)
            except Exception as exc:                        # noqa: BLE001
                _log.warning("sidebar: errand threads unavailable for %s: %s",
                             gid, exc)
                _errand_rows = []

            # Threads whose agent is an SHCA belong in the assistant group, not conversations
            shca_threads = [dict(t) for t in threads if t.get("agent_id") in shca_ids and not t["is_own"]]
            own_threads = [dict(t) for t in threads if t["is_own"] and t.get("agent_id") not in shca_ids]
            shared_threads = [dict(t) for t in threads if not t["is_own"] and t.get("agent_id") not in shca_ids]

            # Batch-fetch unread counts for GO + SHCA + conversation threads in one query
            # (covers every icon that renders a badge — goal-thread, assistant channel,
            # and each thread rendered via _thread_link()). Non-critical: a failure just
            # leaves all badges hidden (defaulting to zero).
            unread_counts: dict = {}
            _thread_ids_to_check = list({
                tid for tid in ([go_thread, shca_thread_id]
                                + [t["id"] for t in threads]
                                + [r["id"] for r in _errand_rows])
                if tid and tid != "none"
            })
            if _thread_ids_to_check:
                try:
                    _unread_rows = await pool.fetch(
                        """SELECT thread_id, unread_count FROM thread_participants
                           WHERE thread_id = ANY($1) AND uid = $2""",
                        _thread_ids_to_check, user["uid"],
                    )
                    unread_counts = {r["thread_id"]: r["unread_count"] for r in _unread_rows}
                    _log.debug(
                        "sidebar: unread counts uid=%s goal=%s counts=%s",
                        user["uid"][:8], gid, dict(unread_counts),
                    )
                except Exception as exc:
                    _log.warning(
                        "sidebar: unread count fetch failed (non-fatal) uid=%s goal=%s: %s",
                        user["uid"][:8], gid, exc,
                    )

            # ── Latest-message previews for hover tooltips ──────────────────
            # One DISTINCT ON query covers all thread types (GO, SHCA, conversations).
            # We resolve sender display name in Python using data we already have,
            # so no extra joins are needed here.
            #
            # Agent names keyed by thread_id so _make_preview() can label agent
            # messages without an extra DB lookup.
            _agent_name_by_thread = {t["id"]: t.get("agent_name") for t in threads}
            _agent_name_by_thread[go_thread] = "GO"
            if shca_thread_id:
                _agent_name_by_thread[shca_thread_id] = shca_name

            _all_preview_tids = [
                tid for tid in (
                    [go_thread, shca_thread_id] + [t["id"] for t in threads]
                ) if tid and tid != "none"
            ]

            # {thread_id: "SenderLabel: truncated content…"}
            previews: dict = {}
            if _all_preview_tids:
                try:
                    _preview_rows = await pool.fetch(
                        """SELECT DISTINCT ON (m.thread_id)
                               m.thread_id,
                               LEFT(m.content, 120) AS content,
                               m.sender,
                               m.role
                           FROM messages m
                           WHERE m.thread_id = ANY($1::text[])
                             AND m.role IN ('human', 'agent', 'assistant')
                           ORDER BY m.thread_id, m.created_at DESC""",
                        _all_preview_tids,
                    )
                    uid_self = user["uid"]
                    for r in _preview_rows:
                        raw = (r["content"] or "").strip().replace("\n", " ")
                        snippet = raw[:60] + ("…" if len(raw) > 60 else "")
                        if not snippet:
                            continue
                        if r["sender"] == uid_self:
                            label = "You"
                        elif r["role"] in ("agent", "assistant"):
                            label = _agent_name_by_thread.get(r["thread_id"]) or "Assistant"
                        else:
                            label = "User"
                        # Escape for safe injection into HTML attribute values.
                        previews[r["thread_id"]] = _html.escape(f"{label}: {snippet}")
                    _log.debug(
                        "sidebar: loaded %d thread previews for goal=%s", len(previews), gid,
                    )
                except Exception as exc:
                    _log.warning(
                        "sidebar: thread preview fetch failed (non-fatal) goal=%s: %s", gid, exc,
                    )

            def _thread_link(t: dict, violet: bool = False) -> str:
                tid = t["id"]
                t_agent = t.get("agent_id") or shca_id
                # For assistant (SHCA) threads use the thread title if set;
                # avoid showing the raw agent DB name (e.g. "Alan's Assistant")
                if violet:
                    title = t.get("title") or "assistant"
                else:
                    title = t.get("agent_name") or t.get("title") or "Conversation"
                participants = t.get("participants") or []
                badge = f' <span class="text-gray-600">({len(participants)})</span>' if len(participants) > 1 else ""
                is_current = tid == current_thread
                if violet:
                    link_cls = "text-violet-400 bg-violet-500/10 font-medium" if is_current else "text-gray-500 hover:text-gray-300"
                    dot_cls = "text-violet-400 flex-shrink-0"
                    icon_path = 'd="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"'
                else:
                    link_cls = "text-brand-light bg-brand/10 font-medium" if is_current else "text-gray-500 hover:text-gray-300"
                    dot_cls = ("text-brand" if is_current else "text-gray-700") + " flex-shrink-0"
                    icon_path = 'd="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"'
                display_title = ('# ' + title) if violet else title
                participants_label = f" ({len(participants)} members)" if len(participants) > 1 else ""
                tooltip_title = _html.escape(f"{display_title}{participants_label}")
                tooltip_preview = previews.get(tid, "")  # already escaped by previews builder
                return f'''
    <a href="/goals/{gid}/chat/{t_agent}?thread_id={tid}"
       data-tooltip-title="{tooltip_title}"
       data-tooltip-preview="{tooltip_preview}"
       class="sidebar-item text-2xs py-1 {link_cls}">
      <span class="thread-icon-wrap">
        <svg class="w-3 h-3 {dot_cls}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" {icon_path}/>
        </svg>
      </span>
      <span class="sidebar-label truncate">{display_title}{badge}</span>
      {_unread_badge(tid, unread_counts.get(tid, 0))}
    </a>'''

            # ── Channels (no section header) ──
            go_thread_active = "/thread" in current_path and gid in current_path
            go_thread_cls = "text-emerald-400 bg-emerald-500/10 font-medium" if go_thread_active else "text-gray-500 hover:text-gray-300"
            _go_unread = unread_counts.get(go_thread, 0) if go_thread and go_thread != "none" else 0
            _go_badge_tid = go_thread if go_thread and go_thread != "none" else ""
            _go_preview = previews.get(go_thread, "")

            # GO thread's sub-channels: Agent Mesh, Goal Review Council, Security
            # Council. Each is its own dedicated thread (goals.orchestrator_thread_id
            # / council_thread_id / security_thread_id — see
            # council_security_channel_threads.sql), rendered as a collapsible
            # child list under the GO row using the same expand-arrow convention
            # as tasks/human_board.html's toggleSubtasks().
            _errand_html = "".join(
                errand_thread_row(gid, r, current_thread, unread_counts,
                                  previews)
                for r in _errand_rows
            )
            _go_children_active = any(
                seg in current_path and gid in current_path
                for seg in ("/orchestrator", "/council", "/security")
            ) or any(r["id"] == current_thread for r in _errand_rows)
            _go_arrow_open = "rotate-90" if _go_children_active else ""
            _go_children_hidden = "" if _go_children_active else "hidden"

            def _sub_channel_link(path_segment: str, label: str, icon_color: str, icon_path: str) -> str:
                is_active = f"/{path_segment}" in current_path and gid in current_path
                link_cls = f"text-{icon_color}-400 bg-{icon_color}-500/10 font-medium" if is_active else "text-gray-500 hover:text-gray-300"
                return f'''
    <a href="/goals/{gid}/{path_segment}"
       data-tooltip-title="{_html.escape(label)}"
       class="sidebar-item text-2xs py-1 {link_cls}">
      <span class="thread-icon-wrap">
        <svg class="w-3 h-3 flex-shrink-0 text-{icon_color}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="{icon_path}"/>
        </svg>
      </span>
      <span class="sidebar-label truncate">{_html.escape(label)}</span>
    </a>'''

            html.append(f'''
    <div class="flex items-center">
      <button type="button" onclick="toggleGoChildren('{gid}')"
              title="{'Collapse' if _go_children_active else 'Expand'} goal-thread channels"
              class="thread-icon-btn row-toggle-btn flex-shrink-0 h-5 px-1 flex items-center justify-center gap-0.5 rounded hover:bg-surface-hover">
        <svg id="arrow-go-{gid}" class="w-2.5 h-2.5 flex-shrink-0 text-gray-600 transition-transform {_go_arrow_open}"
             fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
        </svg>
        <svg class="w-3 h-3 flex-shrink-0 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a2 2 0 01-2-2V6a2 2 0 012-2h6"/>
        </svg>
      </button>
      <a href="/goals/{gid}/thread"
         data-tooltip-title="# goal-thread"
         data-tooltip-preview="{_go_preview}"
         class="sidebar-item text-2xs py-1 flex-1 {go_thread_cls}">
        <svg class="rail-nav-icon w-3 h-3 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a2 2 0 01-2-2V6a2 2 0 012-2h6"/>
        </svg>
        <span class="sidebar-label"># goal-thread</span>
        {_unread_badge(_go_badge_tid, _go_unread)}
      </a>
    </div>
    <div id="go-children-{gid}" class="go-children ml-4 space-y-0.5 {_go_children_hidden}">
      {_sub_channel_link("orchestrator", "Agent Mesh", "sky", "M13 10V3L4 14h7v7l9-11h-7z")}
      {_sub_channel_link("council", "Goal Review Council", "amber", "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z")}
      {_sub_channel_link("security", "Security Council", "rose", "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z")}
      {_errand_html}
    </div>''')

            # ── Assistant (SHCA) channel link, and its story threads ──
            shca_active = (
                shca_id
                and f"/goals/{gid}/chat/{shca_id}" in current_path
            )
            shca_cls = "text-violet-400 bg-violet-500/10 font-medium" if shca_active else "text-gray-500 hover:text-gray-300"
            if shca_id:
                _shca_unread = unread_counts.get(shca_thread_id, 0) if shca_thread_id else 0
                _shca_badge_tid = shca_thread_id or ""
                _shca_preview = previews.get(shca_thread_id, "") if shca_thread_id else ""
                _shca_tooltip_title = _html.escape(f"# {shca_name}")
                from app.services.sidebar_threads import (  # noqa: PLC0415
                    assistant_story_threads, story_thread_row,
                )
                story_rows = await assistant_story_threads(
                    pool, gid, shca_id, user["uid"])
                _shca_children = "".join(
                    story_thread_row(gid, shca_id, r, current_thread,
                                     unread_counts, previews)
                    for r in story_rows
                )
                _shca_open = bool(story_rows) and (
                    shca_active or any(r["id"] == current_thread for r in story_rows)
                )
                _shca_arrow = "rotate-90" if _shca_open or story_rows else ""
                _shca_hidden = "" if _shca_open or story_rows else "hidden"
                # The toggle only exists when there is something to reveal —
                # an arrow that opens an empty list reads as a broken control.
                if story_rows:
                    html.append(f'''
    <div class="flex items-center">
      <button type="button" onclick="toggleAssistantThreads('{gid}')"
              title="Collapse/expand this assistant's story threads"
              class="thread-icon-btn row-toggle-btn flex-shrink-0 h-5 px-1 flex items-center justify-center gap-0.5 rounded hover:bg-surface-hover">
        <svg id="arrow-shca-{gid}" class="w-2.5 h-2.5 flex-shrink-0 text-gray-600 transition-transform {_shca_arrow}"
             fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
        </svg>
        <svg class="w-3 h-3 flex-shrink-0 text-violet-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/>
        </svg>
      </button>
      <a href="/goals/{gid}/chat/{shca_id}"
         data-tooltip-title="{_shca_tooltip_title}"
         data-tooltip-preview="{_shca_preview}"
         class="sidebar-item text-2xs py-1 flex-1 {shca_cls}">
        <span class="sidebar-label"># {shca_name}</span>
        {_unread_badge(_shca_badge_tid, _shca_unread)}
      </a>
    </div>
    <div id="shca-threads-{gid}" class="ml-4 space-y-0.5 {_shca_hidden}">{_shca_children}
    </div>''')
                else:
                    html.append(f'''
    <a href="/goals/{gid}/chat/{shca_id}"
       data-tooltip-title="{_shca_tooltip_title}"
       data-tooltip-preview="{_shca_preview}"
       class="sidebar-item text-2xs py-1 {shca_cls}">
      <span class="thread-icon-wrap">
        <svg class="w-3 h-3 flex-shrink-0 text-violet-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/>
        </svg>
      </span>
      <span class="sidebar-label"># {shca_name}</span>
      {_unread_badge(_shca_badge_tid, _shca_unread)}
    </a>''')
                # Shared assistant threads from other users sit in the same group
                for t in shca_threads:
                    html.append(_thread_link(t, violet=True))

            # ── Goal sub-pages: Stories, Tasks, Resources ──
            stories_active = f"/goals/{gid}/stories" in current_path
            stories_cls = "text-gray-200 bg-surface-hover font-medium" if stories_active else "text-gray-500 hover:text-gray-300"
            html.append(f'''
    <a href="/goals/{gid}/stories" data-tooltip-title="Stories" class="sidebar-item text-2xs py-1 {stories_cls}">
      <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/>
      </svg>
      <span class="sidebar-label">Stories</span>
    </a>''')

            tasks_active = f"/goals/{gid}/tasks" in current_path and "/tasks/" not in current_path.split(f"/goals/{gid}")[1]
            tasks_cls = "text-gray-200 bg-surface-hover font-medium" if tasks_active else "text-gray-500 hover:text-gray-300"
            html.append(f'''
    <a href="/goals/{gid}/tasks" data-tooltip-title="Tasks" class="sidebar-item text-2xs py-1 {tasks_cls}">
      <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
      </svg>
      <span class="sidebar-label">Tasks</span>
    </a>''')

            resources_active = f"/goals/{gid}/resources" in current_path
            resources_cls = "text-gray-200 bg-surface-hover font-medium" if resources_active else "text-gray-500 hover:text-gray-300"
            html.append(f'''
    <a href="/goals/{gid}/resources" data-tooltip-title="Resources" class="sidebar-item text-2xs py-1 {resources_cls}">
      <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>
      </svg>
      <span class="sidebar-label">Resources</span>
    </a>''')

            # ── Conversations: own non-SHCA threads + non-SHCA shared ──
            # Only render the section if there are actual non-SHCA conversations
            if own_threads or shared_threads:
                html.append('    <div class="border-t border-border opacity-30 my-1"></div>')
                for t in own_threads:
                    html.append(_thread_link(t))
                if shared_threads:
                    html.append('    <div class="flex items-center gap-1 my-1 sidebar-label"><div class="flex-1 border-t border-border opacity-30"></div><span class="text-2xs text-gray-700 px-1 flex-shrink-0">shared</span><div class="flex-1 border-t border-border opacity-30"></div></div>')
                    for t in shared_threads[:3]:
                        html.append(_thread_link(t))
                    if len(shared_threads) > 3:
                        html.append(f'    <div class="text-2xs text-gray-600 py-1 px-2 sidebar-label">+{len(shared_threads) - 3} more</div>')


        html.append('  </div>')  # close sub-tree
        html.append('</div>')  # close goal wrapper

    return HTMLResponse("\n".join(html))


# -- pin toggle ---------------------------------------------------------------

@router.post("/sidebar-data/pin/{pid}", response_class=HTMLResponse)
async def toggle_goal_pin(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
    active_goal: str = "",
    current_path: str = "",
    current_thread: str = "",
):
    """Toggle pinned state for a goal and return the refreshed sidebar HTML."""
    pool = get_pool()
    _log.info("toggle_goal_pin: user=%s goal=%s", user["uid"], pid)
    await pool.execute(
        """UPDATE goal_members
           SET pinned_at = CASE WHEN pinned_at IS NULL THEN NOW() ELSE NULL END
           WHERE goal_id = $1 AND uid = $2""",
        pid, user["uid"],
    )
    # Return the refreshed sidebar so the reorder + icon state update server-side
    return await sidebar_goals(request, user, active_goal, current_path, current_thread)


# -- list & create ------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def list_goals(request: Request, user: dict = Depends(get_current_user)):
    goals = await _get_user_goals(
        user["uid"], user.get("system_role") == SystemRole.PLATFORM_ADMIN
    )
    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "platform-overview")
    return templates.TemplateResponse(
        "goals/list.html",
        {
            "request": request, "user": user, "goals": goals,
            "auto_fire_tour": auto_fire_tour,
        },
    )


@router.get("/new-modal", response_class=HTMLResponse)
async def new_goal_modal(request: Request, user: dict = Depends(require_editor)):
    """HTMX fragment — returns just the create-goal form for the global modal."""
    from app.services.model_registry.dropdowns import (
        build_go_model_tree_v2, is_dropdown_empty,
    )
    tree = await build_go_model_tree_v2(get_pool())
    _log.debug("new_goal_modal: loaded by %s bases=%d", user.get("uid"), len(tree["bases"]))
    return templates.TemplateResponse(
        "goals/_new_goal_form.html",
        {
            "request": request,
            "user": user,
            "go_model_tree": tree,
            "models_empty": is_dropdown_empty(tree),
        },
    )


@router.post("", response_class=HTMLResponse)
async def create_goal(
    request: Request,
    user: dict = Depends(require_editor),
):
    form = await request.form()
    data = GoalCreate(
        name=form.get("name", ""),
        description=form.get("description", ""),
        objective=form.get("objective", ""),
        color_tag=form.get("color_tag", "#6366f1"),
        priority=form.get("priority", "medium"),
    )
    # Parse deadline if provided
    deadline_str = form.get("deadline", "")
    deadline = None
    if deadline_str:
        try:
            from datetime import datetime
            deadline = datetime.fromisoformat(deadline_str)
        except ValueError:
            pass

    # Validate the user-provided Goal ID (3-5 uppercase alphanumeric,
    # first char letter). This becomes the primary key AND the Slack
    # channel name, so it must be unique and lowercase-safe.
    pid = _validate_goal_id(form.get("id", ""))

    pool = get_pool()
    existing = await pool.fetchval("SELECT id FROM goals WHERE id = $1", pid)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Goal ID `{pid}` is already taken. Please pick another.",
        )

    go_thread_id = str(uuid.uuid4())
    orch_thread_id = str(uuid.uuid4())
    council_thread_id = str(uuid.uuid4())
    security_thread_id = str(uuid.uuid4())

    # Pre-generate event_id so it can serve as its own correlation_id
    root_event_id = generate_event_id()

    # Resolved before the transaction because the picker's value has to
    # be validated whatever happens next.
    from app.llm_models import is_supported, default_for
    from app.services.model_registry.dropdowns import is_model_registered_v2
    go_model = (form.get("go_model") or "").strip()
    if not is_supported(go_model) and not await is_model_registered_v2(pool, go_model):
        go_model = default_for("go")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Event first -- ledger is source of truth
            await append_event(
                conn,
                event_id=root_event_id,
                event_type=GOAL_CREATED,
                payload={
                    "goal_id": pid,
                    "name": data.name,
                    "description": data.description,
                    "objective": data.objective,
                    "priority": data.priority,
                    "color_tag": data.color_tag,
                    "created_by": user["uid"],
                },
                actor_id=user["actor_id"],
                correlation_id=str(root_event_id),
                context_goal_id=pid,
            )
            # 2. Create goal with new fields
            await conn.execute(
                """INSERT INTO goals (id, name, description, objective, deadline,
                                     priority, state, color_tag, created_by,
                                     go_thread_id, orchestrator_thread_id,
                                     council_thread_id, security_thread_id)
                   VALUES ($1, $2, $3, $4, $5, $6, 'planning', $7, $8, $9, $10, $11, $12)""",
                pid, data.name, data.description, data.objective, deadline,
                data.priority, data.color_tag, user["uid"], go_thread_id, orch_thread_id,
                council_thread_id, security_thread_id,
            )
            # 3. Creator becomes goal admin
            await append_event(
                conn,
                event_type=GOAL_MEMBER_ADDED,
                payload={
                    "goal_id": pid,
                    "uid": user["uid"],
                    "role": GoalRole.GOAL_ADMIN,
                    "added_by": user["uid"],
                },
                actor_id=user["actor_id"],
                correlation_id=str(root_event_id),
                causation_ids=[str(root_event_id)],
                context_goal_id=pid,
            )
            await conn.execute(
                """INSERT INTO goal_members (goal_id, uid, role, added_by)
                   VALUES ($1, $2, 'admin', $3)""",
                pid, user["uid"], user["uid"],
            )
            # 4. Create the goal thread (shared discussion for GO + all members).
            # agent_id is set to the GO's template id (go-<goal_id_lowercase>) so
            # the memory_v2 event bridge can resolve the goal-thread when a story
            # completion needs to dispatch a story_complete flush to the GO.
            # Previously this was '' which caused the bridge's
            # `WHERE agent_id = 'go-<goal>'` lookup to silently miss, dropping
            # every story_complete flush for the GO.
            await conn.execute(
                """INSERT INTO threads (id, goal_id, agent_id, created_by, title, status)
                   VALUES ($1, $2, $3, $4, $5, 'active')""",
                go_thread_id, pid, f"go-{pid.lower()}", user["uid"], f"Goal: {data.name}",
            )
            # 4b. Create the orchestrator channel (GO ↔ assistants, human read-only)
            await conn.execute(
                """INSERT INTO threads (id, goal_id, created_by, title, status)
                   VALUES ($1, $2, $3, 'Orchestrator Channel', 'active')""",
                orch_thread_id, pid, user["uid"],
            )
            # 4c. Create the Goal Review Council channel (human read-only)
            await conn.execute(
                """INSERT INTO threads (id, goal_id, created_by, title, status)
                   VALUES ($1, $2, $3, 'Goal Review Council', 'active')""",
                council_thread_id, pid, user["uid"],
            )
            # 4d. Create the Security Council channel (human read-only)
            await conn.execute(
                """INSERT INTO threads (id, goal_id, created_by, title, status)
                   VALUES ($1, $2, $3, 'Security Council', 'active')""",
                security_thread_id, pid, user["uid"],
            )

    # ── 5. The GO, and a failure undoes the goal ─────────────────────
    #
    # AFTER THE COMMIT AND NOT INSIDE IT, for two reasons that both had
    # to be learned the hard way. `agent_contexts` has a foreign key to
    # `goals`, so provisioning cannot run before the row exists --
    # trying it failed with `Key (goal_id)=(CATV9) is not present`. And
    # putting it inside the creating transaction would hold
    # `pg_advisory_xact_lock(GLOBAL_CHAIN_LOCK_ID)`, which
    # `append_event` takes, across six seconds of gateway I/O -- every
    # ledger append on the platform would queue behind a goal form.
    #
    # So the goal is written, provisioning runs, and a failure UNDOES
    # the write. A goal with no orchestrator can plan nothing, and
    # "created, but it may not be able to plan tasks yet" left a person
    # with no way forward: the only retry was a startup sweep they
    # could not see or trigger. Retrying is now resubmitting the form.
    try:
        go_result = await create_go_for_goal(
            goal_id=pid,
            goal_name=data.name,
            objective=data.objective,
            created_by_uid=user["uid"],
            model=go_model,
        )
        go_agent_id = (go_result or {}).get("agent_id") or ""
        if not go_agent_id:
            raise RuntimeError("provisioning returned no agent id")
        # AN AGENT ID IS NOT EVIDENCE OF AN AGENT. The dead-goal guard
        # inside returns one even when it skips, which is how a goal
        # once came out pointing at an agent that was never created.
        if (go_result or {}).get("skipped"):
            raise RuntimeError(
                f"provisioning skipped ({go_result['skipped']})")
        await pool.execute(
            "UPDATE goals SET go_agent_id = $1 WHERE id = $2", go_agent_id, pid)
        _log.info("GO agent created for goal %s: %s", pid, go_agent_id)
    except Exception as exc:                                # noqa: BLE001
        _log.exception("GO provisioning failed for goal %s — undoing it", pid)
        from app.services.goal_rollback import undo_goal_creation
        undone = await undo_goal_creation(pool, pid)
        try:
            async with pool.acquire() as _conn:
                await append_event(
                    _conn,
                    event_type=AGENT_PROVISIONING_FAILED,
                    payload={"agent_type": "GO", "goal_id": pid,
                             "error": str(exc), "goal_rolled_back": undone},
                    actor_id=SYSTEM_ACTOR_ID,
                    actor_type="system",
                    context_goal_id=pid,
                )
        except Exception as ledger_exc:                     # noqa: BLE001
            _log.warning("Failed to record GO provisioning failure for %s: %s",
                         pid, ledger_exc)
        if undone:
            msg = (f"Goal not created — the Goal Orchestrator could not be "
                   f"provisioned ({type(exc).__name__}: {exc}). Nothing was "
                   f"saved; try again once the cause is cleared.")
        else:
            # SAY THE TRUE THING. Claiming nothing was saved when the
            # rollback itself failed sends somebody to create it again
            # and hit a duplicate id they cannot see.
            msg = (f"The Goal Orchestrator could not be provisioned "
                   f"({type(exc).__name__}: {exc}) AND goal `{pid}` could "
                   f"not be rolled back — it is still in the database and "
                   f"needs an admin.")
        return flash_redirect("/goals", msg, flash_type="error")


    # 6. Auto-provision: add creator's assistants + standard IAs to the goal
    try:
        from app.assistant_provisioning import auto_provision_goal_agents
        result = await auto_provision_goal_agents(
            pid, user["uid"], user["actor_id"],
            user.get("display_name", ""),
        )
        _log.info("Auto-provisioned new goal %s: %s", pid, result)
    except Exception as exc:
        _log.warning("Auto-provision failed for new goal %s: %s", pid, exc)

    return RedirectResponse(url=f"/goals/{pid}", status_code=303)


# -- delete goal ---------------------------------------------------------------

@router.post("/{pid}/delete")
async def delete_goal(
    pid: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Soft-delete a goal. Platform-admin-only — this is the Kill Switch's
    delete-goal action (see admin_controls.py / admin/controls.html), and
    execution-control-style actions on a goal are admin-only across the
    board. platform.editor can view goal state on the Kill Switch panel but
    cannot delete a goal (goal.manager gets no bypass here either, same as
    the rest of goal deletion).

    Marks the goal as deleted. Does NOT delete tasks, threads, messages,
    or ledger events — those are immutable records.
    """
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not row:
        # Already deleted (double-click, stale tab) — just redirect.
        _log.info("delete_goal: pid=%s already deleted or missing — redirecting to /goals", pid)
        return RedirectResponse(url="/goals", status_code=303)

    await pool.execute("UPDATE goals SET status = 'deleted' WHERE id = $1", pid)

    # Cascade non-terminal work to a terminal state. Goal delete is SOFT and does
    # not otherwise touch tasks/stories, so their live state would keep the
    # state-scanning dispatch loops (task controller, phase/gate dispatchers, dag
    # janitor, branch-ready listener, council dispatcher) treating this dead goal's
    # work as live — provisioning agents, cloning repos, refreshing tokens. This is
    # the root fix; the per-loop goals.status='active' guards are the complement.
    await pool.execute(
        "UPDATE tasks SET state = 'cancelled', updated_at = now() "
        "WHERE goal_id = $1 "
        "  AND state NOT IN ('done','approved','failed','rejected','cancelled','halted')",
        pid,
    )
    # Through the ledger helper: cancelling a goal cancels its stories,
    # and each is a transition somebody later asks about ("when did this
    # stop, and why?").
    from app.services.story_state_ledger import record_states_where
    async with pool.acquire() as _c, _c.transaction():
        await record_states_where(
            _c, "cancelled",
            where=("goal_id = $1 AND state NOT IN "
                   "('done','reviewed','cancelled','rejected')"),
            params=(pid,), actor_uid=user["uid"], action="goal_cancelled")
    # Terminate pending/running Goal Review Council runs for this goal. Otherwise a
    # pending run lingers forever (the dispatcher guard skips it but never clears
    # it) and a running one holds the global single-run gate until the stale
    # reaper. NB: council_review_runs.status CHECK allows only
    # pending|running|done|failed — use 'failed' (there is no 'cancelled').
    await pool.execute(
        "UPDATE council_review_runs SET status = 'failed', finished_at = now() "
        "WHERE goal_id = $1 AND status IN ('pending','running')",
        pid,
    )

    # Clean up any active execution pauses so a future goal with the same id
    # doesn't inherit stale pause state (M15).
    await pool.execute(
        "DELETE FROM execution_pauses WHERE scope = 'goal' AND scope_id = $1", pid,
    )

    # AND THE RUNS IN FLIGHT. Cancelling the STORY is not a signal the
    # reclaim sweep recognises -- it frees a workspace on `done`,
    # `reviewed` or a CANCELLED execution -- so a deleted goal's pod
    # kept running and its claim kept its disk, against a namespace
    # quota of 40 claims for all executions ever.
    try:
        from app.services.kernel_goal_teardown import cancel_goal_runs

        await cancel_goal_runs(pid, user["uid"])
    except Exception as exc:                                # noqa: BLE001
        _log.warning("Goal-delete run cancellation failed for %s: %s",
                     pid, exc)

    # Notify connected channels before archiving bindings — relay functions need
    # active bindings to resolve the target pages/channels.
    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(pid, "goal.deleted", {"goal_name": row["name"]})
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for goal delete %s: %s", pid, exc)

    # Archive all platform channel bindings and disable integrations for this goal
    await pool.execute(
        "UPDATE platform_channel_bindings SET archived_at = now() WHERE goal_id = $1 AND archived_at IS NULL",
        pid,
    )
    await pool.execute(
        "UPDATE goal_platform_integrations SET enabled = false WHERE goal_id = $1",
        pid,
    )

    # Log to ledger
    await append_event(
        pool,
        event_type=GOAL_COMPLETED,
        payload={"goal_id": pid, "reason": f"Deleted by {user.get('email', user['uid'])}"},
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    # Destroy all gateway agents owned by this goal as a background task so
    # the redirect is not blocked. teardown_goal_agents covers: GO session
    # eviction, GO + SHCA archive-and-delete, IA/RA plain-delete,
    # agent_instances rows, agent_contexts, and orphaned agents-table rows.
    try:
        import asyncio as _asyncio
        from app.services.agent_registry.teardown import teardown_goal_agents
        _asyncio.create_task(teardown_goal_agents(pool, pid))
    except Exception as exc:
        _log.warning("Goal-delete teardown failed to schedule for goal %s: %s", pid, exc)

    _log.info("Goal %s deleted by %s", pid, user.get("email", user["uid"]))
    return RedirectResponse(url="/goals", status_code=303)


# -- goal dashboard ------------------------------------------------------------

@router.get("/{pid}", response_class=HTMLResponse)
async def goal_dashboard(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    pool = get_pool()
    # Look up the goal first — if it's missing or already soft-deleted, send
    # the user back to the goals list rather than serving a 404 page (which
    # is what they'd otherwise see right after clicking Delete Goal).
    row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    if not row or row.get("status") != "active":
        _log.info(
            "goal_dashboard: pid=%s missing-or-deleted (row=%s status=%s) — redirecting to /goals",
            pid, "yes" if row else "no",
            (row or {}).get("status"),
        )
        return RedirectResponse(url="/goals", status_code=303)

    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        _log.warning("goal_dashboard: denied uid=%s not-a-member goal=%s", user["uid"], pid)
        raise HTTPException(status_code=403, detail="Not a goal member")

    goal = dict(row)

    task_rows = await pool.fetch(
        "SELECT * FROM tasks "
        "WHERE goal_id = $1 AND status = 'active' "
        "AND review_target_id IS NULL "
        "ORDER BY created_at DESC LIMIT 10",
        pid,
    )
    tasks = [dict(r) for r in task_rows]

    member_rows = await pool.fetch(
        """SELECT gm.uid, gm.role, u.display_name, u.email
           FROM goal_members gm
           JOIN users u ON u.uid = gm.uid
           WHERE gm.goal_id = $1
           ORDER BY gm.role DESC, u.display_name
           LIMIT 12""",
        pid,
    )
    members = [dict(r) for r in member_rows]

    agent_rows = await pool.fetch(
        """SELECT a.id, a.name, a.type, a.emoji, a.model, a.scope
           FROM goal_agent_memberships gam
           JOIN agents a ON a.id = gam.agent_id
           WHERE gam.goal_id = $1 AND gam.status = 'active'
           ORDER BY a.type, a.name
           LIMIT 8""",
        pid,
    )
    agents = [dict(r) for r in agent_rows]

    # Threads shared with the user (created by others, user is a participant)
    shared_thread_rows = await pool.fetch(
        """SELECT t.id, t.title, t.created_by, t.created_at,
                  a.name AS agent_name, u.display_name AS owner_name
           FROM threads t
           LEFT JOIN agents a ON a.id = t.agent_id
           LEFT JOIN users u ON u.uid = t.created_by
           WHERE t.goal_id = $1
             AND t.status = 'active'
             AND t.created_by != $2
             AND t.participants @> ARRAY[$2]::text[]
           ORDER BY t.created_at DESC
           LIMIT 5""",
        pid, user["uid"],
    )
    shared_threads = [dict(r) for r in shared_thread_rows]

    # Former members' conversations — read-only, visible to any current member
    # (dynamic, not a fixed snapshot of who was present at removal time — see
    # threads_left_goal_at migration / remove_goal_member). Distinct from
    # shared_threads above: that's an opt-in, per-participant grant for active
    # collaboration; this is what happens automatically when someone leaves.
    left_goal_thread_rows = await pool.fetch(
        """SELECT t.id, t.agent_id, t.title, t.created_by, t.left_goal_at,
                  u.display_name AS owner_name
           FROM threads t
           LEFT JOIN users u ON u.uid = t.created_by
           WHERE t.goal_id = $1 AND t.left_goal_at IS NOT NULL
           ORDER BY t.left_goal_at DESC
           LIMIT 10""",
        pid,
    )
    left_goal_threads = [dict(r) for r in left_goal_thread_rows]

    # Recent ledger activity relevant to this goal (5 most recent)
    activity_rows = await pool.fetch(
        """SELECT event_type, timestamp, actor_type, payload
           FROM ledger_events
           WHERE context_goal_id = $1
             AND (event_type LIKE 'task.%' OR event_type LIKE 'story.%'
                  OR event_type LIKE 'goal.%' OR event_type LIKE 'approval.%'
                  OR event_type LIKE 'execution.%')
           ORDER BY timestamp DESC
           LIMIT 5""",
        pid,
    )
    activity = [dict(r) for r in activity_rows]

    _log.info(
        "goal_dashboard: pid=%s tasks=%d members=%d agents=%d shared_threads=%d left_goal_threads=%d activity=%d",
        pid, len(tasks), len(members), len(agents), len(shared_threads), len(left_goal_threads), len(activity),
    )

    from app.config import get_settings
    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "goal-dashboard-intro")
    return templates.TemplateResponse(
        "goals/detail.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "role": role or GoalRole.GOAL_ADMIN,
            "tasks": tasks,
            "members": members,
            "agents": agents,
            "shared_threads": shared_threads,
            "left_goal_threads": left_goal_threads,
            "activity": activity,
            "auto_fire_tour": auto_fire_tour,
            "gitgraph_enabled": get_settings().gitgraph_enabled,
        },
    )


# -- members -------------------------------------------------------------------

@router.post("/{pid}/members")
async def add_member(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # No platform.admin bypass here — membership management (add/invite,
    # role change, search) is goal.manager-only, full stop. A platform.admin
    # who wants to manage this goal's membership must actually be a member.
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("add_member: denied uid=%s role=%s goal=%s", user["uid"], role, pid)
        raise HTTPException(status_code=403, detail="Goal admin required")

    form = await request.form()
    email = form.get("email", "").strip()
    target_uid_form = form.get("target_uid", "").strip()
    member_role = form.get("role", GoalRole.GOAL_MEMBER)

    pool = get_pool()

    if target_uid_form:
        # Direct UID (from search dropdown)
        target_uid = target_uid_form
    elif email:
        # Search by email
        target_uid = await pool.fetchval(
            "SELECT uid FROM users WHERE email = $1", email,
        )
    else:
        raise HTTPException(status_code=400, detail="Email or user selection required")

    if not target_uid:
        # 404 would be misleading here — the route resolved fine, the email
        # just didn't match a user. Using 400 keeps this on the Accept-aware
        # bad_request_handler branch (main.py) so the web form's fetch+toast
        # handler gets the real message; main.py's 404 handler always
        # renders error.html regardless of Accept.
        raise HTTPException(status_code=400, detail=f"No user found with email '{email}'.")

    # Membership + full provisioning chain lives in the shared service so the
    # Slack /goal invite command behaves identically. See goal_membership.py.
    from app.services.goal_membership import add_goal_member
    ok, message = await add_goal_member(
        pid, target_uid, member_role, user["uid"], user["actor_id"],
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    return flash_redirect(f"/goals/{pid}/members", message)


@router.post("/{pid}/members/{uid}/role")
async def update_member_role(
    pid: str,
    uid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Change a member's goal role. No platform.admin bypass — membership
    management is goal.manager-only (see add_member for rationale).
    Last-admin guard: refuses to demote the final goal.manager."""
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("update_member_role: denied uid=%s role=%s goal=%s target=%s", user["uid"], role, pid, uid)
        raise HTTPException(status_code=403, detail="Goal admin required")

    form = await request.form()
    new_role = (form.get("role") or "").strip()
    if new_role not in GoalRole.ALL:
        raise HTTPException(status_code=400, detail="Invalid role value")

    pool = get_pool()
    current_row = await pool.fetchrow(
        "SELECT role FROM goal_members WHERE goal_id = $1 AND uid = $2", pid, uid,
    )
    if not current_row:
        raise HTTPException(status_code=404, detail="Member not found")

    current_role = current_row["role"]

    # Last-admin guard: block demotion of the sole remaining goal.manager.
    # Shared with add_goal_member / remove_goal_member (goal_membership.py)
    # so the rule can't drift between the three membership-mutation paths.
    if current_role == GoalRole.GOAL_ADMIN and new_role != GoalRole.GOAL_ADMIN:
        from app.services.goal_membership import count_goal_admins
        if await count_goal_admins(pool, pid) <= 1:
            _log.warning(
                "update_member_role: blocked last-admin demotion pid=%s uid=%s by %s",
                pid, uid, user["uid"],
            )
            raise HTTPException(
                status_code=400,
                detail="Cannot demote: this member is the only goal.manager. Promote another member first.",
            )

    if current_role == new_role:
        return RedirectResponse(url=f"/goals/{pid}/members", status_code=303)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE goal_members SET role = $1 WHERE goal_id = $2 AND uid = $3",
                new_role, pid, uid,
            )
            await append_event(
                conn,
                event_type=GOAL_MEMBER_ROLE_CHANGED,
                payload={
                    "goal_id": pid,
                    "uid": uid,
                    "previous_role": current_role,
                    "new_role": new_role,
                    "changed_by": user["uid"],
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )

    _log.info(
        "update_member_role: pid=%s uid=%s %s->%s by %s",
        pid, uid, current_role, new_role, user["uid"],
    )
    return RedirectResponse(url=f"/goals/{pid}/members", status_code=303)


@router.delete("/{pid}/members/{uid}")
async def remove_member(
    pid: str,
    uid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Remove a member from the goal. No platform.admin bypass (see add_member).

    Removal can come back needing a reassignment decision (409) rather than a
    flat success/failure — see remove_goal_member for the full precondition
    chain (active tasks/stories block outright, not-yet-started stories can be
    handed to someone else via the `reassignments` body).
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("remove_member: denied uid=%s role=%s goal=%s target=%s", user["uid"], role, pid, uid)
        raise HTTPException(status_code=403, detail="Goal admin required")

    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    reassignments = body.get("reassignments") or {}

    from app.services.goal_membership import remove_goal_member
    result = await remove_goal_member(pid, uid, user["uid"], user["actor_id"], reassignments)
    if result.get("status") == "needs_reassignment":
        return JSONResponse(result, status_code=409)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return JSONResponse(result)


# -- member search (for add-member dropdown) -----------------------------------

@router.get("/{pid}/members/search")
async def search_members(
    pid: str,
    q: str = "",
    user: dict = Depends(get_current_user),
):
    """Search users by name or email for adding to a goal.

    Returns users NOT already in this goal, matching the query.
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("search_members: denied uid=%s role=%s goal=%s", user["uid"], role, pid)
        raise HTTPException(status_code=403)

    if len(q) < 2:
        return JSONResponse({"results": []})

    pool = get_pool()
    rows = await pool.fetch("""
        SELECT uid, display_name, email FROM users
        WHERE status = 'active'
          AND uid NOT IN (SELECT uid FROM goal_members WHERE goal_id = $1)
          AND (display_name ILIKE $2 OR email ILIKE $2)
        ORDER BY display_name
        LIMIT 10
    """, pid, f"%{q}%")

    return JSONResponse({
        "results": [
            {"uid": r["uid"], "name": r["display_name"] or r["email"], "email": r["email"]}
            for r in rows
        ],
    })


# -- join requests -------------------------------------------------------------

@router.post("/{pid}/join-requests")
async def request_to_join(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    form = await request.form()
    message = form.get("message", "")
    pool = get_pool()
    await pool.execute(
        """INSERT INTO join_requests (goal_id, uid, message)
           VALUES ($1, $2, $3)
           ON CONFLICT (goal_id, uid) DO NOTHING""",
        pid, user["uid"], message,
    )
    return RedirectResponse(url="/goals", status_code=303)


@router.post("/{pid}/join-requests/{uid}/approve")
async def approve_join_request(
    pid: str,
    uid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """No platform.admin bypass — membership management is goal.manager-only
    (see add_member for rationale)."""
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("approve_join_request: denied uid=%s role=%s goal=%s target=%s", user["uid"], role, pid, uid)
        raise HTTPException(status_code=403)

    form = await request.form()
    action = form.get("action", "approve")
    new_status = "approved" if action == "approve" else "rejected"

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE join_requests SET status = $1 WHERE goal_id = $2 AND uid = $3",
                new_status, pid, uid,
            )
            if new_status == "approved":
                await append_event(
                    conn,
                    event_type=GOAL_MEMBER_ADDED,
                    payload={
                        "goal_id": pid,
                        "uid": uid,
                        "role": GoalRole.GOAL_MEMBER,
                        "added_by": user["uid"],
                        "via": "join_request",
                    },
                    actor_id=user["actor_id"],
                    context_goal_id=pid,
                )
                await conn.execute(
                    """INSERT INTO goal_members (goal_id, uid, role, added_by)
                       VALUES ($1, $2, 'member', $3)
                       ON CONFLICT (goal_id, uid) DO NOTHING""",
                    pid, uid, user["uid"],
                )

    # Notify GO agent of approved join
    if new_status == "approved":
        try:
            from app.services.go_events import on_member_added
            await on_member_added(pid, uid, GoalRole.GOAL_MEMBER, user["uid"])
        except Exception as exc:
            _log.warning("GO event notification failed for join approval in %s: %s", pid, exc)

    return RedirectResponse(url=f"/goals/{pid}/members", status_code=303)


# -- members page ---------------------------------------------------------------

@router.get("/{pid}/members", response_class=HTMLResponse)
async def goal_members_page(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Viewing the roster is a member+ action (see the "members see everything,
    # managers edit" policy in research/GOAL_ROLE_OPERATIONS.md) — only the
    # add-member/role-change/join-request controls are manager-gated, and the
    # template hides those behind can_manage (viewer_role == admin).
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("goal_members_page: denied uid=%s not-a-member goal=%s", user["uid"], pid)
        raise HTTPException(status_code=403)

    pool = get_pool()
    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    member_rows = await pool.fetch(
        """SELECT pm.*, u.display_name, u.email
           FROM goal_members pm
           JOIN users u ON u.uid = pm.uid
           WHERE pm.goal_id = $1""",
        pid,
    )
    members = [dict(r) for r in member_rows]

    # Join requests are manager-only (template hides the section for plain
    # members), so only fetch them for a viewer who can actually act on them.
    join_requests = []
    if role == GoalRole.GOAL_ADMIN:
        jr_rows = await pool.fetch(
            """SELECT jr.uid, jr.message, jr.requested_at, u.display_name, u.email
               FROM join_requests jr
               JOIN users u ON u.uid = jr.uid
               WHERE jr.goal_id = $1 AND jr.status = 'pending'
               ORDER BY jr.requested_at""",
            pid,
        )
        join_requests = [dict(r) for r in jr_rows]

    _log.info(
        "goal_members_page: pid=%s members=%d join_requests=%d",
        pid, len(members), len(join_requests),
    )
    return templates.TemplateResponse(
        "goals/members.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "members": members,
            "join_requests": join_requests,
            "viewer_role": role,
            "error": request.query_params.get("error"),
        },
    )


# -- integrations page ----------------------------------------------------------

@router.get("/{pid}/integrations", response_class=HTMLResponse)
async def goal_integrations_page(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("goal_integrations_page: denied uid=%s not-a-member goal=%s", user["uid"], pid)
        raise HTTPException(status_code=403)

    pool = get_pool()
    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    # Slack config — same logic as settings page
    slack_config = None
    slack_workspaces = []
    _new_system_row = False
    try:
        from app.channels.slack.config import get_slack_config_for_goal, list_workspaces as _slack_list_ws
        slack_config = await get_slack_config_for_goal(pid, pool)
        slack_workspaces = await _slack_list_ws(pool)
        _new_system_row = bool(await pool.fetchval(
            """SELECT 1 FROM goal_platform_integrations gpi
               JOIN platform_integrations pi ON pi.id = gpi.integration_id
               WHERE gpi.goal_id = $1 AND pi.platform = 'slack' LIMIT 1""",
            pid,
        ))
    except Exception as _slack_exc:
        _log.warning("Failed to load Slack config for goal %s: %s", pid, _slack_exc, exc_info=True)
    import json as _json
    from app.channels.registry import registered_platforms, get_provisioner

    channels = []
    for _platform in registered_platforms():
        _integration = await pool.fetchrow(
            "SELECT id, config FROM platform_integrations WHERE platform = $1 AND status = 'active'",
            _platform,
        )
        if not _integration:
            channels.append({"platform": _platform, "available": False})
            continue

        _raw_cfg = _integration.get("config") or {}
        _cfg = _json.loads(_raw_cfg) if isinstance(_raw_cfg, str) else (_raw_cfg or {})
        _provisioner = get_provisioner(_platform)
        _has_auto = _provisioner.is_auto_provisioned(_cfg) if _provisioner else False

        _gpi = await pool.fetchrow(
            """SELECT enabled, email_domain_allowlist
               FROM goal_platform_integrations
               WHERE goal_id = $1 AND integration_id = $2""",
            pid, _integration["id"],
        )
        _enabled = bool(_gpi and _gpi["enabled"])
        _bindings = []
        if _gpi:
            _bindings = [
                dict(r) for r in await pool.fetch(
                    """SELECT pcb.id, pcb.external_channel_id, pcb.resource_kind, pcb.actor_id
                       FROM platform_channel_bindings pcb
                       WHERE pcb.goal_id = $1 AND pcb.integration_id = $2
                         AND pcb.archived_at IS NULL
                       ORDER BY pcb.created_at""",
                    pid, _integration["id"],
                )
            ]

        channels.append({
            "platform": _platform,
            "available": True,
            "enabled": _enabled,
            "email_domain_allowlist": (_gpi["email_domain_allowlist"] if _gpi else None) or [],
            "bindings": _bindings,
            "has_auto_provision": _has_auto,
        })

    _notion_ch = next((c for c in channels if c["platform"] == "notion"), None)
    _notion_bot_ch = next((c for c in channels if c["platform"] == "notion_bot"), None)
    if (_notion_bot_ch and _notion_bot_ch.get("available")
            and not _notion_bot_ch.get("has_auto_provision")
            and _notion_ch and _notion_ch.get("has_auto_provision")):
        _notion_bot_ch["has_auto_provision"] = True

    _log.info("goal_integrations_page: pid=%s channels=%d", pid, len(channels))
    return templates.TemplateResponse(
        "goals/integrations.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "role": role,
            "slack_config": slack_config,
            "slack_workspaces": slack_workspaces,
            "channels": channels,
        },
    )


# -- model config page ----------------------------------------------------------

@router.get("/{pid}/model-config", response_class=HTMLResponse)
async def goal_model_config_page(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Viewing is member+; only the change-model form is manager-gated (see
    # goal_members_page and research/GOAL_ROLE_OPERATIONS.md for the same
    # view-vs-edit split applied elsewhere). No platform.admin bypass here —
    # this stays a "participation" route per ROLE_SYSTEM.md §4: a platform
    # admin who isn't a goal member still can't view/change it.
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        _log.warning("goal_model_config_page: denied uid=%s not-a-member goal=%s", user["uid"], pid)
        raise HTTPException(status_code=403)

    pool = get_pool()
    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    from app.llm_models import label_for
    from app.services.model_registry.dropdowns import (
        build_go_model_tree_v2, is_dropdown_empty,
    )
    go_agent_id = goal.get("go_agent_id") or ""
    go_model = ""
    if go_agent_id:
        go_model = await pool.fetchval(
            "SELECT model FROM agents WHERE id = $1", go_agent_id,
        ) or ""

    tree = await build_go_model_tree_v2(pool)
    _log.info("goal_model_config_page: pid=%s go_model=%s bases=%d", pid, go_model, len(tree["bases"]))
    return templates.TemplateResponse(
        "goals/model_config.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "role": role,
            "go_model_tree": tree,
            "models_empty": is_dropdown_empty(tree),
            "go_model": go_model,
            "go_model_label": label_for(go_model) if go_model else "—",
            # THE KERNEL's models, not the gateway's. The GO runs on the
            # gateway and its list comes from the `models` TABLE; the
            # executor and verifier run in the kernel, whose adapters
            # serve a different, smaller set. Offering the wrong one
            # would let somebody pick a model `choice.validate` then
            # refuses -- a form rejecting its own options.
            "kernel_models": _kernel_model_menu(),
            "executor_model": goal.get("executor_model") or "",
            "sandbox_size": goal.get("sandbox_size") or "",
            "verifier_model": goal.get("verifier_model") or "",
            # The goal's executor leg size and count; blank = defaults.
            "leg_context_tokens": goal.get("leg_context_tokens") or "",
            "max_legs": goal.get("max_legs") or "",
        },
    )


# -- GO model change ------------------------------------------------------------

@router.post("/{pid}/settings/go-model")
async def change_go_model(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Change the LLM the GO agent uses for this goal."""
    # Platform admin counts here too, for the reason spelled out at
    # `change_kernel_models`: the same omission, the same setting class.
    role = await get_goal_role(user["uid"], pid)
    is_platform_admin = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    if role != GoalRole.GOAL_ADMIN and not is_platform_admin:
        _log.warning("change_go_model: denied uid=%s role=%s goal=%s", user["uid"], role, pid)
        raise HTTPException(
            status_code=403, detail="Goal admin or platform admin required")

    pool = get_pool()
    pid = await resolve_goal_id(pid)

    goal = await pool.fetchrow(
        "SELECT go_agent_id FROM goals WHERE id = $1 AND status = 'active'", pid,
    )
    if not goal or not goal.get("go_agent_id"):
        raise HTTPException(status_code=404, detail="Goal has no GO agent")

    form = await request.form()
    new_model = (form.get("model") or "").strip()
    force = bool(form.get("force"))

    # No context-window cap here — removed GO_CREATE_MAX_CONTEXT
    # restriction; any registered model/size is a legitimate, explicit
    # choice now that the registry's context-window variant system means
    # picking a larger window is deliberate, not something to guard
    # against.
    from app.services.model_change import change_agent_model
    result = await change_agent_model(
        agent_id=goal["go_agent_id"],
        new_model=new_model,
        actor_id=user["actor_id"],
        actor_uid=user["uid"],
        force=force,
    )
    if not result.ok:
        # 409 for the busy-gate case so the UI can show a clean retry prompt;
        # 400 otherwise.
        status = 409 if result.was_busy else 400
        raise HTTPException(status_code=status, detail=result.error)

    return RedirectResponse(url=f"/goals/{pid}/model-config", status_code=303)


# -- Slack enable/disable (LEGACY — commented out 2026-06-18) -------------------
# The legacy slack_config endpoints lived here. app/slack/ was deleted on
# 2026-10-16; goal-level Slack is goal_platform_integrations.enabled,
# handled by the channel routes above.


# -- Notion enable/disable/allowlist -------------------------------------------

def _parse_domains(raw: str) -> list[str]:
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


# ── generic channel endpoints (works for any registered platform) ──────────────

# ── Executor and verifier models (kernel-side), per goal ───────────────
#
# A DIFFERENT REGISTRY FROM THE GO's, and the distinction is the whole
# reason these are separate handlers rather than more fields on the one
# above. The GO runs on the GATEWAY and its models come from the `models`
# TABLE (`model_registry.dropdowns`); the executor and verifier run in
# the KERNEL, whose adapters serve `kernel.harness.models.registry.
# SUPPORTED` -- a different, smaller, hardcoded set.
#
# Offering the gateway's list here would let somebody pick a model the
# kernel cannot serve, and `choice.validate` would then refuse it: a form
# that rejects its own options.


def _kernel_model_menu() -> list:
    """The models a person may be OFFERED, grouped by vendor.

    Delegates to `choice.menu`, which is NARROWER than what the adapters
    can serve: Gemini is curated to the 3.8 series, because offering
    nine -- four of them previews -- is a menu nobody can choose from
    well. `validate` still accepts anything the registry serves, so a
    goal configured before the curation keeps working.

    One function for both the goal page and the story page: two menus
    would drift, and the drift is a story offered a model its goal is
    not.
    """
    from kernel.harness.models.choice import menu

    return menu()


@router.post("/{pid}/settings/kernel-models")
async def change_kernel_models(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Set which model runs this goal's work and which model judges it.

    GOAL_ADMIN, the same gate `change_go_model` uses: which model judges
    is a standard, and a standard anybody could lower is not one.
    """
    # OR A PLATFORM ADMIN, which this gate left out.
    #
    # `get_goal_role` answers from `goal_members` for an ordinary goal
    # and special-cases only `__platform__`, so a platform admin who is
    # not a member of THIS goal comes back None and was refused -- the
    # one role that administers the installation could not set the
    # models its executions run on. The same route file already spells
    # the correct test at `remove_goal`; this is that test, not a new
    # rule.
    #
    # Deliberately NOT fixed inside `get_goal_role`: that function backs
    # every access check in the app -- chat, threads, the WS -- and
    # making platform admins goal admins everywhere would widen read
    # access to every goal's conversations as a side effect of a models
    # dropdown.
    role = await get_goal_role(user["uid"], pid)
    is_platform_admin = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    if role != GoalRole.GOAL_ADMIN and not is_platform_admin:
        _log.warning("change_kernel_models: denied uid=%s role=%s goal=%s",
                     user["uid"], role, pid)
        raise HTTPException(
            status_code=403, detail="Goal admin or platform admin required")

    from kernel.harness.models.choice import UnknownModel, validate
    from kernel.conductor.size_choice import valid as valid_size

    form = await request.form()
    try:
        # BLANK MEANS INHERIT, not "the empty string". A cleared
        # dropdown is a person saying "use the default", and storing ""
        # would send an empty model id to an adapter.
        executor = validate((form.get("executor_model") or "").strip() or None)
        verifier = validate((form.get("verifier_model") or "").strip() or None)
    except UnknownModel as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # THE GOAL'S MACHINE, and without this an ERRAND could never be
    # anything but `small`. A story can be re-sized on its own page
    # before it runs; an errand's story is created AT APPROVAL and
    # dispatched immediately, so there is no window in which to edit it
    # -- the goal is the only place its rung can come from. The column
    # existed and nothing wrote it, which is the same defect one level
    # up that `contracts.size` was.
    try:
        sandbox = valid_size((form.get("sandbox_size") or "").strip() or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # THE EXECUTOR'S LEG SIZE AND LEG COUNT, validated by the same module
    # the pod applies them with, so the form cannot store a value the
    # run would then refuse.
    from kernel.harness.models.leg_choice import valid as valid_legs
    try:
        leg_tokens, leg_count = valid_legs(form.get("leg_context_tokens"),
                                           form.get("max_legs"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await get_pool().execute(
        "UPDATE goals SET executor_model = $2, verifier_model = $3, "
        "                 sandbox_size = $4, leg_context_tokens = $5, "
        "                 max_legs = $6 "
        "WHERE id = $1", pid, executor, verifier, sandbox,
        leg_tokens, leg_count)
    _log.info("change_kernel_models: goal=%s executor=%s verifier=%s size=%s "
              "leg_tokens=%s max_legs=%s",
              pid, executor or "(inherit)", verifier or "(inherit)",
              sandbox or "(inherit)", leg_tokens or "(default)",
              leg_count or "(default)")
    return RedirectResponse(url=f"/goals/{pid}/model-config", status_code=303)

@router.post("/{pid}/settings/channels/{platform}/enable")
async def enable_channel(
    pid: str,
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    from app.channels.registry import get_provisioner

    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("enable_channel: denied uid=%s role=%s goal=%s platform=%s", user["uid"], role, pid, platform)
        raise HTTPException(status_code=403)

    pool = get_pool()
    integration = await pool.fetchrow(
        "SELECT id FROM platform_integrations WHERE platform = $1 AND status = 'active'",
        platform,
    )
    if not integration:
        raise HTTPException(status_code=400, detail=f"No active {platform} integration configured")

    form = await request.form()
    domains = _parse_domains(form.get("domains", ""))

    await pool.execute(
        """INSERT INTO goal_platform_integrations
               (goal_id, integration_id, enabled, email_domain_allowlist)
           VALUES ($1, $2, true, $3)
           ON CONFLICT (goal_id, integration_id) DO UPDATE
               SET enabled = true, email_domain_allowlist = EXCLUDED.email_domain_allowlist,
                   updated_at = now()""",
        pid, integration["id"], domains,
    )

    # Always call provision_goal — the provisioner owns idempotency.
    # It checks for an existing GO binding internally and either creates the full
    # page structure (first enable) or refreshes member subpage bindings (re-enable).
    provisioner = get_provisioner(platform)
    if provisioner:
        error = await provisioner.provision_goal(pid, integration["id"], pool)
        if error:
            raise HTTPException(status_code=400, detail=error)

    _log.info("%s enabled for goal %s", platform, pid)

    # BACK-FILL THE THREADS THAT ALREADY EXIST. Provisioning creates the
    # channels that exist at this moment; under one channel per thread a
    # goal switched on after twenty stories needs twenty of them, and
    # nothing used to make them -- so every conversation already open
    # stayed invisible while new ones appeared. Backgrounded because it
    # is paced by design (conversations.create is Tier-2) and the toggle
    # should return at once.
    if platform == "slack":
        import asyncio as _asyncio

        async def _backfill() -> None:
            try:
                from app.services.slack_backfill import backfill_goal
                await backfill_goal(get_pool(), pid)
            except Exception as exc:                           # noqa: BLE001
                _log.warning("Slack backfill for goal %s failed: %s", pid, exc)

        _task = _asyncio.create_task(_backfill())
        _task.add_done_callback(
            lambda t: t.exception() and _log.warning(
                "Slack backfill task failed: %s", t.exception()))

    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/channels/{platform}/reprovision-member")
async def reprovision_member_channel(
    pid: str,
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Re-run provision_member for the requesting user on this goal.

    Used when the user's SHCA subpage was never created (e.g. they were already
    a member when Notion was first enabled) or after a stale-binding cleanup.
    """
    role = await get_goal_role(user["uid"], pid)
    if not role and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("reprovision_member_channel: denied uid=%s not-a-member goal=%s platform=%s", user["uid"], pid, platform)
        raise HTTPException(status_code=403)

    from app.channels.registry import get_provisioner
    provisioner = get_provisioner(platform)
    if not provisioner:
        raise HTTPException(status_code=400, detail=f"No provisioner registered for {platform}")

    pool = get_pool()
    try:
        await provisioner.provision_member(pid, user["uid"], pool)
        _log.info("%s member re-provisioned: goal=%s uid=%s", platform, pid, user["uid"])
    except Exception as exc:
        _log.warning("reprovision_member failed: goal=%s uid=%s platform=%s: %s", pid, user["uid"], platform, exc)
        raise HTTPException(status_code=400, detail="Re-provisioning failed. Check server logs for details.")

    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/channels/{platform}/disable")
async def disable_channel(
    pid: str,
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("disable_channel: denied uid=%s role=%s goal=%s platform=%s", user["uid"], role, pid, platform)
        raise HTTPException(status_code=403)

    await get_pool().execute(
        """UPDATE goal_platform_integrations gpi
           SET enabled = false, updated_at = now()
           FROM platform_integrations pi
           WHERE pi.id = gpi.integration_id
             AND pi.platform = $1
             AND gpi.goal_id = $2""",
        platform, pid,
    )
    _log.info("%s disabled for goal %s", platform, pid)
    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/channels/{platform}/allowlist")
async def update_channel_allowlist(
    pid: str,
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("update_channel_allowlist: denied uid=%s role=%s goal=%s platform=%s", user["uid"], role, pid, platform)
        raise HTTPException(status_code=403)

    form = await request.form()
    domains = _parse_domains(form.get("domains", ""))

    await get_pool().execute(
        """UPDATE goal_platform_integrations gpi
           SET email_domain_allowlist = $2, updated_at = now()
           FROM platform_integrations pi
           WHERE pi.id = gpi.integration_id
             AND pi.platform = $1
             AND gpi.goal_id = $3
             AND gpi.enabled = true""",
        platform, domains, pid,
    )
    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/channels/{platform}/bindings")
async def add_channel_binding(
    pid: str,
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    from app.channels.registry import get_provisioner

    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("add_channel_binding: denied uid=%s role=%s goal=%s platform=%s", user["uid"], role, pid, platform)
        raise HTTPException(status_code=403)

    form = await request.form()
    raw = form.get("page_url", "").strip()
    label = form.get("label", "").strip()

    provisioner = get_provisioner(platform)
    channel_id = provisioner.parse_channel_id(raw) if provisioner else raw.strip()
    if not channel_id:
        raise HTTPException(status_code=400, detail=f"Could not parse a valid channel ID for {platform}")

    pool = get_pool()
    goal = await pool.fetchrow("SELECT go_thread_id FROM goals WHERE id = $1", pid)
    if not goal or not goal["go_thread_id"]:
        raise HTTPException(status_code=400, detail="Goal has no GO thread")

    integration = await pool.fetchrow(
        "SELECT id FROM platform_integrations WHERE platform = $1 AND status = 'active'",
        platform,
    )
    if not integration:
        raise HTTPException(status_code=400, detail=f"No active {platform} integration")

    await pool.execute(
        """INSERT INTO platform_channel_bindings
               (goal_id, integration_id, external_channel_id, thread_id, resource_kind)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (integration_id, external_channel_id) DO NOTHING""",
        pid, integration["id"], channel_id, goal["go_thread_id"], label or None,
    )
    _log.info("%s binding added: goal=%s channel=%s", platform, pid, channel_id)
    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/channels/{platform}/bindings/{binding_id}/remove")
async def remove_channel_binding(
    pid: str,
    platform: str,
    binding_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and user.get("system_role") != SystemRole.PLATFORM_ADMIN:
        _log.warning("remove_channel_binding: denied uid=%s role=%s goal=%s platform=%s binding=%s", user["uid"], role, pid, platform, binding_id)
        raise HTTPException(status_code=403)

    await get_pool().execute(
        "UPDATE platform_channel_bindings SET archived_at = now() WHERE id = $1 AND goal_id = $2",
        binding_id, pid,
    )
    _log.info("%s binding removed: %s (goal=%s)", platform, binding_id, pid)
    return RedirectResponse(url=f"/goals/{pid}/integrations", status_code=303)


@router.post("/{pid}/settings/memory")
async def save_memory_settings(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Save memory configuration for a goal."""
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        _log.warning("save_memory_settings: denied uid=%s role=%s goal=%s", user["uid"], role, pid)
        raise HTTPException(status_code=403)

    form = await request.form()
    pool = get_pool()

    # Parse form fields — empty string means "use default" (NULL)
    def parse_int(key: str):
        val = form.get(key, "").strip()
        return int(val) if val else None

    await pool.execute("""
        UPDATE goals SET
            memory_goal_max_kb = $2,
            memory_conversation_max_kb = $3,
            memory_compaction_target_pct = $4,
            memory_recent_messages = $5,
            memory_auto_summary_interval = $6,
            memory_stale_conversation_days = $7,
            memory_instance_idle_minutes = $8
        WHERE id = $1
    """,
        pid,
        parse_int("goal_max_kb"),
        parse_int("conversation_max_kb"),
        parse_int("compaction_target_pct"),
        parse_int("recent_messages"),
        parse_int("auto_summary_interval"),
        parse_int("stale_conversation_days"),
        parse_int("instance_idle_minutes"),
    )

    # No dedicated page for memory settings — the form is parked (see the
    # comment above this route's sibling in the old settings.html, now
    # removed). Land on the goal dashboard as a safe generic fallback.
    return RedirectResponse(url=f"/goals/{pid}", status_code=303)


# ── Goal Documents ────────────────────────────────────────────────────────────

_DOC_META = {
    "code":     {"title": "Work Documentation",  "subtitle": "What was built, how, and why."},
    "progress": {"title": "Feature Overview",     "subtitle": "What this goal builds and delivers — features and capabilities in plain language."},
}

# Generous over the ~20-30s a dispatch normally takes. A requested_at older
# than this is treated as abandoned (crashed dispatch, disabled scheduler, or
# a stale row carried over from the goal_summaries backfill migration) rather
# than still in flight — otherwise a single failed generation permanently
# hides the Regenerate button with no way to retry.
_REGEN_STALE_AFTER = timedelta(minutes=3)


def _is_regen_in_progress(requested_at, generated_at) -> bool:
    """True if a document dispatch is plausibly still running for this goal."""
    if not requested_at:
        return False
    if generated_at and generated_at >= requested_at:
        return False
    return requested_at > datetime.now(timezone.utc) - _REGEN_STALE_AFTER


async def _safe_regenerate_goal_documents(goal_id: str) -> None:
    """Background task: force-dispatch both goal documents.

    Best-effort and fully isolated from the triggering request — this runs
    detached via asyncio.create_task, so the HTTP handler returns immediately
    instead of blocking on the ~20-30s it takes both LLM calls to finish.
    """
    from app.services.goal_summary.dispatcher import dispatch_summary_for_goal
    try:
        ok = await dispatch_summary_for_goal(goal_id)
        if ok:
            _log.info("goal_documents: dispatch succeeded for manual regenerate of goal %s", goal_id)
        else:
            _log.warning("goal_documents: dispatch failed for manual regenerate of goal %s", goal_id)
    except Exception:
        _log.exception("goal_documents: manual regenerate crashed for goal %s", goal_id)


@router.post("/{pid}/summary/regenerate")
async def regenerate_goal_documents(
    pid: str, request: Request, doc_type: str = "progress", user=Depends(get_current_user)
):
    """Kick off a background dispatch of both goal documents, bypassing the trigger condition.

    Called by the Regenerate button on the document page. Any goal member can trigger this.
    Returns the shared _regen_button.html fragment immediately, already wired to
    self-poll regen-status — the actual generation runs as a detached background
    task (see _safe_regenerate_goal_documents). `doc_type` is only used to build
    that poll URL for whichever document page triggered this; both doc types are
    always regenerated together.
    """
    from fastapi.responses import HTMLResponse
    pool = get_pool()
    goal = await pool.fetchrow("SELECT id, go_agent_id FROM goals WHERE id = $1", pid)
    if not goal:
        raise HTTPException(status_code=404)
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        _log.warning("regenerate_goal_documents: denied uid=%s not-a-member goal=%s", user["uid"], pid)
        raise HTTPException(status_code=403)

    if not goal["go_agent_id"]:
        _log.warning("goal_documents: regenerate requested for %s but no GO agent wired", pid)
        return HTMLResponse(
            '<span class="text-xs text-red-400">No GO agent — cannot regenerate.</span>'
        )

    _log.info(
        "goal_documents: manual regenerate requested for goal %s by user %s", pid, user["uid"]
    )
    import asyncio as _asyncio
    _asyncio.create_task(_safe_regenerate_goal_documents(pid))

    return templates.TemplateResponse(
        "goals/_regen_button.html",
        {"request": request, "pid": pid, "doc_type": doc_type, "regen_in_progress": True},
    )


@router.get("/{pid}/summary/{doc_type}/regen-status")
async def get_goal_document_regen_status(
    pid: str, doc_type: str, request: Request, user=Depends(get_current_user)
):
    """HTMX poll target — re-checks whether regeneration is still in flight.

    Returns the shared _regen_button.html fragment with freshly computed
    regen_in_progress. Once that flips false (done, or past the staleness
    cutoff — see _is_regen_in_progress), the returned fragment is the plain
    Regenerate button with no polling trigger, so htmx stops polling on its
    own. This is what lets a stuck "Generating…" indicator recover without a
    full page reload.
    """
    if doc_type not in _DOC_META:
        raise HTTPException(status_code=404, detail="Unknown document type")
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT requested_at, generated_at FROM goal_documents WHERE goal_id = $1 AND doc_type = $2",
        pid, doc_type,
    )
    requested_at = row["requested_at"] if row else None
    generated_at = row["generated_at"] if row else None
    return templates.TemplateResponse(
        "goals/_regen_button.html",
        {
            "request": request,
            "pid": pid,
            "doc_type": doc_type,
            "regen_in_progress": _is_regen_in_progress(requested_at, generated_at),
        },
    )


@router.get("/{pid}/summary")
async def get_goal_summary_redirect(pid: str):
    """Legacy redirect — /summary now defaults to the Feature Overview.

    Work Documentation (doc_type='code') is not currently surfaced in the UI
    (see the hidden switch-link comment in goals/summary.html), so the
    legacy /summary link shouldn't route there either.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/goals/{pid}/summary/progress", status_code=302)


@router.get("/{pid}/summary/{doc_type}")
async def get_goal_document(
    pid: str, doc_type: str, request: Request, user=Depends(get_current_user)
):
    """Render a goal document page (doc_type = 'code' or 'progress')."""
    if doc_type not in _DOC_META:
        # Full-page nav — bounce back to the goal resources page with a toast
        # rather than raising (which would render as a raw JSON body).
        return flash_redirect(f"/goals/{pid}/resources", "Unknown document type.", flash_type="warning")
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT id, name, objective, status, completion_pct FROM goals WHERE id = $1", pid,
    )
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    row = await pool.fetchrow(
        "SELECT content, requested_at, generated_at, generated_by "
        "FROM goal_documents WHERE goal_id = $1 AND doc_type = $2",
        pid, doc_type,
    )
    requested_at = row["requested_at"] if row else None
    generated_at = row["generated_at"] if row else None
    regen_in_progress = _is_regen_in_progress(requested_at, generated_at)
    _log.info(
        "goal_document page: pid=%s doc_type=%s has_content=%s regen_in_progress=%s",
        pid, doc_type, bool(row and row["content"]), regen_in_progress,
    )
    meta = _DOC_META[doc_type]
    return templates.TemplateResponse(
        "goals/summary.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "doc_type": doc_type,
            "doc_title": meta["title"],
            "doc_subtitle": meta["subtitle"],
            "other_doc_type": "progress" if doc_type == "code" else "code",
            "other_doc_title": _DOC_META["progress" if doc_type == "code" else "code"]["title"],
            "content": row["content"] if row else None,
            "requested_at": requested_at,
            "generated_at": generated_at,
            "generated_by": row["generated_by"] if row else None,
            "regen_in_progress": regen_in_progress,
        },
    )


@router.get("/{pid}/summary/{doc_type}/content")
async def get_goal_document_content(
    pid: str, doc_type: str, request: Request, user=Depends(get_current_user)
):
    """HTMX partial — refreshes just the #summary-body div."""
    if doc_type not in _DOC_META:
        raise HTTPException(status_code=404, detail="Unknown document type")
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT content, requested_at, generated_at, generated_by "
        "FROM goal_documents WHERE goal_id = $1 AND doc_type = $2",
        pid, doc_type,
    )
    requested_at = row["requested_at"] if row else None
    generated_at = row["generated_at"] if row else None
    return templates.TemplateResponse(
        "goals/_summary_content.html",
        {
            "request": request,
            "user": user,
            "pid": pid,
            "doc_type": doc_type,
            "content": row["content"] if row else None,
            "requested_at": requested_at,
            "generated_at": generated_at,
            "regen_in_progress": _is_regen_in_progress(requested_at, generated_at),
            "generated_by": row["generated_by"] if row else None,
        },
    )
