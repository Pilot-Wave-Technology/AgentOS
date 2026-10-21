"""Goal thread — shared discussion between all goal members and the GO agent.

The goal thread is the single place where humans discuss planning, decisions,
and status with the Goal Orchestrator. All members see all messages in real-time.
Messages are forwarded to the GO agent's OpenClaw session for processing.
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, verify_firebase_token
from app.config import get_settings
from app.database import get_pool
# I5f — the goal thread's one card type. Extracted because this
# module imports firebase_admin, so a parser living here cannot
# be tested without the whole auth stack (same reason as
# work_shape.py in Phase 3).
from app.services.goal_thread_cards import proposal_of
from app.notifications import push_notification
from app.openclaw import session_manager, gateway_admin
from app.unread import increment_unread, mark_thread_read
from app.ledger.journal import append_event
from app.ledger.schema import MESSAGE_POSTED
from app.ledger.actors import agent_actor
from app.services.go_agent import build_go_context
from app.services.go_response import process_go_response
from app.services.go_batcher import get_or_create_batcher
from app.services.go_history import maybe_update_goal_summary
from app.routers.thread_paging import (
    INITIAL_MESSAGE_LIMIT,
    EARLIER_PAGE_SIZE,
    split_probe_row,
)

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Page sizes and the probe-row helper are shared with the read-only channels —
# see app/routers/thread_paging.py for the keyset design and why this page is
# windowed at all. The GO thread keeps its OWN queries below because it joins
# users/agents to resolve a display name and filters on sender rather than the
# channels' kind-based rules.

# Columns every message-list query must select, so the full page render and the
# "load earlier" partial hydrate an identical `msg` shape for _message.html.
# `platform` is here so the page can say WHERE a message was sent from.
# It has been written by the Slack inbound path since the channel rework
# and selected by nothing, so a message typed in Slack rendered exactly
# like one typed in the browser.
_MESSAGE_COLUMNS = """m.id, m.sender, m.role, m.content, m.created_at, m.token_count,
                      m.card_data, m.platform,
                      COALESCE(u.display_name, a.name, m.sender) AS sender_name"""


# THE ERRAND PROPOSAL IS THE ONE EXCEPTION, because it already has a
# server-rendered partial with its own Approve/Decline form. Everything
# else goes to the client renderer.
_SERVER_RENDERED = ("errand_proposal",)


def _kernel_card(card) -> str:
    """The card as JSON for the client, or "" when there is none.

    PASSES EVERYTHING THROUGH, and the first version did not. It carried
    a four-name allowlist -- kernel_park, kernel_verdict, kernel_pr,
    kernel_stall -- written from the kinds I happened to have read.
    There are sixteen: approval, completion, question, review, progress,
    failure, kernel_answer, git_pr_opened, git_node_error and the rest.
    A card whose kind was not on the list reached the thread and
    rendered as nothing at all.

    An allowlist is the wrong shape here twice over: it drops what
    exists today, and it silently drops whatever is added next. The
    client renders what it knows -- a parked executor gets its buttons
    -- and anything else gets its title and body, which is a card
    somebody can read rather than a message that vanished.

    Returned as TEXT for a `<script type="application/json">`, the same
    carrier the assistant's thread uses, so both hydrate from one shape.
    """
    if not card:
        return ""
    try:
        data = json.loads(card) if isinstance(card, str) else dict(card)
    except Exception:                                       # noqa: BLE001
        return ""
    if not isinstance(data, dict) or not data.get("card_type"):
        return ""
    if data.get("card_type") in _SERVER_RENDERED:
        return ""
    # `</` would close the script tag early. The client un-escapes it.
    return json.dumps(data, default=str).replace("</", "<" + chr(92) + "/")


def _normalize_messages(rows) -> list[dict]:
    """Convert message rows to template dicts, tidying the GO's display name.

    The GO agent is stored per-goal as "GO — <Goal Name>"; the thread UI always
    refers to it as plain "Goal Orchestrator". Council senders keep their own
    names and must not be rewritten (the template colours them differently).
    """
    messages = []
    for row in rows:
        msg = dict(row)
        if msg["role"] == "agent" and (msg["sender_name"] or "").startswith("GO —"):
            msg["sender_name"] = "Goal Orchestrator"
        card = msg.pop("card_data", None)
        msg["proposal"] = proposal_of(card)
        # A PARKED EXECUTOR'S CARD, kept so the page can render its
        # BUTTONS. It used to be dropped here -- `proposal_of` returns
        # None for anything that is not an errand proposal -- so when an
        # approved errand's conversation moved to this thread, its plan
        # card arrived as prose and the only way to approve one was a
        # POST typed into a browser console.
        msg["kernel_card"] = _kernel_card(card)
        messages.append(msg)
    return messages


# ── Goal thread subscriber registry (multi-user broadcast) ──────────────────
# THE REGISTRY MOVED, and it is now keyed by (goal, thread) rather than
# by goal -- see `goal_thread_subs`. A goal with autonomous errands has
# more than one GO thread, and a goal-keyed fan-out puts every thread's
# replies in every open tab.
#
# These two names are kept as thin wrappers because they are imported by
# three modules outside this router (`app/channels/base.py`, and both
# Slack inbound paths), and `thread_id` defaults to None -- the old
# goal-wide behaviour -- so a caller that has no thread to name still
# reaches somebody.
from app.routers.goal_thread_subs import (                  # noqa: E402
    broadcast as _subs_broadcast,
    subscribe as _subs_subscribe,
    watcher_count as _subs_watcher_count,
)


def _subscribe_to_goal_thread(goal_id: str, send_fn, thread_id: str):
    """Register a websocket send function for one thread's broadcasts."""
    return _subs_subscribe(goal_id, thread_id, send_fn)


async def _broadcast_to_goal_thread(goal_id: str, message: str,
                                    thread_id: str | None = None):
    """Send a message to everyone watching that thread of the goal."""
    await _subs_broadcast(goal_id, message, thread_id)


async def _story_of_thread(pool, pid: str, thread_id: str):
    """The story an errand thread is about, or None.

    ONLY WHAT THE PAGE READS -- id, title, workspace_state and the
    machine. A wider
    select here would be a second definition of "the current story"
    beside the assistant's, and the first time one grew a field they
    would disagree about what a thread is showing.
    """
    try:
        row = await pool.fetchrow(
            "SELECT s.id, s.title, s.workspace_state, s.sandbox_size "
            "  FROM threads t JOIN stories s ON s.id = t.story_id "
            " WHERE t.id = $1 AND t.goal_id = $2", thread_id, pid)
    except Exception:                                       # noqa: BLE001
        _log.warning("goal-thread: could not resolve the story for %s",
                     thread_id, exc_info=True)
        return None
    return dict(row) if row else None


async def _thread_for(pool, pid: str, thread_id: str | None) -> str | None:
    """Which GO thread this request is about, or None to refuse.

    EVERY ENDPOINT THIS PAGE CALLS NEEDS IT, and three of them did not
    take it. The page resolved the errand's thread server-side and
    rendered its history correctly -- and then the websocket, the
    new-message poll, the load-earlier fetch and the run-state poll all
    connected without naming a thread, resolved to `goals.go_thread_id`,
    and sent everything to the MAIN conversation. So an errand thread
    displayed its own past and wrote its present somewhere else.

    Half-wiring a parameter is worse than not adding it: the page LOOKS
    right, which is what took a person running a live errand to notice.
    """
    goal = await pool.fetchrow(
        "SELECT id, go_thread_id, orchestrator_thread_id, "
        "       council_thread_id, security_thread_id "
        "  FROM goals WHERE id = $1", pid)
    if goal is None:
        return None
    from app.routers.go_thread_pick import resolve as _pick
    return await _pick(pool, dict(goal), thread_id)



# ── Goal thread page ────────────────────────────────────────────────────────

@router.get("/goals/{pid}/thread", response_class=HTMLResponse)
async def goal_thread_page(
    pid: str,
    request: Request,
    thread_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Render a GO thread of this goal — the main one unless named.

    `thread_id` names an autonomous errand's own GO thread. Omitted, this
    is the page it has always been. See `go_thread_pick` for what is
    checked and why a bad id is refused rather than defaulted.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not goal:
        raise HTTPException(status_code=404)
    goal = dict(goal)

    from app.routers.go_thread_pick import resolve as _pick_go_thread
    go_thread_id = await _pick_go_thread(pool, goal, thread_id)
    if not go_thread_id:
        raise HTTPException(status_code=404, detail="Goal thread not found")

    from app.services.machine_lock import locked_reason
    current_story = await _story_of_thread(pool, pid, go_thread_id)

    # Mark the GO thread as read for this user — zeroes their unread counter
    # and notifies any other open tabs via pg_notify → user WS.
    # Non-critical: wrapped so a counter failure never breaks page load.
    try:
        await mark_thread_read(pool, go_thread_id, user["uid"])
    except Exception as exc:
        _log.warning("goal-thread: mark_thread_read failed (non-fatal): %s", exc)

    # Load the NEWEST window of messages (join users for humans, agents for GO).
    # Fetching newest-first then re-sorting ascending is what bounds the page:
    # the inner ORDER BY walks idx_messages_thread (thread_id, created_at)
    # backwards and stops after the limit instead of scanning the whole thread.
    #
    # SYSTEM ROWS BELONG ON AN AUTONOMOUS ERRAND'S THREAD and nowhere
    # else. They are how the errand proceeds -- the parks, the check-ins,
    # the kernel's answers -- and hiding them leaves a person watching
    # work happen with no account of why. On every other surface the same
    # rows are plumbing and stay hidden. See
    # `app/services/errand_thread_visibility`.
    from app.services.errand_thread_visibility import (
        is_autonomous_errand_thread)
    show_system = await is_autonomous_errand_thread(pool, go_thread_id)

    # We ask for one row more than we display purely as a has-more probe — if
    # the extra row comes back there is older history, so the button renders.
    msg_rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_MESSAGE_COLUMNS}
                  FROM messages m
                  LEFT JOIN users u ON u.uid = m.sender
                  LEFT JOIN agents a ON a.id = m.sender
                 WHERE m.thread_id = $1
                   -- A CARD IS NEVER PLUMBING, whoever sent it. The live
                   -- fanout already says so -- "a card-bearing row
                   -- (card_data present) is a visible bubble even when
                   -- its kind became system_wake, so it is NOT filtered"
                   -- and it filters only a SYSTEM-sender system_wake
                   -- with NO card.
                   --
                   -- This filter looked at the sender alone. So a card
                   -- posted as `system` was broadcast live and then
                   -- hidden on reload: the verification card and the
                   -- completion card both "went away after a refresh",
                   -- sitting correct in the database the whole time.
                   --
                   -- Two readers of one thread must not disagree about
                   -- what is on it; the fanout's rule is the right one
                   -- and this is it, restated where the page reads.
                   AND (m.sender <> 'system'
                        OR m.card_data IS NOT NULL
                        OR $3::bool)
                 ORDER BY m.created_at DESC, m.id DESC
                 LIMIT $2
           ) newest
           ORDER BY created_at ASC, id ASC""",
        go_thread_id, INITIAL_MESSAGE_LIMIT + 1, show_system,
    )
    msg_rows, has_older_messages = split_probe_row(msg_rows, INITIAL_MESSAGE_LIMIT)
    messages = _normalize_messages(msg_rows)
    # Cursor the "Load earlier" button pages backwards from.
    oldest_message_id = messages[0]["id"] if messages else ""

    _log.info(
        "goal-thread: rendering goal=%s thread=%s messages=%d has_older=%s",
        pid, go_thread_id, len(messages), has_older_messages,
    )

    # Load members for display
    member_rows = await pool.fetch(
        """SELECT gm.uid, gm.role, u.display_name, u.email
           FROM goal_members gm
           JOIN users u ON u.uid = gm.uid
           WHERE gm.goal_id = $1""",
        pid,
    )
    members = [dict(r) for r in member_rows]

    firebase_token = request.cookies.get("firebase_token", "")

    # Skipped for the reserved __platform__ goal: its agent is the Platform
    # Steward, not a per-goal GO, and the tour's copy talks specifically
    # about GO.
    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = (
        await check_and_mark_milestone_if_enabled(user, "goal-thread-intro")
        if pid != "__platform__" else False
    )

    return templates.TemplateResponse(
        "goal_thread/index.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "thread_id": go_thread_id,
            # THE STORY BEHIND AN ERRAND THREAD, so the page can render
            # its workspace. `None` on the goal's main thread, and the
            # shared panel renders nothing without it -- which is every
            # goal thread that existed before errands moved here.
            "current_story": current_story,
            # Whether the machine can still be changed, or why not. The
            # same helper guards the endpoint -- this only decides
            # whether the control is greyed out.
            "machine_locked": (
                await locked_reason(pool, current_story["id"])
                if current_story else None),
            "messages": messages,
            "has_older": has_older_messages,
            "oldest_message_id": oldest_message_id,
            # NAMED, so "load earlier" on an errand thread does not
            # page back through the main goal conversation.
            "load_earlier_url": (f"/goals/{pid}/thread/messages/earlier"
                                 f"?thread_id={go_thread_id}"),
            "members": members,
            "firebase_token": firebase_token,
            # Gate the Security Council manual-trigger button: hidden when the
            # manual trigger is disabled on this deployment (e.g. dormant on prod).
            "security_enabled": getattr(get_settings(), "security_ui_enabled", False),
            "auto_fire_tour": auto_fire_tour,
        },
    )


# ── Run state (is GO currently thinking / using a tool?) ────────────────────

@router.get("/goals/{pid}/thread/run-state")
async def get_go_thread_run_state(
    pid: str,
    thread_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Return the GO's current run indicator state for this goal's thread.

    Used by the GO thread UI on page load to restore the "thinking…" or
    "Using <tool>…" indicator if a run is still in flight.

    Stale rows (updated_at > 30 min ago) are cleaned up lazily.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    from fastapi.responses import JSONResponse
    pool = get_pool()
    thread_id = await _thread_for(pool, pid, thread_id)
    if not thread_id:
        return JSONResponse({"status": "idle"})
    await pool.execute(
        "DELETE FROM chat_run_state WHERE updated_at < now() - interval '30 minutes'",
    )
    row = await pool.fetchrow(
        """SELECT status, current_tool, started_at
             FROM chat_run_state
            WHERE thread_id = $1
              AND updated_at > now() - interval '30 minutes'""",
        thread_id,
    )
    if not row:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status": row["status"],
        "tool": row["current_tool"] or "",
        "started_at": row["started_at"].isoformat() if row["started_at"] else "",
    })


@router.get("/goals/{pid}/thread/messages/earlier", response_class=HTMLResponse)
async def get_thread_messages_earlier(
    pid: str,
    request: Request,
    before_id: str = "",
    thread_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Return the page of messages immediately OLDER than `before_id`, as HTML.

    Backs the "Load earlier messages" button. Returns a rendered partial rather
    than JSON so the rows go straight into the DOM via htmx and reuse the exact
    same template as the full page render — no duplicate client-side renderer.

    Paging is keyset, not OFFSET: OFFSET re-scans the skipped rows on every
    page, which is the cost we are trying to remove. The cursor is the tuple
    (created_at, id), NOT created_at alone — created_at is not unique, and two
    messages sharing a timestamp across a page boundary would otherwise be
    silently skipped or repeated.

    This endpoint only ever looks backwards, so it cannot collide with the
    live WebSocket append path or the forward catch-up below: new messages are
    always newer than any cursor this button can hold.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    thread_id = await _thread_for(pool, pid, thread_id)
    if not thread_id:
        raise HTTPException(status_code=404, detail="Goal thread not found")

    # Resolve the cursor. A missing/unknown anchor means the client is out of
    # sync with the thread (stale tab, message deleted) — render an empty,
    # terminal partial rather than erroring out the whole button.
    anchor = None
    if before_id:
        anchor = await pool.fetchrow(
            "SELECT created_at, id FROM messages WHERE id = $1 AND thread_id = $2",
            before_id, thread_id,
        )
    if anchor is None:
        _log.warning(
            "goal-thread: earlier-page anchor not found goal=%s before_id=%r", pid, before_id,
        )
        return templates.TemplateResponse(
            "goal_thread/_earlier_messages.html",
            {
                "request": request, "pid": pid, "messages": [], "has_older": False,
                "load_earlier_url": f"/goals/{pid}/thread/messages/earlier",
            },
        )

    # SAME SYSTEM-ROW RULE AS THE PAGE RENDER AND THE CATCH-UP, and this
    # one did not have it. Both of those show `sender = 'system'` rows on
    # an autonomous errand's thread -- there they are the errand talking,
    # not plumbing -- and hide them everywhere else. This path hid them
    # unconditionally.
    #
    # So on an errand thread the first page showed the system rows and
    # scrolling back made them disappear: the same conversation, two
    # different histories, depending on how far up you had read. The
    # catch-up path's own comment states the rule -- "it has to be the
    # same or a reconnect would quietly change what the thread shows" --
    # and the same argument applies to paging backwards.
    from app.services.errand_thread_visibility import (
        is_autonomous_errand_thread)
    show_system = await is_autonomous_errand_thread(pool, thread_id)

    rows = await pool.fetch(
        f"""SELECT * FROM (
                SELECT {_MESSAGE_COLUMNS}
                  FROM messages m
                  LEFT JOIN users u ON u.uid = m.sender
                  LEFT JOIN agents a ON a.id = m.sender
                 WHERE m.thread_id = $1
                   -- Same rule as the page render and the fanout: a row
                   -- carrying a card is a bubble, not plumbing.
                   AND (m.sender <> 'system'
                        OR m.card_data IS NOT NULL
                        OR $5::bool)
                   AND (m.created_at, m.id) < ($2, $3)
                 ORDER BY m.created_at DESC, m.id DESC
                 LIMIT $4
           ) older
           ORDER BY created_at ASC, id ASC""",
        thread_id, anchor["created_at"], anchor["id"], EARLIER_PAGE_SIZE + 1,
        show_system,
    )
    rows, has_older = split_probe_row(rows, EARLIER_PAGE_SIZE)
    messages = _normalize_messages(rows)

    _log.info(
        "goal-thread: earlier page goal=%s before_id=%s returned=%d has_older=%s",
        pid, before_id, len(messages), has_older,
    )

    return templates.TemplateResponse(
        "goal_thread/_earlier_messages.html",
        {
            "request": request,
            "pid": pid,
            "messages": messages,
            "has_older": has_older,
            "load_earlier_url": f"/goals/{pid}/thread/messages/earlier",
            # Next cursor: the oldest row in THIS page.
            "before_id": messages[0]["id"] if messages else "",
        },
    )


@router.get("/goals/{pid}/thread/messages")
async def get_thread_messages_after(
    pid: str,
    after: str = "",
    thread_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Return messages in the GO thread that arrived after `after` (message ID).

    Used by the browser on WS reconnect to catch up on any messages that were
    broadcast while the connection was down. Returns up to 50 messages to
    bound response size.

    If `after` is empty or not found in the thread, returns an empty list.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    from fastapi.responses import JSONResponse
    pool = get_pool()
    thread_id = await _thread_for(pool, pid, thread_id)
    if not thread_id:
        return JSONResponse({"messages": []})
    if not after:
        return JSONResponse({"messages": []})

    # Find the creation time of the anchor message so we can fetch anything after it.
    anchor = await pool.fetchrow(
        "SELECT created_at FROM messages WHERE id = $1 AND thread_id = $2",
        after, thread_id,
    )
    if not anchor:
        return JSONResponse({"messages": []})

    # SAME RULE AS THE PAGE RENDER, and it has to be the same or a
    # reconnect would quietly change what the thread shows.
    from app.services.errand_thread_visibility import (
        is_autonomous_errand_thread)

    # CARD_DATA IS PART OF A MESSAGE, and leaving it out of this one
    # query is why an errand approval "only appears after a refresh".
    #
    # The sequence, measured on ERR08. The card is posted at 15:32:31.
    # The browser's socket is down between 15:31:36 and 15:33:09, so the
    # live broadcast reaches nobody -- correctly; there was nobody there.
    # On reconnect the client calls this endpoint to catch up, and gets
    # the card's row WITHOUT its card_data, so it renders as a plain
    # bubble: the proposal's prose, no buttons, unapprovable. A reload
    # then "fixes" it, because the full page render does read card_data.
    #
    # Neither delivery nor rendering was at fault, which is why looking
    # at both explained nothing. The catch-up path simply dropped the
    # half of the row that makes a card a card.
    rows = await pool.fetch(
        """SELECT m.id, m.sender, m.role, m.content, m.created_at,
                  m.card_data,
                  COALESCE(u.display_name, a.name, m.sender) AS sender_name
             FROM messages m
             LEFT JOIN users u ON u.uid = m.sender
             LEFT JOIN agents a ON a.id = m.sender
            WHERE m.thread_id = $1
              AND m.created_at > $2
              -- Same rule as the page render and the fanout.
              AND (m.sender <> 'system'
                   OR m.card_data IS NOT NULL
                   OR $3::bool)
            ORDER BY m.created_at ASC
            LIMIT 50""",
        thread_id, anchor["created_at"],
        await is_autonomous_errand_thread(pool, thread_id),
    )
    out = []
    for r in rows:
        msg = {
            "id": r["id"],
            "sender": r["sender"],
            "sender_name": r["sender_name"],
            "role": r["role"],
            "content": r["content"],
        }
        # THE SAME TWO SHAPES THE PAGE RENDER PRODUCES, from the same
        # two helpers, so a caught-up card is byte-identical to one that
        # arrived live. A third way of describing a card is a third way
        # for it to be subtly wrong.
        card = r["card_data"]
        proposal = proposal_of(card)
        if proposal is not None:
            msg["proposal"] = proposal
        kernel_card = _kernel_card(card)
        if kernel_card:
            msg["kernel_card"] = kernel_card
        out.append(msg)
    return JSONResponse({"messages": out})


# ── WebSocket endpoint ──────────────────────────────────────────────────────

@router.websocket("/goals/{pid}/thread/ws")
async def goal_thread_ws(
    pid: str,
    websocket: WebSocket,
    token: str,
    thread_id: str | None = None,
):
    """WebSocket for the shared goal thread.

    All connected members see each other's messages in real-time.
    Messages are also forwarded to the GO agent for processing.
    """
    await websocket.accept()

    # Auth
    try:
        claims = verify_firebase_token(token)
        uid = claims["uid"]
    except HTTPException:
        await websocket.send_text(json.dumps({"type": "error", "message": "Unauthorized"}))
        await websocket.close()
        return

    pool = get_pool()

    # Verify membership
    role = await get_goal_role(uid, pid)
    if role is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "Not a member"}))
        await websocket.close()
        return

    # Get goal + GO info
    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    if not goal:
        await websocket.close()
        return
    # Only active goals get a GO session / lazy GO create (status column, not state).
    if goal.get("status") != "active":
        _log.info("goal_thread ws: goal %s not active (status=%s) — refusing GO session/lazy-create",
                  pid, goal.get("status"))
        await websocket.close()
        return

    go_thread_id = goal["go_thread_id"]
    # ONLY WHEN ONE WAS NAMED. The page can 404 a GO-less goal; this
    # socket cannot, because the lazy-create below is the self-heal for
    # exactly that state and it runs with an empty `go_thread_id`.
    # Resolving unconditionally would close the socket on the goals that
    # most need it open.
    #
    # Same resolver as the page, though: the socket carries what you
    # type, so a thread it accepts that the page refuses would let
    # somebody write into a conversation they cannot read.
    if thread_id:
        from app.routers.go_thread_pick import resolve as _pick_go_thread
        picked = await _pick_go_thread(pool, dict(goal), thread_id)
        if not picked:
            _log.info("goal_thread ws: %s is not a GO thread of %s — refusing",
                      thread_id, pid)
            await websocket.close()
            return
        go_thread_id = picked
    go_agent_id = goal["go_agent_id"]
    user_row = await pool.fetchrow("SELECT * FROM users WHERE uid = $1", uid)
    user_actor_id = user_row["actor_id"] if user_row else None
    sender_name = (user_row["display_name"] if user_row else None) or ""

    if not go_agent_id:
        # GO creation at goal-creation is best-effort and was historically lost to a
        # gateway readiness race ("unknown agent id"), leaving the goal GO-less with no
        # retry anywhere. Self-heal: (re)create the GO on thread open. create_go_for_goal
        # is idempotent (reuses an existing gateway agent), and it now waits for the
        # gateway to register the agent before pushing files.
        try:
            from app.services.go_agent import create_go_for_goal
            from app.llm_models import default_for
            go_result = await create_go_for_goal(
                goal_id=pid,
                goal_name=goal.get("name") or pid,
                objective=goal.get("objective") or "",
                created_by_uid=uid,
                model=goal.get("go_model") or default_for("go"),
            )
            go_agent_id = go_result["agent_id"]
            await pool.execute(
                "UPDATE goals SET go_agent_id = $1 WHERE id = $2", go_agent_id, pid,
            )
            _log.info("GO agent lazily created for goal %s on thread open: %s",
                      pid, go_agent_id)
        except Exception as exc:
            _log.warning(
                "Lazy GO creation failed for goal %s: %s — thread will work but GO "
                "won't respond (retries next thread open)", pid, exc,
            )

    # Build GO session context and callbacks
    go_context = await _get_go_context(pool, pid, go_agent_id)
    on_message, on_notify = _make_go_callbacks(pool, pid, go_agent_id, go_thread_id)

    # Get or create GO session (reused across all members in this goal)
    session = None
    if go_agent_id:
        session = session_manager.get_or_create(
            pid, go_agent_id, go_context, on_message, on_notify,
            thread_id=go_thread_id,
        )
        # Set gateway agent ID so send_to_gateway uses it for session key
        gw_id = go_context.get("gateway_agent_id", "")
        if gw_id:
            session._gateway_agent_id = gw_id
            _log.info("GO session: agent_id=%s, gw_id=%s, thread=%s", go_agent_id, gw_id, go_thread_id)

            # NOTE: We deliberately do NOT re-push AGENTS.md (or any
            # other static bootstrap file) on WS open. Those files are
            # written ONCE at agent provisioning. Re-pushing them on
            # every chat reconnect invalidates Gemini's implicit
            # prompt cache, costing 15-20K tokens of re-uploaded
            # `input` per reconnect. The only file that legitimately
            # changes mid-session is RESOURCES.md, which has its own
            # change-driven push via _notify_resources_refreshed (with
            # hash-dedupe). If the GO template's AGENTS.md is edited
            # in the admin UI, the template_sync push-on-write path
            # propagates that to live agents — no belt-and-braces
            # needed here.
        else:
            _log.warning("GO session: no gateway_agent_id found for %s", go_agent_id)

    # Mark the GO thread as read for this user on WS connect (they are now viewing it).
    # Non-critical: counter failure must never block the WS connection.
    try:
        await mark_thread_read(pool, go_thread_id, uid)
    except Exception as exc:
        _log.warning("goal-thread WS: mark_thread_read failed (non-fatal): %s", exc)

    # Subscribe this websocket to goal thread broadcasts (all members see all messages)
    # NAMED, not goal-wide. This endpoint renders the MAIN GO thread, so
    # it must not receive an errand thread's traffic once errands open
    # threads of their own.
    unsub_thread = _subscribe_to_goal_thread(pid, websocket.send_text,
                                             go_thread_id)
    _log.info("Goal thread subscriber added: goal=%s, user=%s, thread=%s, total=%d",
              pid, uid, go_thread_id,
              _subs_watcher_count(pid, go_thread_id))

    # Subscribe to the GO session's tool events so the UI can show
    # "using <tool>" in real time. We only forward tool_start / tool_result —
    # everything else still comes through _broadcast_to_goal_thread to avoid
    # duplicate message rendering.
    unsub_session = None
    if session:
        async def _tool_event_relay(raw: str):
            try:
                parsed = json.loads(raw)
                t = parsed.get("type")
                # AND `agent_card`, which is how a PARKED EXECUTOR reaches
                # a browser that is already open. `_chat_broadcaster`
                # pushes the card frame to the SESSION's subscribers --
                # the surface the assistant's chat page listens on -- and
                # this page listens on the goal-thread registry instead.
                # So the card landed in the database and on a socket
                # nobody here was reading: the GO could truthfully say
                # "the card is in the thread" while the person saw
                # nothing, and a refresh fixed it. Measured on ERR01,
                # question 233.
                if t in ("tool_start", "tool_result", "agent_card",
                         "diff_review", "dag_recovery"):
                    await websocket.send_text(raw)
            except Exception:
                pass

        session._subscribers.setdefault(go_thread_id, set()).add(_tool_event_relay)

        def _unsub_session():
            session._subscribers.get(go_thread_id, set()).discard(_tool_event_relay)

        unsub_session = _unsub_session
        _log.info(
            "GO session tool-event subscriber added: thread=%s",
            go_thread_id[:12],
        )

    try:
        async for raw in websocket.iter_text():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if parsed.get("type") != "message":
                continue

            content = parsed.get("content", "").strip()
            if not content:
                continue

            msg_id = str(uuid.uuid4())

            # 1. Freeze gate — check BEFORE persisting so a dropped message
            # never becomes an orphaned DB row that GO silently misses.
            try:
                from app.services.execution_control import conversation_block as _conv_block  # noqa: PLC0415
                _freeze = await _conv_block(pid)
                if _freeze is not None:
                    await websocket.send_text(json.dumps({
                        "type": "system_notice",
                        "code": "EXECUTION_PAUSED",
                        "scope": _freeze.scope,
                        "message": (
                            f"⏸ The goal is paused ({_freeze.scope}). "
                            "Resume to talk to the orchestrator again."
                        ),
                    }))
                    continue
            except Exception:
                _log.exception("goal_thread freeze gate failed — failing open")

            # 2. Persist human message to DB (after gate — no orphaned rows on freeze)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    last_event_id = await _last_thread_event_id(conn, go_thread_id)
                    await append_event(
                        conn,
                        event_type=MESSAGE_POSTED,
                        payload={
                            "thread_id": go_thread_id,
                            "message_id": msg_id,
                            "sender": uid,
                            "role": "human",
                            "content_preview": content[:200],
                        },
                        actor_id=user_actor_id,
                        correlation_id=go_thread_id,
                        causation_ids=[last_event_id] if last_event_id else None,
                        context_goal_id=pid,
                        context_conversation_id=go_thread_id,
                    )
                    await conn.execute(
                        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                           VALUES ($1, $2, $3, 'human', $4, 0)""",
                        msg_id, go_thread_id, uid, content,
                    )

            # 2e. Increment unread for all OTHER goal members so their sidebar
            # badges update. Non-critical — wrapped so a counter failure never
            # breaks message delivery.
            try:
                await increment_unread(pool, go_thread_id, sender_uid=uid)
            except Exception as exc:
                _log.warning(
                    "goal-thread: increment_unread (human msg) failed (non-fatal): %s", exc,
                )

            # 3. Broadcast to ALL goal thread subscribers (sender + other members)
            broadcast_msg = json.dumps({
                "type": "message",
                "id": msg_id,
                "sender": uid,
                "sender_name": sender_name,
                "role": "human",
                "content": content,
            })
            await _broadcast_to_goal_thread(pid, broadcast_msg, go_thread_id)

            # 3b. Mirror to Slack goal channel (if Slack is enabled).
            try:
                from app.channels.slack.outbound import relay_human_message
                import asyncio as _asyncio_relay
                _asyncio_relay.create_task(
                    relay_human_message(pid, go_thread_id, content, uid, msg_id=msg_id)
                )
            except ImportError:
                pass
            except Exception as exc:
                _log.warning(
                    "Slack human relay scheduling failed (goal=%s): %s", pid, exc,
                )

            # 3c. Mirror to Notion GO thread page.
            try:
                from app.channels.notion.outbound import relay_human_message as _notion_human
                import asyncio as _asyncio_notion
                _asyncio_notion.create_task(_notion_human(pid, go_thread_id, content, sender_name))
            except Exception as exc:
                _log.warning("Notion human relay scheduling failed (goal=%s): %s", pid, exc)

            # 2d. Self-heal a stale connection: if this WS connected BEFORE the GO
            # existed (session is None — go_agent_id was empty at connect) but the GO has
            # since been created (lazy-ensure on another connection), bind the shared GO
            # session NOW so this message forwards instead of being silently dropped.
            # Without this the user has to hard-refresh; with it, the next message just
            # works. get_or_create returns the existing GO session if one is live.
            if session is None:
                fresh_go_id = await pool.fetchval(
                    "SELECT go_agent_id FROM goals WHERE id = $1", pid,
                )
                if fresh_go_id:
                    go_agent_id = fresh_go_id
                    go_context = await _get_go_context(pool, pid, go_agent_id)
                    on_message, on_notify = _make_go_callbacks(
                        pool, pid, go_agent_id, go_thread_id,
                    )
                    session = session_manager.get_or_create(
                        pid, go_agent_id, go_context, on_message, on_notify,
                        thread_id=go_thread_id,
                    )
                    gw_id = go_context.get("gateway_agent_id", "")
                    if gw_id:
                        session._gateway_agent_id = gw_id
                    _log.info(
                        "GO session lazily bound on first message for goal %s "
                        "(connection was stale/session-less)", pid,
                    )

            # 3. Forward to GO. With the chat_module queue on, enqueue the RAW
            # message (the co-located dispatcher batches + adds GO context);
            # otherwise use the in-router batcher.
            from app.chat_module.config import get_chat_config as _gcc
            _queue_on = _gcc().queue_enabled
            if session and _queue_on:
                await session.send_to_gateway(
                    json.dumps({"type": "message", "content": content,
                                "thread_id": go_thread_id}),
                    persisted_message_id=msg_id,
                )
                # Show the "thinking" indicator live for all thread members.
                # The legacy in-router batcher (the elif below) broadcasts this
                # when the batch fires; the queue path bypasses the batcher, so
                # without this the loader only ever appears on refresh (via the
                # DB run-state the dispatcher writes) and never on send.
                await _broadcast_to_goal_thread(
                    pid, json.dumps({"type": "thinking"}), go_thread_id)
            elif session:
                async def _send_batched_to_go(formatted_message: str):
                    """Send a batched message to GO with slim context (goal + team only)."""
                    # Broadcast "thinking" when batch fires
                    await _broadcast_to_goal_thread(
                        pid, json.dumps({"type": "thinking"}), go_thread_id)

                    # build_go_context is now slim: only goal identity (id/name/
                    # objective/description) + team roster. Everything else
                    # (tasks, resources, memory, recent messages, state doc) is
                    # available via bootstrap files + pwt_* tools.
                    try:
                        dynamic_context = await build_go_context(
                            pid, go_thread_id)
                    except Exception as exc:
                        _log.warning("Failed to build GO context for %s: %s", pid, exc)
                        dynamic_context = ""

                    full_message = (
                        f"{dynamic_context}\n\n---\n\n{formatted_message}"
                        if dynamic_context else formatted_message
                    )

                    go_msg = json.dumps({
                        "type": "message",
                        "content": full_message,
                        "thread_id": go_thread_id,
                    })
                    _log.info("Sending batched message to GO: goal=%s, context_len=%d",
                              pid, len(dynamic_context))
                    await session.send_to_gateway(go_msg)

                # Get goal settings for batch window
                batch_window = 10.0  # default
                try:
                    settings = goal.get("settings") or {}
                    if isinstance(settings, str):
                        import json as _json
                        settings = _json.loads(settings)
                    batch_window = float(settings.get("batch_window_seconds", 10.0))
                except Exception:
                    pass

                batcher = get_or_create_batcher(pid, _send_batched_to_go, batch_window)
                await batcher.add_message(uid, sender_name, content)
                _log.info("Message queued in batcher (pending=%d)", batcher.pending_count)

    except WebSocketDisconnect:
        _log.debug("Goal thread WS disconnected: %s/%s", pid, uid)
    except Exception as exc:
        _log.error("Goal thread WS error: %s/%s: %s", pid, uid, exc)
    finally:
        unsub_thread()
        if unsub_session:
            unsub_session()


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_go_context(pool, pid: str, go_agent_id: str) -> dict:
    """Build the goal context dict for the GO agent session."""
    if not go_agent_id:
        return {}

    agent = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", go_agent_id)
    if not agent:
        return {}

    ctx = await pool.fetchrow(
        "SELECT * FROM agent_contexts WHERE goal_id = $1 AND agent_id = $2",
        pid, go_agent_id,
    )

    return {
        "agent_id": go_agent_id,
        "gateway_agent_id": agent.get("gateway_agent_id", ""),
        "goal_id": pid,
        "agent_type": "GO",
        "scope": "goal",
        "model": agent.get("model", "google/gemini-3.8-flash"),
        "additional_instructions": ctx.get("additional_instructions", "") if ctx else "",
        "tools_allow": agent.get("tools_allow", []),
        "tools_deny": agent.get("tools_deny", []),
        "memory_enabled": True,
        "max_tokens": agent.get("max_tokens"),
        "workspace_files": ctx.get("workspace_files", []) if ctx else [],
        "skills": ctx.get("skills", []) if ctx else [],
    }


def _make_go_callbacks(pool, pid: str, go_agent_id: str, go_thread_id: str):
    """Create on_message and on_notify callbacks for the GO agent session.

    `go_thread_id` IS A FALLBACK, NOT THE DESTINATION. Both callbacks are
    handed the thread the reply actually belongs to; they used to accept
    that argument and then write everything -- message row, ledger event,
    unread bump, broadcast, Slack relay, notification link -- against the
    closure's `go_thread_id` instead.

    That was harmless while a goal had exactly one GO thread and is the
    single thing standing between the GO and autonomous errands: an
    approved errand opens a thread of its own on the GO, and with the
    closure winning, every reply to every errand lands in the main goal
    thread, attributed to the wrong conversation and invisible in the
    right one.

    So `_thread(thread_id)` below prefers the argument. The fallback
    stays because a caller that passes nothing used to get the main
    thread and must keep getting it -- silence would be a worse
    regression than the bug being fixed. `app/routers/chat.py:270` is the
    assistant's equivalent and simply uses the argument; it has been
    multi-thread from the start.
    """

    def _thread(thread_id: str | None) -> str:
        return thread_id or go_thread_id

    async def on_message(thread_id: str, role: str, content: str):
        """Process GO's response: execute actions, then persist clean text."""
        if not content or not content.strip():
            return
        thread_id = _thread(thread_id)

        # 1. Parse and execute any go-action blocks, get clean text
        try:
            clean_content = await process_go_response(pid, content)
        except Exception as exc:
            _log.error("GO response processing failed for %s: %s", pid, exc)
            clean_content = content  # fall back to raw content

        # 2. Persist the clean text (action blocks stripped)
        msg_id = str(uuid.uuid4())

        async with pool.acquire() as conn:
            async with conn.transaction():
                last_event_id = await _last_thread_event_id(conn, thread_id)
                # The GO agent is the sender; system-typed fallback if its
                # principal row is missing (never crash on the ledger NOT NULL).
                agent_actor_id, agent_actor_type = await agent_actor(go_agent_id)
                await append_event(
                    conn,
                    event_type=MESSAGE_POSTED,
                    payload={
                        "thread_id": thread_id,
                        "message_id": msg_id,
                        "sender": go_agent_id,
                        "role": "agent",
                        "content_preview": clean_content[:200],
                    },
                    actor_id=agent_actor_id,
                    actor_type=agent_actor_type,
                    correlation_id=thread_id,
                    causation_ids=[last_event_id] if last_event_id else None,
                    context_goal_id=pid,
                    context_conversation_id=thread_id,
                )
                await conn.execute(
                    """INSERT INTO messages (id, thread_id, sender, role, content)
                       VALUES ($1, $2, $3, 'agent', $4)""",
                    msg_id, thread_id, go_agent_id, clean_content,
                )

        # 3. Broadcast final message to all goal thread subscribers
        #    This ensures all members see the response even if streaming failed
        final_msg = json.dumps({
            "type": "message",
            "id": msg_id,
            "sender": go_agent_id,
            "sender_name": "Goal Orchestrator",
            "role": "agent",
            "content": clean_content,
        })
        await _broadcast_to_goal_thread(pid, final_msg, thread_id)

        # 3b. Increment unread for all goal members — the GO agent id is not a
        # human uid so the WHERE uid != $2 clause effectively matches everyone.
        # Non-critical: counter failure must never block the GO reply path.
        try:
            await increment_unread(pool, thread_id, sender_uid=go_agent_id)
        except Exception as exc:
            _log.warning(
                "goal-thread: increment_unread (GO reply) failed (non-fatal): %s", exc,
            )

        # 4. Mirror to all registered outbound channels (Slack, Notion, etc.).
        #    Skipped if clean_content is empty — means the whole response was
        #    action markup (e.g. the GO replied via reply_assistant only).
        if clean_content and clean_content.strip():
            from app.channels.base import relay_all_outbound
            await relay_all_outbound(pid, thread_id, clean_content, msg_id)

        # Refresh the rolling per-goal summary in the background (non-blocking;
        # self-gates at GO_SUMMARY_MSG_THRESHOLD GO-sent messages).
        import asyncio
        asyncio.create_task(_safe_update_goal_summary(pid))

        # Return the persisted message id so callers (openclaw.py) can
        # ship it in the token_stream_end broadcast.
        return msg_id

    async def on_notify(thread_id: str, agent_id: str, content: str):
        """Notify all goal members when GO posts a response."""
        thread_id = _thread(thread_id)
        member_uids = await pool.fetch(
            "SELECT uid FROM goal_members WHERE goal_id = $1", pid,
        )
        for row in member_uids:
            try:
                await push_notification(
                    row["uid"],
                    type="go_message",
                    title="Goal Orchestrator responded",
                    body=content[:120],
                    goal_id=pid,
                    thread_id=thread_id,
                    link=f"/goals/{pid}/thread",
                )
            except Exception:
                pass

    return on_message, on_notify


async def _safe_update_goal_summary(goal_id: str):
    """Background task: refresh the rolling per-goal summary if threshold reached.

    Best-effort and fully isolated — a failure here must never touch GO's
    reply path.
    """
    try:
        await maybe_update_goal_summary(goal_id)
    except Exception as exc:
        _log.debug("Goal summary update skipped for %s: %s", goal_id, exc)


async def _last_thread_event_id(conn, thread_id: str) -> str | None:
    """Get the most recent ledger event_id for this thread (for causation chain)."""
    row = await conn.fetchrow(
        """SELECT event_id FROM ledger_events
           WHERE context_conversation_id = $1
           ORDER BY timestamp DESC LIMIT 1""",
        thread_id,
    )
    return str(row["event_id"]) if row else None
