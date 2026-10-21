"""Chat WebSocket bridge + thread management."""
import logging
import uuid
import json

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_agent_actor_id, get_goal_role, verify_firebase_token
from app.database import get_pool
from app.flash import flash_redirect
from app.notifications import push_notification
from app.unread import increment_unread, mark_thread_read
from app.openclaw import session_manager, gateway_admin, make_task_session_key, make_goal_session_key
from app.ledger.journal import append_event
from app.ledger.actors import SYSTEM_ACTOR_ID
from app.ledger.schema import MESSAGE_POSTED, THREAD_REFRESHED, THREAD_DELETED, QUESTION_ANSWERED, TASK_STATUS_CHANGED
from app.ledger.payloads import question_answered_payload, task_status_changed_payload
from app.task_controller import emit_task_event, get_task_correlation_id, get_last_event_id

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _guard_thread_in_goal(pool, thread_id: str, pid: str) -> None:
    """Cross-tenant IDOR guard for caller-supplied thread ids.

    get_goal_role(uid, pid) only proves the caller is a member of the URL's
    goal `pid` — NOT that `thread_id` belongs to `pid`. Without this, a member
    of ANY goal could pass a foreign thread_id and read/drive/destroy another
    tenant's conversation. Mirrors app/routers/git_graph.py's
    `entity.goal_id != pid → 404` gate. No-op when the thread doesn't exist
    (callers handle not-found naturally); the job here is to block access to a
    thread that exists in a DIFFERENT goal.
    """
    if not thread_id:
        return
    owner = await pool.fetchval("SELECT goal_id FROM threads WHERE id = $1", thread_id)
    if owner is not None and owner != pid:
        raise HTTPException(status_code=404, detail="thread not found")


# ── Assistant/IA chat message pagination ─────────────────────────────────────
#
# A long-lived assistant thread accumulates thousands of messages; rendering
# them all produced a multi-MB page that hung the browser. The page now renders
# only the newest window and pages backwards on demand — the same keyset design
# the read-only channels use (app/routers/thread_paging.py), but with the chat's
# own richer row shape (sender_name join, state-context stripping, card_data).
# The `messages` id is TEXT and created_at TIMESTAMPTZ, so the cursor is the
# tuple (created_at, id) — created_at alone is not unique and would skip/dup a
# row across a page boundary.

_CHAT_MSG_SELECT = """
    SELECT m.*,
           COALESCE(u.display_name, ag.name, m.sender) AS sender_name
      FROM messages m
      LEFT JOIN users  u  ON u.uid = m.sender
      LEFT JOIN agents ag ON ag.id = m.sender
     WHERE m.thread_id = $1
       AND m.kind <> 'system_inject'
       AND NOT (m.kind = 'system_wake' AND m.sender = 'system' AND m.card_data IS NULL)
"""


def _process_chat_rows(rows) -> list[dict]:
    """Strip the appended state-context block from human messages (it's for the
    LLM only) and parse card_data JSONB (asyncpg hands it back as a str) so the
    template's `is mapping` branches match. A malformed card degrades to a plain
    row rather than breaking the whole render."""
    from app.services.story_state import strip_state_context
    messages = []
    for row in rows:
        m = dict(row)
        if m.get("role") == "human":
            m["content"] = strip_state_context(m.get("content") or "")
        cd = m.get("card_data")
        if isinstance(cd, str) and cd:
            try:
                m["card_data"] = json.loads(cd)
            except Exception:
                pass
        messages.append(m)
    return messages


async def _fetch_chat_page(pool, thread_id: str, before_id: str = "") -> tuple[list[dict], bool]:
    """Return (messages ascending, has_older) for a chat thread.

    `before_id` empty → the newest window; otherwise the page immediately older
    than that message. Each query fetches `limit + 1` rows so the extra oldest
    "probe" row proves whether more history exists without a COUNT(*). An unknown
    `before_id` (stale tab / deleted message) yields an empty terminal page."""
    from app.routers.thread_paging import (
        INITIAL_MESSAGE_LIMIT, EARLIER_PAGE_SIZE, split_probe_row,
    )
    if before_id:
        anchor = await pool.fetchrow(
            "SELECT created_at, id FROM messages WHERE id = $1 AND thread_id = $2",
            before_id, thread_id,
        )
        if anchor is None:
            return [], False
        rows = await pool.fetch(
            f"""SELECT * FROM ({_CHAT_MSG_SELECT}
                   AND (m.created_at, m.id) < ($2, $3)
                   ORDER BY m.created_at DESC, m.id DESC
                   LIMIT $4
               ) sub ORDER BY created_at ASC, id ASC""",
            thread_id, anchor["created_at"], anchor["id"], EARLIER_PAGE_SIZE + 1,
        )
        page, has_older = split_probe_row(rows, EARLIER_PAGE_SIZE)
    else:
        rows = await pool.fetch(
            f"""SELECT * FROM ({_CHAT_MSG_SELECT}
                   ORDER BY m.created_at DESC, m.id DESC
                   LIMIT $2
               ) sub ORDER BY created_at ASC, id ASC""",
            thread_id, INITIAL_MESSAGE_LIMIT + 1,
        )
        page, has_older = split_probe_row(rows, INITIAL_MESSAGE_LIMIT)
    return _process_chat_rows(page), has_older


async def _maybe_generate_thread_title(pool, thread_id: str, pid: str, agent_id: str):
    """Auto-generate a thread title after 6 messages if still 'New conversation'."""
    try:
        thread = await pool.fetchrow("SELECT title FROM threads WHERE id = $1", thread_id)
        if not thread or thread["title"] != "New conversation":
            return  # Already has a title

        msg_count = await pool.fetchval(
            "SELECT COUNT(*) FROM messages WHERE thread_id = $1", thread_id,
        )
        if msg_count < 6:
            return  # Not enough messages yet

        # Get messages for title generation
        msgs = await pool.fetch(
            "SELECT role, content FROM messages WHERE thread_id = $1 ORDER BY created_at LIMIT 10",
            thread_id,
        )

        # Get existing titles to avoid duplicates
        existing = await pool.fetch(
            "SELECT title FROM threads WHERE goal_id = $1 AND agent_id = $2 AND title != 'New conversation'",
            pid, agent_id,
        )
        existing_titles = [r["title"] for r in existing]

        from app.gemini_client import generate_thread_title
        title = await generate_thread_title(
            [dict(m) for m in msgs],
            existing_titles=existing_titles,
        )
        if title:
            await pool.execute(
                "UPDATE threads SET title = $1 WHERE id = $2 AND title = 'New conversation'",
                title, thread_id,
            )
            logger.info("Auto-titled thread %s: %s", thread_id[:12], title)
    except Exception as exc:
        logger.debug("Thread title generation failed for %s: %s", thread_id[:12], exc)


async def _get_goal_context(pool, pid: str, agent_id: str) -> dict:
    """
    Build the context payload forwarded to the gateway.

    Task-scope agents receive only their own assigned tasks.
    Goal-scope agents receive the full goal snapshot.
    """
    ctx_row = await pool.fetchrow(
        "SELECT * FROM agent_contexts WHERE goal_id = $1 AND agent_id = $2",
        pid, agent_id,
    )
    ctx = dict(ctx_row) if ctx_row else {}

    agent_row = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    agent = dict(agent_row) if agent_row else {}

    base = {
        "agent_id": agent_id,
        "goal_id": pid,
        "agent_type": agent.get("type", "IA"),
        "scope": agent.get("scope", "task"),
        "model": agent.get("model", "google/gemini-3.8-flash"),
        "additional_instructions": ctx.get("additional_instructions", ""),
        "tools_allow": agent.get("tools_allow", []),
        "tools_deny": agent.get("tools_deny", []),
        "memory_enabled": agent.get("memory_enabled", False),
        "max_tokens": agent.get("max_tokens"),
        "workspace_files": ctx.get("workspace_files", []),
        "skills": ctx.get("skills", []),
    }

    if agent.get("scope") == "goal":
        base["goal_snapshot"] = await _build_goal_snapshot(pool, pid)
    else:
        base["assigned_tasks"] = await _build_assigned_tasks(pool, pid, agent_id)

    return base


async def _build_goal_snapshot(pool, pid: str) -> dict:
    """Full goal state for a goal-scope (coordinator) agent."""
    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row) if proj_row else {}

    task_rows = await pool.fetch(
        """SELECT id, title, state, assigned_to, template_id, instance_id,
                  priority, depends_on, parent_task_id, result, blocked_reason,
                  acceptance_criteria
           FROM tasks WHERE goal_id = $1 AND status = 'active'
           ORDER BY created_at""",
        pid,
    )
    tasks = [dict(r) for r in task_rows]

    agent_rows = await pool.fetch(
        """SELECT a.id, a.name, a.type, a.scope
           FROM agent_contexts ac
           JOIN agents a ON a.id = ac.agent_id
           WHERE ac.goal_id = $1""",
        pid,
    )
    agents = [dict(r) for r in agent_rows]

    return {
        "name": goal.get("name", ""),
        "description": goal.get("description", ""),
        "tasks": tasks,
        "agents": agents,
    }


async def _build_assigned_tasks(pool, pid: str, agent_id: str) -> list:
    """Only the tasks assigned to this agent -- for task-scope workers."""
    rows = await pool.fetch(
        """SELECT id, title, description, state, priority, depends_on, thread_id
           FROM tasks
           WHERE goal_id = $1 AND assigned_to = $2 AND status = 'active'""",
        pid, agent_id,
    )
    return [dict(r) for r in rows]


async def _last_thread_event_id(conn, thread_id: str) -> str | None:
    """Return the event_id of the most recent ledger event for this thread."""
    row = await conn.fetchval(
        """SELECT event_id FROM ledger_events
           WHERE payload->>'thread_id' = $1
           ORDER BY sequence_number DESC LIMIT 1""",
        thread_id,
    )
    return str(row) if row else None


def _make_callbacks(pool, pid: str, agent_id: str):
    """
    Returns the two callbacks injected into _AgentSession:

    on_message -- persists a completed agent reply to PostgreSQL.
    on_notify  -- fires a user notification so the browser learns about the
                 reply even if it was disconnected during the task.
    """
    async def on_message(thread_id: str, role: str, content: str):
        msg_id = str(uuid.uuid4())
        agent_actor_id = await get_agent_actor_id(agent_id)
        # Registered system actor as fallback (never an unresolvable all-zero id).
        agent_actor_id = agent_actor_id or SYSTEM_ACTOR_ID

        # Read thread's correlation_id (cast — asyncpg UUID ≠ stdlib UUID)
        _raw_corr = await pool.fetchval(
            "SELECT correlation_id FROM threads WHERE id = $1", thread_id,
        )
        thread_corr_id = str(_raw_corr) if _raw_corr else None

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Last event in this thread → causation for the agent reply
                last_event_id = await _last_thread_event_id(conn, thread_id)

                # WHO THE ASSISTANT IS ACTING FOR. An assistant message
                # attributed only to the assistant is attributable to a robot
                # and to nobody — the same dead end `system` attribution was.
                # An SHCA is per-(goal, user) by construction, so this is a
                # fact about the instance rather than a guess from who last
                # spoke. Returns None when the thread has no human owner
                # (`engine` and `go-*` own threads too), and None is left
                # unset rather than filled with an invented principal.
                from app.ledger.actors import human_principal_for_thread

                acting_for = await human_principal_for_thread(conn, thread_id)

                await append_event(
                    conn,
                    event_type=MESSAGE_POSTED,
                    payload={
                        "message_id": msg_id,
                        "thread_id": thread_id,
                        "sender": agent_id,
                        "role": role,
                        "content": content,
                    },
                    actor_id=agent_actor_id,
                    actor_type="shca",
                    on_behalf_of=acting_for,
                    correlation_id=thread_corr_id,
                    causation_ids=[last_event_id] if last_event_id else None,
                    context_goal_id=pid,
                    context_conversation_id=thread_id,
                )
                await conn.execute(
                    """INSERT INTO messages (id, thread_id, sender, role, content)
                       VALUES ($1, $2, $3, $4, $5)""",
                    msg_id, thread_id, agent_id, role, content,
                )

                # Update task result with the agent's latest reply
                # so SHCA (goal-scope) can see what each agent did via goal_snapshot
                if role == "agent":
                    await conn.execute(
                        "UPDATE tasks SET result = $1 WHERE thread_id = $2 AND status = 'active'",
                        content[:2000], thread_id,
                    )

        # Mirror to all registered outbound channels (Slack, Notion, etc.).
        # Placed here — not in openclaw.py's chat:final handler — so it fires
        # on ALL paths that call on_message: the primary chat:final path AND
        # the transcript-recovery path (_retry_fetch_response) AND the
        # "envelope lost" fallback. Symmetric with GO's goal_thread.py hook.
        if role == "agent" and content and content.strip():
            from app.channels.base import relay_all_outbound
            await relay_all_outbound(pid, thread_id, content, msg_id)

        # Increment unread for the thread owner when the agent replies.
        # The agent_id is not a human uid so all participants (just the thread
        # owner for SHCA) get their counter incremented.
        # Non-critical: counter failure must never block the agent reply path.
        if role == "agent":
            try:
                await increment_unread(pool, thread_id, sender_uid=agent_id)
            except Exception as exc:
                logger.warning(
                    "chat: increment_unread (agent reply) failed (non-fatal): %s", exc,
                )

        # Return the persisted message id so callers (openclaw.py) can
        # ship it in the token_stream_end broadcast — lets the browser
        # tag the live-rendered bubble with its canonical id and keep
        # `lastMsgId` honest, avoiding stale-poll duplicate renders.
        return msg_id

    async def on_notify(thread_id: str, agent_id_inner: str, content: str):
        creator_uid = await pool.fetchval(
            "SELECT created_by FROM threads WHERE id = $1", thread_id,
        )
        if not creator_uid:
            return

        preview = content[:80] + ("..." if len(content) > 80 else "")

        await push_notification(
            creator_uid,
            type="agent_reply",
            title=f"{agent_id_inner} replied",
            body=preview,
            goal_id=pid,
            agent_id=agent_id_inner,
            thread_id=thread_id,
            link=f"/goals/{pid}/chat/{agent_id_inner}?thread_id={thread_id}",
        )

    return on_message, on_notify


# -- Chat redirect (find SHCA for this goal) -----------------------------------

@router.get("/goals/{pid}/chat", response_class=HTMLResponse)
async def chat_redirect(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Redirect to the SHCA's chat page for this goal."""
    pool = get_pool()
    shca = await pool.fetchval(
        """SELECT a.id FROM agents a
           JOIN agent_contexts ac ON a.id = ac.agent_id
           WHERE ac.goal_id = $1 AND a.type = 'SHCA' AND a.status = 'active'
           LIMIT 1""",
        pid,
    )
    if shca:
        return RedirectResponse(url=f"/goals/{pid}/chat/{shca}", status_code=303)
    # No SHCA provisioned yet is a recoverable/transient state (it auto-provisions on
    # first use elsewhere), not a hard failure — this is a full-page nav, so raising
    # here would render as a raw JSON body instead of navigating back with a toast.
    return flash_redirect(f"/goals/{pid}", "No Assistant agent found for this goal yet.", flash_type="warning")


# -- Chat page -----------------------------------------------------------------

@router.get("/goals/{pid}/chat/{agent_id}", response_class=HTMLResponse)
async def chat_page(
    pid: str,
    agent_id: str,
    request: Request,
    thread_id: str | None = None,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Validate thread_id if provided
    is_read_only = False
    if thread_id:
        thread_row = await pool.fetchrow(
            "SELECT id, goal_id, participants, created_by, left_goal_at FROM threads WHERE id = $1",
            thread_id,
        )
        # not found OR belongs to another goal (IDOR) → start fresh rather than
        # leak/serve a foreign thread. get_goal_role above only gates pid.
        if not thread_row or thread_row["goal_id"] != pid:
            thread_id = None
        else:
            # Check participant access: if thread has participants, user must be one
            participants = thread_row.get("participants") or []
            created_by = thread_row.get("created_by", "")
            if participants and user["uid"] not in participants and user["uid"] != created_by:
                # Full-page nav — redirect back to the agent's default thread with a
                # toast rather than raising (which would render as a raw JSON body).
                return flash_redirect(
                    f"/goals/{pid}/chat/{agent_id}",
                    "You are not a participant in that conversation.",
                    flash_type="warning",
                )
            # Read-only: thread's owner left the goal (see threads_left_goal_at
            # migration / remove_goal_member). UI hides the send box; the WS
            # handler enforces this server-side regardless (THREAD_READ_ONLY).
            is_read_only = thread_row.get("left_goal_at") is not None

    # If no thread_id, check for existing threads instead of auto-creating
    if not thread_id:
        # The GENERAL thread first — the one with no story on it. Story threads
        # have their own rows in the sidebar and their own links; landing on
        # whichever happens to be newest means "# assistant" opens a different
        # story from one visit to the next, which is the one thing a fixed link
        # must not do.
        existing = await pool.fetchval(
            """SELECT id FROM threads
               WHERE goal_id = $1 AND agent_id = $2 AND task_id IS NULL
               ORDER BY (story_id IS NULL OR story_id = '') DESC,
                        created_at DESC
               LIMIT 1""",
            pid, agent_id,
        )
        if not existing:
            # F7 — CREATE ONE. Asked for directly: "the user should be
            # allowed to open an assistant session even without a story
            # or errand."
            #
            # This branch used to fall through and render the page with
            # NO thread, so the client connected to the empty string and
            # sat on "connecting" for ever. An assistant thread was only
            # ever created as a side effect of a STORY, so when the story
            # ended you were left with an assistant you could not reach
            # and no way to make one.
            #
            # The capability was always meant to exist -- CLAUDE.md
            # describes a "general-mode conversation" with no story
            # linked -- and the code to create it existed too, in the
            # SLACK path (app/routers/assistant.py). So a Slack user got
            # a general conversation and a web user did not.
            #
            # The same columns that creator writes, so this is the row
            # the product would have made.
            agent_name = await pool.fetchval(
                "SELECT name FROM agents WHERE id = $1", agent_id) or agent_id
            existing = str(uuid.uuid4())
            await pool.execute(
                "INSERT INTO threads (id, goal_id, agent_id, created_by, "
                "title, correlation_id) VALUES ($1,$2,$3,$4,$5,$6)",
                existing, pid, agent_id, user["uid"], agent_name,
                str(uuid.uuid4()))
            logger.info("goal %s: opened a general assistant thread %s for %s",
                      pid, existing, user["uid"])
        if existing:
            return RedirectResponse(
                url=f"/goals/{pid}/chat/{agent_id}?thread_id={existing}",
                status_code=303,
            )

    # Load the newest window of messages (empty if no thread_id -- empty state).
    # A long-lived thread accumulates thousands of messages; rendering them all
    # server-side produced a multi-MB page that hung the browser. We render only
    # the newest THREAD_INITIAL_MESSAGE_LIMIT and page backwards on demand via
    # the "Load earlier" sentinel (see _fetch_chat_page + messages_earlier).
    messages: list[dict] = []
    has_older = False
    if thread_id:
        messages, has_older = await _fetch_chat_page(pool, thread_id)
    oldest_message_id = str(messages[0]["id"]) if messages else ""

    # Sidebar threads -- exclude task threads
    thread_rows = await pool.fetch(
        """SELECT * FROM threads
           WHERE goal_id = $1 AND agent_id = $2 AND task_id IS NULL
           ORDER BY created_at DESC LIMIT 20""",
        pid, agent_id,
    )
    threads = [dict(r) for r in thread_rows]

    agent_row = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    agent = dict(agent_row) if agent_row else {"id": agent_id, "name": agent_id}

    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    # Resolve the story currently associated with this thread (operator-mode
    # workspace lookup). The right-rail file viewer uses this to show files
    # from <agent-workspace>/stories/<sid>/.
    current_story = None
    if thread_id:
        # Right-rail workspace panel: pick the story to show for this thread.
        # Exclude terminal states, and PREFER the actively-worked story over a
        # lingering 'reviewed' one. 'reviewed' = a done-and-council-reviewed story
        # (story_transitions groups it with 'done' as completed) that is NOT in the
        # terminal exclusion set — so ordering by pure updated_at let a recent
        # reviewed story shadow an older-but-still-in_progress story on the same
        # thread (the workspace panel showed the wrong story). Rank active
        # (non-reviewed) first, then newest; a reviewed story still wins if it's
        # the only non-terminal one on the thread.
        story_row = await pool.fetchrow(
            # `sandbox_size` IS READ BY THE MACHINE ROW, and leaving it out
            # of this SELECT is why changing the machine here looked like it
            # did nothing. The POST saved correctly every time; the page then
            # re-rendered from this row, found no `sandbox_size` attribute,
            # and selected "default for this work" -- so the control reset
            # itself on every reload and reported the opposite of the truth.
            # A control that lies about the stored value is worse than one
            # that refuses, because the second attempt looks identical.
            """SELECT id, title, execution_mode, current_phase, workspace_state,
                      workspace_path, shca_instance_id, execution_paused,
                      current_plan_path, current_plan_id, sandbox_size
                 FROM stories
                WHERE goal_id = $1 AND active_thread_id = $2
                  AND state NOT IN ('done', 'cancelled', 'rejected')
                ORDER BY (state = 'reviewed') ASC, updated_at DESC
                LIMIT 1""",
            pid, thread_id,
        )
        from app.services.machine_lock import locked_reason
        if story_row:
            current_story = dict(story_row)
            # 2026-08-20: `workspace_state == 'ready'` is a legitimate, common
            # outcome of a PARTIAL multi-repo provision — see
            # `is_write_repo_set_incomplete`'s docstring. Without this flag the
            # workspace panel below renders as if every granted repo is present,
            # with no indication a repo is still landing (or, if the branch-ready
            # catch-up has exhausted its retries, silently missing). Cheap here:
            # exactly one story per page load, not a list view.
            current_story["workspace_repos_incomplete"] = False
            if current_story.get("workspace_state") == "ready":
                from app.services.operator_provisioning import is_write_repo_set_incomplete
                try:
                    current_story["workspace_repos_incomplete"] = (
                        await is_write_repo_set_incomplete(pool, pid, current_story["id"])
                    )
                except Exception:
                    logger.warning(
                        "chat: is_write_repo_set_incomplete check failed for story=%s "
                        "(non-fatal)", current_story["id"],
                    )

    firebase_token = request.cookies.get("firebase_token", "")

    # Mark the SHCA thread as read for this user — zeroes their unread counter.
    # Non-critical: counter failure must never break page load.
    if thread_id:
        try:
            await mark_thread_read(pool, thread_id, user["uid"])
        except Exception as exc:
            logger.warning("chat: mark_thread_read failed (non-fatal): %s", exc)

    # Scoped to SHCA (Assistant) agents only - this page also renders IA/GO
    # chat views, which this milestone's copy doesn't apply to.
    auto_fire_tour = False
    if agent.get("type") == "SHCA":
        from app.services.tour_progress import check_and_mark_milestone_if_enabled
        auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "assistant-thread-intro")

    return templates.TemplateResponse(
        "chat/index.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "agent": agent,
            "thread_id": thread_id or "",
            "messages": messages,
            "has_older": has_older,
            "oldest_message_id": oldest_message_id,
            "load_earlier_url": (
                f"/goals/{pid}/chat/{agent_id}/thread/{thread_id}/messages/earlier"
                if thread_id else ""
            ),
            "threads": threads,
            "current_story": current_story,
            # Whether the machine can still be changed, or why not. The
            # same helper guards the endpoint; this only decides whether
            # the control is greyed out.
            "machine_locked": (
                await locked_reason(pool, current_story["id"])
                if current_story else None),
            "firebase_token": firebase_token,
            "is_read_only": is_read_only,
            "auto_fire_tour": auto_fire_tour,
        },
    )


# -- Load earlier messages (backwards pagination) ------------------------------

@router.get(
    "/goals/{pid}/chat/{agent_id}/thread/{tid}/messages/earlier",
    response_class=HTMLResponse,
)
async def messages_earlier(
    pid: str, agent_id: str, tid: str,
    request: Request,
    before_id: str = "",
    user: dict = Depends(get_current_user),
):
    """Return the page of messages older than `before_id` as an HTML fragment
    (next sentinel + rows), reusing chat/_message.html so paged-in rows are
    byte-identical to the full render. HTML, not JSON — a JSON endpoint would
    need a second client-side copy of the bubble markup that would diverge."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)
    pool = get_pool()
    await _guard_thread_in_goal(pool, tid, pid)  # cross-tenant IDOR guard

    messages, has_older = await _fetch_chat_page(pool, tid, before_id)
    return templates.TemplateResponse(
        "chat/_earlier_messages.html",
        {
            "request": request,
            "messages": messages,
            "has_older": has_older,
            # Next cursor: the oldest row in THIS page.
            "oldest_message_id": str(messages[0]["id"]) if messages else "",
            "load_earlier_url": f"/goals/{pid}/chat/{agent_id}/thread/{tid}/messages/earlier",
        },
    )


# -- Add participant to thread -------------------------------------------------

@router.post("/goals/{pid}/chat/{agent_id}/thread/{tid}/add-participant")
async def add_thread_participant(
    pid: str, agent_id: str, tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Add a goal member to an existing conversation thread."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    participant_uid = form.get("participant_uid", "").strip()
    if not participant_uid:
        raise HTTPException(status_code=400, detail="participant_uid required")

    pool = get_pool()

    # Verify the target user is a goal member
    is_member = await pool.fetchval(
        "SELECT 1 FROM goal_members WHERE goal_id = $1 AND uid = $2", pid, participant_uid,
    )
    if not is_member:
        raise HTTPException(status_code=400, detail="User is not a member of this goal")

    # Add to participants array
    await pool.execute("""
        UPDATE threads
        SET participants = array_append(
            CASE WHEN participants IS NULL THEN '{}' ELSE participants END,
            $1
        )
        WHERE id = $2 AND goal_id = $3
          AND NOT ($1 = ANY(COALESCE(participants, '{}')))
    """, participant_uid, tid, pid)

    # Also ensure the creator is in participants (backfill)
    thread = await pool.fetchrow("SELECT created_by, participants FROM threads WHERE id = $1", tid)
    if thread and thread["created_by"] and thread["created_by"] not in (thread["participants"] or []):
        await pool.execute("""
            UPDATE threads SET participants = array_append(
                CASE WHEN participants IS NULL THEN '{}' ELSE participants END, $1
            ) WHERE id = $2
        """, thread["created_by"], tid)

    logger.info("Added participant %s to thread %s in goal %s", participant_uid, tid, pid)

    # Mirror it into Slack now rather than waiting up to 15 minutes for the
    # reconcile pass. kick=False: this path knows somebody was ADDED, and
    # walking the channel to remove strangers is the reconciler's job.
    try:
        from app.channels.slack.membership import sync_channel_members
        await sync_channel_members(pool, tid, kick=False)
    except Exception as _slack_exc:
        logger.warning("Slack membership sync for thread %s failed: %s",
                       tid, _slack_exc)

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}?thread_id={tid}",
        status_code=303,
    )


# -- Chat-page file upload (JSON) ---------------------------------------------
# Uploads from the chat page become goal-wide read-only documents that every
# active session in the goal can immediately reference.

@router.post("/goals/{pid}/chat/upload")
async def chat_upload_file(
    pid: str,
    request: Request,
    file: UploadFile = File(...),
    sender_agent: str = Form(""),  # 'go', 'shca', or empty
    user: dict = Depends(get_current_user),
):
    """Upload a file from the chat page → register as goal_resource (kind=document)
    → refresh ALL active sessions in the goal → notify GO via orchestrator channel.

    Returns JSON: {ok, resource_id, name, file_size, refresh_summary}.
    """
    import hashlib

    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403, detail="Not a goal member")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Reuse the same file-type allowlist as the resource browser
    from app.routers.goal_resources import ALLOWED_FILE_TYPES, MAX_UPLOAD_SIZE
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: .{ext}")

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024} MB)")

    content_hash = hashlib.sha256(data).hexdigest()
    resource_name = file.filename

    # ── A NAME THAT ALREADY EXISTS IS A REVISION, NOT AN ERROR ────────
    #
    # `uq_goal_resource_kind_name UNIQUE (goal_id, kind, name)` made the
    # plain INSERT below raise, uncaught, and a person re-uploading a
    # revised price list got an internal error with no message. Reusing
    # the existing id keeps the blob path, the grants and every story
    # link intact, so the revision reaches everyone already holding the
    # document with nothing to re-grant. See services/resource_upsert.
    from app.services.resource_upsert import (existing_document,
                                              record_replacement,
                                              replace_document)

    pool = get_pool()
    prior = await existing_document(pool, pid, resource_name)
    resource_id = prior["id"] if prior else str(uuid.uuid4())[:12]

    logger.info(
        "chat_upload_file: goal=%s user=%s sender_agent=%s file=%s ext=%s "
        "size=%d %s",
        pid, user.get("email", user["uid"]), sender_agent or "-",
        file.filename, ext, len(data),
        f"REPLACING {resource_id} (was {prior['file_size_bytes']} bytes)"
        if prior else f"new resource {resource_id}",
    )

    # Upload to GCS. Same object path when replacing, so the bytes behind
    # the resource change and nothing that points at it has to move.
    from app.storage import upload_resource_file
    gcs_uri = await upload_resource_file(
        resource_id, file.filename, data,
        content_type=ALLOWED_FILE_TYPES.get(ext, "application/octet-stream"),
    )

    # Register as goal resource (read-only, no story link → goal-wide)
    via = f"chat:{sender_agent or 'unknown'}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=("GOAL_RESOURCE_UPDATED" if prior
                            else "GOAL_RESOURCE_REGISTERED"),
                payload={
                    "resource_id": resource_id, "kind": "document",
                    "name": resource_name, "file_type": ext,
                    "file_size": len(data),
                    "replaced_in_place": bool(prior),
                    "uploaded_via": via,
                },
                actor_id=user["actor_id"], context_goal_id=pid,
            )
            if prior:
                await replace_document(conn, resource_id, pid, resource_name,
                                       gcs_uri, ext, len(data))
            else:
                await conn.execute("""
                    INSERT INTO goal_resources
                        (id, goal_id, kind, name, uri, created_by, human_context,
                         file_type, file_size_bytes, gcs_uri, summary_stale)
                    VALUES ($1, $2, 'document', $3, $4, $5, $6, $7, $8, $9, true)
                """, resource_id, pid, resource_name, gcs_uri, user["uid"],
                    f"Uploaded by {user.get('email', user['uid'])} via chat",
                    ext, len(data), gcs_uri)
            try:
                await conn.execute("""
                    INSERT INTO resource_attachments
                        (resource_id, goal_id, filename, file_type, file_size_bytes,
                         gcs_uri, uploaded_by, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """, resource_id, pid, file.filename, ext, len(data),
                    gcs_uri, user["uid"], content_hash)
            except Exception as exc:
                logger.warning("Failed to insert resource_attachments row: %s", exc)
            if prior:
                await record_replacement(
                    conn, resource_id, pid, user["uid"], file.filename, ext,
                    len(data), prior["file_size_bytes"], via)
            else:
                await conn.execute("""
                    INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                    VALUES ($1, $2, 'created', $3, $4)
                """, resource_id, pid, user["uid"],
                    json.dumps({
                        "filename": file.filename, "type": ext,
                        "size": len(data), "uploaded_via": via,
                    }))

    # Refresh every active session in the goal so they all see the new doc
    refresh_summary = {"go": False, "shcas": 0, "ia_workers": 0}
    try:
        from app.resource_provisioning import refresh_all_goal_sessions
        refresh_summary = await refresh_all_goal_sessions(pid)
    except Exception as exc:
        logger.warning("refresh_all_goal_sessions failed for goal %s: %s", pid, exc)

    # Notify GO via orchestrator channel (always — even if uploaded TO GO,
    # the channel is the team-visible record)
    try:
        goal = await pool.fetchrow(
            "SELECT orchestrator_thread_id FROM goals WHERE id = $1", pid,
        )
        if goal and goal["orchestrator_thread_id"]:
            uploader = user.get("display_name") or user.get("email") or user["uid"]
            sender_label = {
                "go": "while chatting with the GO",
                "shca": "while chatting with their assistant",
            }.get(sender_agent, "from the chat page")
            # A REPLACEMENT IS NOT AN UPLOAD, and the channel is where a
            # team finds out the contract moved under them. "Uploaded
            # PRICE_LIST.xlsx" reads as a new document; what actually
            # happened is that every story holding the old one is now
            # holding a different file.
            notify_msg = (
                f"[System] **{uploader}** replaced **{resource_name}** "
                f"({ext.upper()}, {len(data) // 1024} KB, was "
                f"{(prior['file_size_bytes'] or 0) // 1024} KB) "
                f"{sender_label}. Same resource, new contents — every "
                f"story already holding it now reads the new version."
                if prior else
                f"[System] **{uploader}** uploaded **{resource_name}** "
                f"({ext.upper()}, {len(data) // 1024} KB) {sender_label}. "
                f"Now available to all agents as a read-only goal resource."
            )
            msg_id = str(uuid.uuid4())
            await pool.execute(
                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                   VALUES ($1, $2, 'system', 'human', $3, 0)""",
                msg_id, goal["orchestrator_thread_id"], notify_msg,
            )
            try:
                from app.routers.orchestrator import _broadcast_to_orchestrator
                await _broadcast_to_orchestrator(pid, json.dumps({
                    "type": "message",
                    "message": {
                        "id": msg_id, "sender": "system", "role": "human",
                        "content": notify_msg,
                    },
                }))
            except Exception as exc:
                logger.warning("orchestrator broadcast failed: %s", exc)
            logger.info(
                "Posted upload notification to orchestrator channel: goal=%s rid=%s",
                pid, resource_id,
            )
    except Exception as exc:
        logger.warning("Failed to notify GO of upload: %s", exc)

    # Trigger background scan (text extraction for PDFs, etc.)
    try:
        from app.routers.goal_resources import _background_doc_scan
        import asyncio
        asyncio.create_task(_background_doc_scan(resource_id, pid, gcs_uri, ext, resource_name))
    except Exception as exc:
        logger.warning("Failed to schedule background scan: %s", exc)

    # Notify Notion page for the relevant chat thread.
    try:
        from app.channels.notion.outbound import relay_file_upload as _notion_file_relay
        import asyncio as _asyncio_notion_file
        _uploader_name = user.get("display_name") or user.get("email") or user["uid"]
        _asyncio_notion_file.create_task(
            _notion_file_relay(pid, user["uid"], _uploader_name, resource_name, len(data) // 1024, ext, sender_agent)
        )
    except Exception as exc:
        logger.warning("Notion file relay scheduling failed (goal=%s): %s", pid, exc)

    return JSONResponse({
        "ok": True,
        "resource_id": resource_id,
        "name": resource_name,
        "file_type": ext,
        "file_size": len(data),
        "gcs_uri": gcs_uri,
        "refresh": refresh_summary,
    })


# -- Search goal members for adding to thread ---------------------------------

@router.get("/goals/{pid}/chat/members/search")
async def search_goal_members_for_thread(
    pid: str,
    q: str = "",
    exclude_thread: str = "",
    user: dict = Depends(get_current_user),
):
    """Search goal members for adding to a conversation thread.

    Returns goal members matching query, excluding those already in the thread.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    if len(q) < 1:
        # Return all goal members if no query
        pool = get_pool()
        rows = await pool.fetch("""
            SELECT gm.uid, gm.role, u.display_name, u.email FROM goal_members gm
            JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1
            ORDER BY u.display_name
        """, pid)
    else:
        pool = get_pool()
        rows = await pool.fetch("""
            SELECT gm.uid, gm.role, u.display_name, u.email FROM goal_members gm
            JOIN users u ON u.uid = gm.uid
            WHERE gm.goal_id = $1
              AND (u.display_name ILIKE $2 OR u.email ILIKE $2)
            ORDER BY u.display_name
        """, pid, f"%{q}%")

    # Exclude members already in the thread
    existing_participants = set()
    if exclude_thread:
        thread = await pool.fetchrow(
            "SELECT participants, created_by FROM threads WHERE id = $1", exclude_thread,
        )
        if thread:
            existing_participants = set(thread["participants"] or [])
            if thread["created_by"]:
                existing_participants.add(thread["created_by"])

    results = [
        {"uid": r["uid"], "name": r["display_name"] or r["email"], "email": r["email"], "role": r["role"]}
        for r in rows
        if r["uid"] not in existing_participants
    ]

    return JSONResponse({"results": results})


# -- Import task into conversation ---------------------------------------------

@router.get("/goals/{pid}/chat/tasks-for-import")
async def tasks_for_import(
    pid: str,
    user: dict = Depends(get_current_user),
):
    """List stories and tasks available for import into SHCA conversation.

    Prioritizes stories assigned to the current user in accepted/in_progress state.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        return JSONResponse({"tasks": [], "error": "Not a goal member"}, status_code=403)

    pool = get_pool()

    # Stories assigned to this user (from stories table)
    story_rows = await pool.fetch("""
        SELECT s.id, s.title, s.state, s.priority, s.assigned_to,
               s.description, s.acceptance_criteria, 'story' AS category
        FROM stories s
        WHERE s.goal_id = $1
          AND s.assigned_to = $2
          AND s.state IN ('accepted', 'in_progress', 'awaiting_merge')
        ORDER BY
            CASE state WHEN 'in_progress' THEN 1 ELSE 2 END,
            s.created_at DESC
    """, pid, user["uid"])

    # Also include regular top-level tasks (backward compat)
    task_rows = await pool.fetch("""
        SELECT t.id, t.title, t.state, t.priority, t.assigned_to,
               t.description, 'task' AS category, t.parent_task_id
        FROM tasks t
        WHERE t.goal_id = $1 AND t.status = 'active'
          AND t.parent_task_id IS NULL
        ORDER BY t.created_at DESC
        LIMIT 20
    """, pid)

    tasks = []
    for r in [*story_rows, *task_rows]:
        task = dict(r)
        subs = await pool.fetch("""
            SELECT id, title, state, priority, assigned_to FROM tasks
            WHERE parent_task_id = $1 AND status = 'active'
            ORDER BY created_at
        """, task["id"])
        task["subtasks"] = [dict(s) for s in subs]
        task["description_preview"] = (task.get("description") or "")[:200]
        tasks.append(task)

    return JSONResponse({"tasks": tasks})


async def _build_story_briefing(pool, pid, task_id, story, existing_tasks, user) -> str:
    """Build a comprehensive story briefing for the SHCA.

    This is the SHCA's full context about what it needs to help the human accomplish.
    It should understand: what a story is, what this specific story requires, what
    resources exist, what work has already been done, and how to create tasks.
    """
    parts = []

    # ── Section 1: How stories and tasks work ──
    parts.append("""## How This Works

You are helping your human complete a **story** — a human-level work item assigned by the Goal Orchestrator (GO).

**Story vs Task:**
- A **story** is WHAT needs to be done (human-level). You're looking at one now.
- A **task** is HOW to do it (technical-level). You create tasks under a story to track the work.
- When all tasks under a story are complete, the human marks the story as done.

**Your role:** Help the human understand, plan, and execute this story. Research, code, write docs, analyze — whatever it takes. Not every story needs code. Some need research, documentation, or design.

**Creating tasks:** When the work involves multiple distinct steps, create tasks using `create_task`. The task's `parent_task_id` should be set to this story's ID so they're tracked together. For simple work, just do it directly without creating formal tasks.""")

    # ── Section 2: The actual story ──
    parts.append(f"\n---\n## Story: {story['title']}")
    parts.append(f"**ID:** `{task_id}` | **Priority:** {story.get('priority', 'medium')}")

    if story.get("description"):
        parts.append(f"\n**Description:**\n{story['description']}")

    if story.get("acceptance_criteria"):
        parts.append(f"\n**Acceptance Criteria (how we know it's done):**\n{story['acceptance_criteria']}")

    if story.get("estimated_effort"):
        parts.append(f"\n**Estimated Effort:** {story['estimated_effort']}")

    # ── Section 3: Goal context ──
    goal = await pool.fetchrow("SELECT name, objective, state FROM goals WHERE id = $1", pid)
    if goal:
        parts.append(f"\n---\n## Goal Context")
        parts.append(f"**Goal:** {goal['name']}")
        if goal.get("objective"):
            parts.append(f"**Objective:** {goal['objective']}")

    # ── Section 4: Other stories (sibling context) ──
    all_stories = await pool.fetch("""
        SELECT id, title, state, assigned_to, result_summary FROM stories
        WHERE goal_id = $1 AND state != 'cancelled' AND id != $2
        ORDER BY created_at
    """, pid, task_id)
    if all_stories:
        parts.append(f"\n---\n## Other Stories in This Goal")
        for s in all_stories:
            status = s['state']
            result = f" — {s['result_summary'][:100]}" if s.get("result_summary") and s["state"] == "done" else ""
            parts.append(f"- **{s['title']}** [{status}]{result}")
        parts.append("(This gives you context on what else is happening. Your focus is on the story above.)")

    # ── Section 5: Upstream completed work ──
    if story.get("depends_on"):
        upstream = await pool.fetch("""
            SELECT title, state, result_summary FROM tasks
            WHERE (id = ANY($1) OR title = ANY($1)) AND state IN ('done', 'approved')
        """, story["depends_on"])
        if upstream:
            parts.append(f"\n---\n## Upstream Work Already Completed")
            parts.append("These stories were completed before this one and may have produced outputs you can use:")
            for u in upstream:
                result = f"\n  Result: {u['result_summary'][:200]}" if u.get("result_summary") else ""
                parts.append(f"- **{u['title']}** [done]{result}")

    # ── Section 6: Available resources ──
    resources = await pool.fetch("""
        SELECT name, kind, uri, human_context FROM goal_resources
        WHERE goal_id = $1 AND status = 'active'
        ORDER BY kind, name
    """, pid)
    if resources:
        parts.append(f"\n---\n## Available Resources")
        parts.append("These are registered goal resources. Use `resource_summary` or `resource_read` to inspect them.")
        for r in resources:
            ctx = f" — {r['human_context'][:120]}" if r.get("human_context") else ""
            uri = f" ({r['uri'][:80]})" if r.get("uri") else ""
            parts.append(f"- **{r['name']}** [{r['kind']}]{uri}{ctx}")

    # ── Section 7: Existing tasks under this story ──
    if existing_tasks:
        done = [t for t in existing_tasks if t["state"] in ("done", "approved")]
        active = [t for t in existing_tasks if t["state"] not in ("done", "approved", "cancelled")]
        parts.append(f"\n---\n## Existing Tasks Under This Story")
        if done:
            parts.append(f"**Completed ({len(done)}):**")
            for t in done:
                parts.append(f"- {t['title']} [done]")
        if active:
            parts.append(f"**In progress ({len(active)}):**")
            for t in active:
                parts.append(f"- {t['title']} [{t['state']}]")
                if t.get("description"):
                    parts.append(f"  {t['description'][:150]}")
    else:
        parts.append(f"\n---\n## No Tasks Yet")
        parts.append("No tasks have been created under this story yet. Start by understanding what needs to be done, then create tasks as needed.")

    # ── Section 8: Instructions ──
    parts.append(f"\n---\n## What To Do Now")
    parts.append(f"1. Read the story description and acceptance criteria carefully")
    parts.append(f"2. If anything is unclear, ask the human for clarification")
    parts.append(f"3. Research the problem if needed (use web_search, read resources)")
    parts.append(f"4. Propose an approach to the human")
    parts.append(f"5. Execute — create tasks for trackable work, do simple work directly")
    parts.append(f"6. When done, help the human write a completion summary for the GO")

    return "\n".join(parts)


@router.post("/goals/{pid}/chat/{agent_id}/import-task")
async def import_task_to_chat(
    pid: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Import a task (with subtasks) into the SHCA conversation context.

    Sends a system-style message with the full task details so the SHCA
    can see what needs to be done.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    task_id = form.get("task_id", "").strip()
    thread_id = form.get("thread_id", "").strip()

    if not task_id or not thread_id:
        raise HTTPException(status_code=400, detail="task_id and thread_id required")

    pool = get_pool()

    # IDOR: task_id and thread_id are caller-supplied. The task query below is
    # goal-scoped, but the thread_id must be guarded too or a message could be
    # injected into another tenant's thread.
    await _guard_thread_in_goal(pool, thread_id, pid)

    # Fetch task + subtasks
    task = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND goal_id = $2", task_id, pid,
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    subtasks = await pool.fetch("""
        SELECT id, title, state, priority, description, assigned_to, depends_on
        FROM tasks WHERE parent_task_id = $1 AND status = 'active'
        ORDER BY created_at
    """, task_id)

    # Check if this is a story (from stories table)
    is_story = await pool.fetchval("SELECT 1 FROM stories WHERE id = $1", task_id) is not None

    if is_story:
        story_row = await pool.fetchrow("SELECT * FROM stories WHERE id = $1", task_id)
        if story_row and story_row.get("state") == "accepted":
            # Concurrency guard — the same one the board, the assistant import
            # and the local checkout use. A fourth entry point with its own copy
            # of the rule is a fourth place to forget to change it; this one was
            # found by the Stage K gate, not by anyone reading the code.
            from app.services.thread_ceiling import refusal_for
            refusal = await refusal_for(pool, pid, user["uid"], task_id)
            if refusal:
                raise HTTPException(status_code=409, detail=refusal)
            await pool.execute(
                "UPDATE stories SET state = 'in_progress', started_at = now(), updated_at = now() WHERE id = $1",
                task_id,
            )

    if is_story:
        context_msg = await _build_story_briefing(pool, pid, task_id, task, subtasks, user)
    else:
        # Legacy task import
        parts = [
            f"**Task Imported: {task['title']}**",
            f"ID: `{task['id']}` | Priority: {task.get('priority', 'medium')}",
        ]
        if task.get("description"):
            parts.append(f"\n**Description:**\n{task['description']}")
        if task.get("acceptance_criteria"):
            parts.append(f"\n**Acceptance Criteria:**\n{task['acceptance_criteria']}")
        if subtasks:
            parts.append(f"\n**Subtasks ({len(subtasks)}):**")
            for i, sub in enumerate(subtasks, 1):
                state_icon = "done" if sub["state"] in ("done", "approved") else sub["state"]
                parts.append(f"  {i}. **{sub['title']}** [{state_icon}]")
        parts.append("\nWhich subtask would you like to work on?")
        context_msg = "\n".join(parts)

    # Insert as a system message in the thread
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
           VALUES ($1, $2, 'system', 'human', $3, 0)""",
        msg_id, thread_id, context_msg,
    )

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}?thread_id={thread_id}",
        status_code=303,
    )


# -- New conversation ----------------------------------------------------------

@router.post("/goals/{pid}/chat/{agent_id}/new")
async def new_conversation(
    pid: str,
    agent_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Create a new chat thread and redirect to it."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    thread_id = str(uuid.uuid4())
    corr_id = str(uuid.uuid4())

    # Seed title with agent name so sidebar / agent-detail pages show
    # something user-meaningful instead of "New conversation".
    agent_name = await pool.fetchval(
        "SELECT name FROM agents WHERE id = $1", agent_id,
    ) or agent_id

    await pool.execute(
        """INSERT INTO threads (id, goal_id, agent_id, created_by, title, correlation_id, participants)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        thread_id, pid, agent_id, user["uid"], agent_name, corr_id, [user["uid"]],
    )

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}?thread_id={thread_id}",
        status_code=303,
    )


# -- Run state (is the agent currently thinking / using a tool?) -----------

@router.get("/goals/{pid}/chat/{agent_id}/run-state")
async def get_chat_run_state(
    pid: str,
    agent_id: str,
    thread_id: str,
    user: dict = Depends(get_current_user),
):
    """Return the current run indicator state for this thread.

    Used by the chat UI on page load to restore the "thinking…" or
    "Using <tool>…" indicator if a run is still in flight.

    Returns:
      {status: "thinking"} | {status: "using_tool", tool: "Bash"} | {status: "idle"}

    Stale rows (updated_at > 30 min ago) are treated as idle and cleaned
    up lazily here.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    if not thread_id:
        return JSONResponse({"status": "idle"})
    pool = get_pool()
    # IDOR: thread_id is caller-supplied — don't leak another goal's run state.
    await _guard_thread_in_goal(pool, thread_id, pid)
    # Lazy cleanup of stale rows
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


# -- Poll for new messages (DB fallback) ----------------------------------------

@router.get("/goals/{pid}/chat/{agent_id}/poll")
async def poll_messages(
    pid: str,
    agent_id: str,
    thread_id: str,
    after: str = "",
    token: str = "",
):
    """Poll for new messages in a thread. Used as fallback when WebSocket
    streaming fails or for multi-user scenarios.

    Returns messages created after the given message ID or timestamp.
    """
    try:
        claims = verify_firebase_token(token)
    except HTTPException:
        return JSONResponse({"messages": [], "error": "unauthorized"}, status_code=401)

    role = await get_goal_role(claims["uid"], pid)
    if role is None:
        return JSONResponse({"messages": [], "error": "Not a goal member"}, status_code=403)

    pool = get_pool()

    # IDOR: thread_id is caller-supplied — a member of pid must not read the
    # messages of a thread owned by another goal.
    _owner = await pool.fetchval("SELECT goal_id FROM threads WHERE id = $1", thread_id) if thread_id else None
    if _owner is not None and _owner != pid:
        return JSONResponse({"messages": [], "error": "not found"}, status_code=404)

    if after:
        # Get messages after a specific message ID
        rows = await pool.fetch(
            """SELECT id, sender, role, content, created_at
               FROM messages
               WHERE thread_id = $1 AND created_at > (
                   SELECT created_at FROM messages WHERE id = $2
               )
               ORDER BY created_at""",
            thread_id, after,
        )
    else:
        # Get last 5 messages (for initial catch-up)
        rows = await pool.fetch(
            """SELECT id, sender, role, content, created_at
               FROM messages
               WHERE thread_id = $1
               ORDER BY created_at DESC LIMIT 5""",
            thread_id,
        )
        rows = list(reversed(rows))

    from app.services.story_state import strip_state_context
    messages = []
    for r in rows:
        content = r["content"]
        # Strip state-context block from human messages so the poll
        # endpoint feeds the UI just the human's original text.
        if r["role"] == "human":
            content = strip_state_context(content or "")
        messages.append({
            "id": r["id"],
            "sender": r["sender"],
            "role": r["role"],
            "content": content,
            "created_at": str(r["created_at"]),
        })

    return JSONResponse({"messages": messages})


# -- WebSocket endpoint --------------------------------------------------------

@router.websocket("/goals/{pid}/chat/{agent_id}/ws")
async def chat_ws(
    pid: str,
    agent_id: str,
    websocket: WebSocket,
    thread_id: str,
    token: str,
):
    try:
        claims = verify_firebase_token(token)
        uid = claims["uid"]
    except HTTPException:
        logger.warning(
            "chat_ws: rejected WS for %s/%s — token invalid or expired (likely stale baked-in token)",
            pid, agent_id,
        )
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # Membership check BEFORE accept() — get_goal_role() returns None rather
    # than raising for a non-member, so this gate must be explicit or any
    # authenticated platform user could join another goal's live chat.
    role = await get_goal_role(uid, pid)
    if role is None:
        logger.warning("chat_ws: rejected WS uid=%s pid=%s — not a goal member", uid, pid)
        await websocket.close(code=4003, reason="Not a goal member")
        return

    # IDOR (before accept): thread_id is caller-supplied. Membership above only
    # gates pid — reject a member of pid who passes a thread owned by another
    # goal (would otherwise read the stream AND post/forward messages into it).
    pool = get_pool()
    _thread_owner = await pool.fetchval("SELECT goal_id FROM threads WHERE id = $1", thread_id)
    if _thread_owner is not None and _thread_owner != pid:
        logger.warning(
            "chat_ws: rejected WS uid=%s pid=%s thread=%s — thread belongs to another goal",
            uid, pid, thread_id[:12],
        )
        await websocket.close(code=4003, reason="Not a goal member")
        return

    await websocket.accept()

    pool = get_pool()
    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", uid)
    goal_context = await _get_goal_context(pool, pid, agent_id)
    on_message, on_notify = _make_callbacks(pool, pid, agent_id)

    # Read thread's correlation_id (cast to str — asyncpg UUID ≠ stdlib UUID)
    _raw_corr = await pool.fetchval(
        "SELECT correlation_id FROM threads WHERE id = $1", thread_id,
    )
    thread_corr_id = str(_raw_corr) if _raw_corr else None

    # Deleted goal → never create a session for it (stale tab / stale token).
    from app.services.goal_guard import goal_is_dead
    if await goal_is_dead(pid, pool):
        logger.info("chat_ws: goal %s deleted — refusing session for %s", pid, agent_id)
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "This goal has been deleted."}))
        await websocket.close()
        return

    # CLOSED THREAD → readable, never writable.
    #
    # The git engine retires a story's thread once every branch it holds has
    # landed on trunk (git_graph/story_threads.close_threads_for_landed). That
    # sets `left_goal_at`, which takes the row out of the sidebar and out of
    # find-or-create — but a hidden row is still reachable by anyone holding
    # its URL or sitting on a stale tab, and a session opened there would spawn
    # a gateway agent for work that is finished and merged.
    #
    # Refused at the SOCKET rather than per message: the socket is what creates
    # the session, and a connection that can never usefully send is better
    # closed with a reason than left open to fail silently on first use. The
    # transcript stays readable over HTTP — this closes the conversation, it
    # does not hide the record.
    _closed = await pool.fetchrow(
        "SELECT left_goal_at, story_id FROM threads WHERE id = $1", thread_id)
    if _closed is not None and _closed["left_goal_at"] is not None:
        logger.info(
            "chat_ws: thread %s is closed (story %s landed) — refusing session",
            thread_id[:12], _closed["story_id"] or "-")
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": ("This story's work has landed on trunk and its "
                        "conversation is closed. Open a current story to "
                        "keep working."),
        }))
        await websocket.close()
        return

    session = session_manager.get_or_create(
        pid, agent_id, goal_context, on_message, on_notify, thread_id=thread_id,
    )

    unsubscribe = session.subscribe(thread_id, websocket.send_text)
    logger.info("chat_ws: connected uid=%s pid=%s agent=%s thread=%s", uid, pid, agent_id, thread_id[:12])

    # Zero the user's unread counter for this SHCA thread on WS connect.
    # The page-load GET handler also calls this, but the WS connect is the
    # definitive "user is now actively watching" moment.
    # Non-critical: counter failure must never break the WS session.
    try:
        await mark_thread_read(pool, thread_id, uid)
    except Exception as exc:
        logger.warning("chat_ws: mark_thread_read failed (non-fatal): %s", exc)

    try:
        async for raw in websocket.iter_text():
            try:
                parsed = json.loads(raw)
                if parsed.get("type") == "card_response":
                    # User responded to an agent card (question, approval, etc.)
                    task_id = parsed.get("task_id", "")
                    option_id = parsed.get("option_id", "")
                    value = parsed.get("value", "")
                    logger.info(
                        "chat_ws: card_response received pid=%s agent=%s thread=%s task=%s option=%s",
                        pid, agent_id, thread_id[:12], task_id, option_id,
                    )

                    if not task_id or not value:
                        logger.warning(
                            "chat_ws: card_response missing task_id or value — dropped pid=%s thread=%s task=%s",
                            pid, thread_id[:12], task_id,
                        )
                        continue

                    # IDOR: task_id comes straight off the socket. Scope to pid
                    # (the goal this WS is authorized for) so a member of one
                    # goal cannot approve/replan/unblock/redirect another
                    # tenant's task + drive its live agent session.
                    task_row = await pool.fetchrow(
                        "SELECT * FROM tasks WHERE id = $1 AND goal_id = $2", task_id, pid,
                    )
                    if not task_row:
                        continue
                    task = dict(task_row)

                    # Kill switch — refuse to act on a card (answer / approve /
                    # request-changes / replan) while the goal or system is frozen.
                    # Every branch below injects the answer into the LIVE agent
                    # session or mutates task rows + wakes the controller, all of
                    # which bypass the freeze. This mirrors the shared
                    # card_responses.handle_card_response gate (chat.py keeps its
                    # own inline copy). Notify + drop before persisting anything.
                    try:
                        from app.services.execution_control import dispatch_block as _card_block  # noqa: PLC0415
                        _cblk = await _card_block(task.get("goal_id") or pid)
                    except Exception:
                        _cblk = None
                    if _cblk is not None:
                        logger.info(
                            "chat_ws: card_response blocked by freeze pid=%s scope=%s task=%s",
                            pid, _cblk.scope, task_id,
                        )
                        await websocket.send_text(json.dumps({
                            "type": "system_notice",
                            "code": "EXECUTION_PAUSED",
                            "scope": _cblk.scope,
                            "message": (
                                f"⏸ Execution is paused ({_cblk.scope}). "
                                "Resume in the control panel, then respond again."
                            ),
                        }))
                        continue

                    # 1. Record user's answer in SHCA thread
                    msg_id = str(uuid.uuid4())
                    display_value = f"[Re: {task_id}] {value}"
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            last_event_id = await _last_thread_event_id(conn, thread_id)

                            await append_event(
                                conn,
                                event_type=QUESTION_ANSWERED,
                                payload=question_answered_payload(
                                    answer=value,
                                    answerer={"actor_id": str(user_actor_id), "actor_type": "human", "display_name": uid},
                                    question_event_id=last_event_id or "",
                                ),
                                actor_id=user_actor_id,
                                correlation_id=thread_corr_id,
                                causation_ids=[last_event_id] if last_event_id else None,
                                context_goal_id=pid,
                                context_conversation_id=thread_id,
                                context_task_id=task_id,
                            )
                            await conn.execute(
                                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                                   VALUES ($1, $2, $3, 'human', $4, 0)""",
                                msg_id, thread_id, uid, display_value,
                            )

                    # 2. Route answer to blocked agent
                    if task["state"] == "blocked":
                        task_corr = await get_task_correlation_id(pool, task_id)
                        task_last = await get_last_event_id(pool, task_id)
                        _parent_tid = task.get("parent_task_id") or task_id

                        if option_id == "replan":
                            # ── Re-plan path: cancel blocked + unstarted, re-dispatch planner ──
                            logger.info(
                                "REPLAN via card: task=%s parent=%s value_len=%d",
                                task_id, _parent_tid, len(value),
                            )

                            # Store feedback on the parent (planner) task
                            parent_task_id_for_replan = _parent_tid
                            await pool.execute(
                                "UPDATE tasks SET blocked_reason = $1 WHERE id = $2",
                                f"HUMAN FEEDBACK: {value}", parent_task_id_for_replan,
                            )

                            # Cancel blocked subtask + all unstarted siblings.
                            # RETURNING ids so we can release any per-task IAM
                            # keys minted on dispatch (typical for 'dispatching'
                            # or 'blocked' subtasks).
                            cancelled_rows = await pool.fetch("""
                                UPDATE tasks SET state = 'cancelled'
                                WHERE parent_task_id = $1
                                  AND state IN ('pending', 'ready', 'dispatching', 'blocked')
                                  AND status = 'active'
                                RETURNING id
                            """, parent_task_id_for_replan)
                            from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
                            for r in cancelled_rows:
                                await release_credentials_safe(r["id"], "chat_router:replan_cancel")

                            # Reset parent to ready for re-dispatch
                            await pool.execute(
                                "UPDATE tasks SET state = 'ready', result = '' WHERE id = $1",
                                parent_task_id_for_replan,
                            )

                            await emit_task_event(
                                pool,
                                event_type=TASK_STATUS_CHANGED,
                                task_id=parent_task_id_for_replan,
                                goal_id=task["goal_id"],
                                payload=task_status_changed_payload(
                                    previous_status="in_progress",
                                    new_status="ready",
                                    reason=f"Re-plan requested: {value[:200]}",
                                ),
                                actor_id=user_actor_id,
                                correlation_id=task_corr,
                                causation_ids=[task_last] if task_last else None,
                                summary=f"Re-plan by {uid}",
                            )

                            # Trigger the task controller
                            from app.task_controller import task_controller
                            await task_controller.trigger()
                            logger.info(
                                "REPLAN: parent=%s reset to ready, controller triggered",
                                parent_task_id_for_replan,
                            )

                        elif task.get("instance_id"):
                            # ── Simple reply path: inject answer, resume agent ──
                            agent_session_key = make_task_session_key(task['instance_id'], _parent_tid)
                            try:
                                await gateway_admin.call("agent", {
                                    "agentId": task["instance_id"],
                                    "sessionKey": agent_session_key,
                                    "message": f"Human response to your question:\n\n{value}",
                                    "idempotencyKey": f"answer-{msg_id}",
                                })
                                logger.info("Routed answer to agent %s for task %s", task["instance_id"], task_id)
                            except Exception as exc:
                                logger.warning("Failed to route answer to agent %s: %s", task["instance_id"], exc)

                            # Unblock task
                            await pool.execute(
                                "UPDATE tasks SET state = 'in_progress', blocked_reason = NULL WHERE id = $1",
                                task_id,
                            )
                            await emit_task_event(
                                pool,
                                event_type=TASK_STATUS_CHANGED,
                                task_id=task_id, goal_id=task["goal_id"],
                                payload=task_status_changed_payload(
                                    previous_status="blocked", new_status="in_progress",
                                    reason=f"Human answered: {value[:200]}",
                                ),
                                actor_id=user_actor_id,
                                correlation_id=task_corr,
                                causation_ids=[task_last] if task_last else None,
                                summary=f"Unblocked by {uid}",
                            )

                        # Auto-save answer to goal memory (both paths)
                        try:
                            from app.memory import auto_save_question_answered
                            user_row = await pool.fetchrow(
                                "SELECT display_name FROM users WHERE uid = $1", uid)
                            _uname = user_row["display_name"] if user_row else uid
                            question_text = task.get("blocked_reason", "")
                            await auto_save_question_answered(
                                pool, task_id, task["goal_id"],
                                question_text, value, _uname, uid,
                            )
                        except Exception as exc:
                            logger.warning("Failed to auto-save answer memory: %s", exc)

                    # 4. Handle plan approval
                    elif option_id == "approve" and task["state"] == "awaiting_approval":
                        from app.task_controller import create_subtasks_from_planner, task_controller
                        logger.info("Plan approved for task %s — creating subtasks", task_id)

                        # Create subtasks from the stored planner result
                        result = task.get("result", "")
                        if result:
                            await create_subtasks_from_planner(task, result)

                        # Move task to in_progress (subtasks will drive to completion)
                        await pool.execute(
                            "UPDATE tasks SET state = 'in_progress' WHERE id = $1", task_id,
                        )
                        task_corr = await get_task_correlation_id(pool, task_id)
                        task_last = await get_last_event_id(pool, task_id)
                        await emit_task_event(
                            pool,
                            event_type=TASK_STATUS_CHANGED,
                            task_id=task_id, goal_id=task["goal_id"],
                            payload=task_status_changed_payload(
                                previous_status="awaiting_approval", new_status="in_progress",
                                reason="Plan approved by human",
                            ),
                            actor_id=user_actor_id,
                            correlation_id=task_corr,
                            causation_ids=[task_last] if task_last else None,
                            summary="Plan approved — dispatching subtasks",
                        )
                        await task_controller.trigger()

                        # Auto-save plan approval to goal memory
                        try:
                            from app.memory import auto_save_plan_approved
                            user_row = await pool.fetchrow(
                                "SELECT display_name FROM users WHERE uid = $1", uid)
                            _uname = user_row["display_name"] if user_row else uid
                            await auto_save_plan_approved(
                                pool, task_id, task["goal_id"],
                                _uname, uid, len(result.split("```")) // 2,  # rough subtask count
                            )
                        except Exception as exc:
                            logger.warning("Failed to auto-save plan approval: %s", exc)

                    # 5. Handle plan change request
                    elif option_id == "changes" and task["state"] == "awaiting_approval":
                        logger.info("Plan changes requested for task %s: %s", task_id, value[:100])

                        # Check if a persistent worker exists for this task
                        _pw = await pool.fetchrow("""
                            SELECT gateway_agent_id FROM agent_instances
                            WHERE parent_task_id = $1 AND status = 'active'
                              AND worker_type = 'persistent'
                            LIMIT 1
                        """, task_id)

                        if _pw and task.get("instance_id"):
                            # Persistent worker exists — resume with feedback
                            # (the planner's full context is in the CLI session)
                            _pw_session_key = make_task_session_key(_pw['gateway_agent_id'], task_id)
                            revision_msg = f"""REVISION REQUESTED by {uid}:
"{value}"

You are still the PLANNER. Revise your plan based on this feedback.
You have full context of your original plan in the session history.
Update the subtask list and call task-report with the revised plan.
Do NOT start implementing — only revise the plan."""

                            try:
                                await gateway_admin.call("agent", {
                                    "agentId": _pw["gateway_agent_id"],
                                    "sessionKey": _pw_session_key,
                                    "message": revision_msg,
                                    "idempotencyKey": f"revision-{task_id}-{msg_id}",
                                })
                                logger.info("Sent plan revision to persistent worker %s", _pw["gateway_agent_id"])
                            except Exception as exc:
                                logger.warning("Failed to send revision to persistent worker: %s", exc)

                            await pool.execute(
                                "UPDATE tasks SET state = 'in_progress', blocked_reason = $1 WHERE id = $2",
                                f"HUMAN FEEDBACK: {value}", task_id,
                            )
                        else:
                            # No persistent worker — fall back to re-dispatch
                            await pool.execute(
                                "UPDATE tasks SET state = 'ready', result = NULL, blocked_reason = $1 WHERE id = $2",
                                f"HUMAN FEEDBACK: {value}", task_id,
                            )

                        task_corr = await get_task_correlation_id(pool, task_id)
                        task_last = await get_last_event_id(pool, task_id)
                        await emit_task_event(
                            pool,
                            event_type=TASK_STATUS_CHANGED,
                            task_id=task_id, goal_id=task["goal_id"],
                            payload=task_status_changed_payload(
                                previous_status="awaiting_approval",
                                new_status="in_progress" if _pw else "ready",
                                reason=f"Plan changes requested: {value[:200]}",
                            ),
                            actor_id=user_actor_id,
                            correlation_id=task_corr,
                            causation_ids=[task_last] if task_last else None,
                            summary=f"Replanning: {value[:100]}",
                        )
                        if not _pw:
                            await task_controller.trigger()

                elif parsed.get("type") == "message":
                    msg_id = str(uuid.uuid4())
                    content = parsed.get("content", "")
                    logger.info(
                        "chat_ws: message received pid=%s agent=%s thread=%s len=%d",
                        pid, agent_id, thread_id[:12], len(content),
                    )

                    # For SHCA chats, append the current story state
                    # context to the message content. The augmented
                    # content is stored in the DB verbatim and forwarded
                    # to the gateway — SHCA's LLM reads it as its source
                    # of truth for current story/phase/session. UI and
                    # Slack relay strip the appended block when rendering
                    # so the human only sees their own text.
                    augmented_content = content
                    if goal_context.get("agent_type") == "SHCA":
                        try:
                            from app.services.story_state import append_state_context_to_content
                            augmented_content = await append_state_context_to_content(
                                thread_id, content,
                            )
                        except Exception:
                            logger.exception(
                                "state_context append failed (thread=%s) — "
                                "sending message without state context",
                                thread_id,
                            )
                            augmented_content = content

                    # Freeze gate — check BEFORE persisting so a dropped
                    # message never becomes an orphaned DB row that the
                    # agent silently misses on resume (in non-queue mode
                    # there is no replay; the message would be lost).
                    try:
                        from app.services.execution_control import conversation_block  # noqa: PLC0415
                        _freeze = await conversation_block(pid)
                        if _freeze is not None:
                            logger.info(
                                "chat_ws: message blocked by execution freeze pid=%s scope=%s thread=%s reason=%s",
                                pid, _freeze.scope, thread_id[:12], getattr(_freeze, "reason", None) or "<none>",
                            )
                            await websocket.send_text(json.dumps({
                                "type": "system_notice",
                                "code": "EXECUTION_PAUSED",
                                "scope": _freeze.scope,
                                "message": (
                                    f"⏸ Execution is paused ({_freeze.scope}). "
                                    "Resume to continue."
                                ),
                            }))
                            continue
                    except Exception:
                        logger.exception("freeze gate check failed — failing open (message sent)")

                    # Read-only gate — the thread's owner left the goal (see
                    # threads_left_goal_at migration / remove_goal_member). The
                    # thread stays visible to every current member but nobody,
                    # including the departed owner if they still hold a stale
                    # session, can post into it. Same shape as the freeze gate
                    # above, checked separately since the two conditions are
                    # independent (a thread can be read-only with no execution
                    # freeze in effect, and vice versa).
                    try:
                        left_goal_at = await pool.fetchval(
                            "SELECT left_goal_at FROM threads WHERE id = $1", thread_id,
                        )
                        if left_goal_at is not None:
                            logger.info(
                                "chat_ws: message blocked — thread read-only pid=%s thread=%s",
                                pid, thread_id[:12],
                            )
                            await websocket.send_text(json.dumps({
                                "type": "system_notice",
                                "code": "THREAD_READ_ONLY",
                                "message": "This member has left the goal — this conversation is read-only.",
                            }))
                            continue
                    except Exception:
                        logger.exception("read-only gate check failed — failing open (message sent)")

                    # DB insert first — message must be persisted before
                    # it reaches the gateway.
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            last_event_id = await _last_thread_event_id(conn, thread_id)

                            await append_event(
                                conn,
                                event_type=MESSAGE_POSTED,
                                payload={
                                    "message_id": msg_id,
                                    "thread_id": thread_id,
                                    "sender": uid,
                                    "role": "human",
                                    "content": augmented_content,
                                    "token_count": 0,
                                },
                                actor_id=user_actor_id,
                                correlation_id=thread_corr_id,
                                causation_ids=[last_event_id] if last_event_id else None,
                                context_goal_id=pid,
                                context_conversation_id=thread_id,
                            )
                            await conn.execute(
                                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                                   VALUES ($1, $2, $3, 'human', $4, 0)""",
                                msg_id, thread_id, uid, augmented_content,
                            )
                    logger.info(
                        "chat_ws: message persisted pid=%s thread=%s msg_id=%s",
                        pid, thread_id[:12], msg_id,
                    )

                    # Q&A relay: if a phase task is blocked *waiting for a
                    # human answer*, treat this chat as the answer and route
                    # it directly to Claude via the phase dispatcher. SHCA's
                    # LLM is NOT invoked — pure mechanical relay so we don't
                    # waste tokens on a paraphrase.
                    #
                    # CRITICAL: filter on `last_blocker_question IS NOT NULL`.
                    # A task can also enter `blocked` for infrastructure
                    # reasons (e.g. gateway connection lost during dispatch —
                    # see phase_dispatcher.py retryable-error path). Those
                    # tasks have NO pending question — routing chat to them
                    # would silently swallow user messages intended for the
                    # SHCA. The column is set ONLY by sentinel_parser when a
                    # phase emits `PHASE_TASK_BLOCKED T<id> — <question>`,
                    # and cleared on resume / done.
                    blocked_task = await pool.fetchrow(
                        """SELECT t.* FROM tasks t
                            JOIN stories s ON s.id = t.story_id
                           WHERE s.active_thread_id = $1
                             AND t.executor = 'claude-code-phase'
                             AND t.state = 'blocked'
                             AND t.last_blocker_question IS NOT NULL
                           ORDER BY t.last_blocker_at DESC NULLS LAST
                           LIMIT 1""",
                        thread_id,
                    )
                    if blocked_task:
                        try:
                            # M17: gate the Q&A relay on execution state. The
                            # answer is NOT stored — user must re-send after
                            # resuming. This is intentional: storing answers
                            # requires a migration and replay machinery, but the
                            # race window where pause lands between here and the
                            # create_task is sub-millisecond. The wider problem
                            # (user sends answer while already paused) is caught
                            # by the conversation_block check above; this guard
                            # covers the narrow Q&A-specific branch.
                            from app.services.execution_control import dispatch_block as _db
                            _qa_block = await _db(blocked_task.get("goal_id") or pid)
                            if _qa_block:
                                logger.info(
                                    "chat_ws: Q&A answer blocked by freeze pid=%s scope=%s thread=%s task=%s",
                                    pid, _qa_block.scope, thread_id[:12], blocked_task["id"],
                                )
                                await websocket.send_text(json.dumps({
                                    "type": "system_notice",
                                    "code": "EXECUTION_PAUSED",
                                    "scope": _qa_block.scope,
                                    "message": (
                                        f"⏸ Execution is paused ({_qa_block.scope}). "
                                        "Resume execution in the control panel, "
                                        "then re-send your answer."
                                    ),
                                }))
                                continue

                            from app.services.phase_dispatcher import resume_blocked_task_safe
                            import asyncio as _asyncio_qa
                            # Safe wrapper: any crash in the fire-and-forget resume fails
                            # the task + cascades instead of stranding it in_progress.
                            _asyncio_qa.create_task(
                                resume_blocked_task_safe(dict(blocked_task), content)
                            )
                            logger.info(
                                "Q&A relay: forwarded chat to blocked task %s (%d chars)",
                                blocked_task["id"], len(content),
                            )
                            # Don't forward to SHCA — the human's chat goes
                            # directly to Claude. (We still recorded the
                            # message above so the chat history is complete.)
                            continue
                        except Exception:
                            logger.exception("Q&A relay failed, falling back to SHCA")

                    # Inject thread_id and the augmented content (with
                    # state context appended, for SHCA chats), then
                    # forward to gateway. The gateway / SHCA's LLM sees
                    # the full content; the UI + Slack render only the
                    # text-before-the-marker so the human sees just
                    # their own message.
                    parsed["thread_id"] = thread_id
                    parsed["content"] = augmented_content
                    raw = json.dumps(parsed)
                    # persisted_message_id: when the chat_module queue is on,
                    # this flips the row we just INSERTed to 'queued' (no
                    # double-persist) instead of sending to the gateway.
                    logger.info(
                        "chat_ws: forwarding to gateway pid=%s agent=%s thread=%s msg_id=%s augmented_len=%d",
                        pid, agent_id, thread_id[:12], msg_id, len(augmented_content),
                    )
                    await session.send_to_gateway(raw, persisted_message_id=msg_id)
                    logger.debug(
                        "chat_ws: gateway forward returned pid=%s agent=%s thread=%s",
                        pid, agent_id, thread_id[:12],
                    )

                    # Mirror to Slack channel (if Slack is enabled for this
                    # goal and the thread is mapped). Fire-and-forget so the
                    # gateway round-trip isn't blocked by Slack API latency.
                    try:
                        from app.channels.slack.outbound import relay_human_message
                        import asyncio as _asyncio_relay
                        _asyncio_relay.create_task(
                            relay_human_message(pid, thread_id, content, uid, msg_id=msg_id)
                        )
                    except ImportError:
                        pass
                    except Exception as exc:
                        logger.warning(
                            "Slack human relay scheduling failed (%s/%s): %s",
                            pid, thread_id[:12], exc,
                        )

                    # Mirror to Notion page (both notion and notion_bot bindings).
                    try:
                        from app.channels.notion.outbound import relay_human_message as _notion_human
                        import asyncio as _asyncio_notion
                        _display = await pool.fetchval("SELECT display_name FROM users WHERE uid=$1", uid) or uid
                        _asyncio_notion.create_task(_notion_human(pid, thread_id, content, _display))
                    except Exception as exc:
                        logger.warning("Notion human relay scheduling failed (%s/%s): %s", pid, thread_id[:12], exc)

                    # Auto-generate thread title after 6 messages
                    import asyncio as _asyncio
                    _asyncio.create_task(_maybe_generate_thread_title(pool, thread_id, pid, agent_id))

                    # Auto-summarize conversation for SHCA memory system
                    if goal_context.get("agent_type") == "SHCA":
                        from app.memory import auto_summarize_if_needed
                        _asyncio.create_task(
                            auto_summarize_if_needed(pool, agent_id, pid, thread_id)
                        )

                else:
                    await session.send_to_gateway(raw)
            except Exception as exc:
                logger.error("Chat WS message handling error (%s/%s): %s", pid, agent_id, exc,
                             exc_info=True)
                # Surface the error to the browser so the user knows their message failed
                try:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "code": "MESSAGE_HANDLING_ERROR",
                        "message": f"Failed to process message: {exc}",
                        "source": "control_panel",
                        "retryable": True,
                    }))
                except Exception:
                    pass  # Browser already disconnected

    except WebSocketDisconnect:
        logger.info("chat_ws: disconnected uid=%s pid=%s agent=%s thread=%s", uid, pid, agent_id, thread_id[:12])
    except Exception as exc:
        logger.error("Chat WS unexpected error (%s/%s): %s", pid, agent_id, exc, exc_info=True)
    finally:
        unsubscribe()


# -- Thread refresh (reset agent session) ------------------------------------

@router.post("/goals/{pid}/chat/{agent_id}/threads/{thread_id}/refresh")
async def refresh_thread(
    pid: str,
    agent_id: str,
    thread_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Reset the OpenClaw session for this thread.

    The agent starts fresh, but MEMORY.md provides a summary of previous conversation.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    # IDOR: thread_id is caller-supplied — don't reset another goal's session.
    await _guard_thread_in_goal(pool, thread_id, pid)
    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user["uid"])

    # Session key uses the control panel agent_id (matches send_to_gateway).
    # pid is lowercased — gateway canonicalises sessionKeys to lowercase.
    session_key = make_goal_session_key(agent_id, pid, thread_id)
    try:
        await gateway_admin.reset_session(session_key)
        logger.info("Reset OpenClaw session for thread %s (key=%s)", thread_id, session_key)
    except Exception as exc:
        logger.warning("Failed to reset OpenClaw session %s: %s", session_key, exc)

    # Log to event ledger
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=THREAD_REFRESHED,
                payload={
                    "thread_id": thread_id,
                    "agent_id": agent_id,
                    "reason": "user_requested",
                },
                actor_id=user_actor_id,
                context_goal_id=pid,
                context_conversation_id=thread_id,
            )

    # Evict only this thread from the shared session — other threads stay alive
    session_manager.evict_thread(pid, agent_id, thread_id)

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}?thread_id={thread_id}",
        status_code=303,
    )


# -- Thread delete -----------------------------------------------------------

@router.post("/goals/{pid}/chat/{agent_id}/threads/{thread_id}/delete")
async def delete_thread(
    pid: str,
    agent_id: str,
    thread_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Delete a thread from PostgreSQL AND the OpenClaw session."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    # IDOR: thread_id is caller-supplied — don't delete another goal's thread.
    await _guard_thread_in_goal(pool, thread_id, pid)
    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user["uid"])

    # Count messages for the ledger payload
    message_count = await pool.fetchval(
        "SELECT COUNT(*) FROM messages WHERE thread_id = $1", thread_id,
    )

    # 1. Delete the OpenClaw session on the gateway
    # Session key uses the control panel agent_id (matches send_to_gateway).
    # pid is lowercased — gateway canonicalises sessionKeys to lowercase.
    session_key = make_goal_session_key(agent_id, pid, thread_id)
    try:
        await gateway_admin.delete_session(session_key, delete_transcript=True)
        logger.info("Deleted OpenClaw session for thread %s (key=%s)", thread_id, session_key)
    except Exception as exc:
        logger.warning("Failed to delete OpenClaw session %s: %s", session_key, exc)

    # 2. Log to event ledger + delete messages and thread from PostgreSQL
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=THREAD_DELETED,
                payload={
                    "thread_id": thread_id,
                    "agent_id": agent_id,
                    "message_count": message_count,
                },
                actor_id=user_actor_id,
                context_goal_id=pid,
                context_conversation_id=thread_id,
            )
            await conn.execute("DELETE FROM messages WHERE thread_id = $1", thread_id)
            # thread_participants (unread-badge counter cache) and tasks.thread_id both
            # FK-reference threads(id) with no ON DELETE CASCADE (migrations must stay
            # additive/non-destructive — see CLAUDE.md). Clear them first or the DELETE
            # below raises a ForeignKeyViolationError.
            await conn.execute("DELETE FROM thread_participants WHERE thread_id = $1", thread_id)
            await conn.execute("UPDATE tasks SET thread_id = NULL WHERE thread_id = $1", thread_id)
            # F6 — THE REVERSE POINTER, which nothing cleared.
            #
            # The binding between an assistant and its conversation is
            # one fact in two columns: `threads.instance_id` and
            # `agent_instances.thread_id`. Deleting the thread left the
            # second pointing at a row that no longer exists, and the
            # instance stayed `status='active'` -- so every listing read
            # as healthy while the goal had no assistant anybody could
            # open, and nothing said why.
            #
            # Cleared rather than destroyed: destroying goes through the
            # gateway and takes the workspace with it, including the
            # accumulated MEMORY.md. That is why the cleanup watchdog is
            # disabled (see app/main.py). An unbound instance is
            # re-bindable; a destroyed one has lost its memory.
            await conn.execute(
                "UPDATE agent_instances SET thread_id = NULL "
                "WHERE thread_id = $1", thread_id)
            # The channel goes with the thread. Slack cannot delete a
            # channel outside Enterprise Grid, so this archives it -- and
            # an archived name stays reserved, which is why channel names
            # carry the thread id rather than the story id.
            try:
                from app.channels.slack.thread_channels import (
                    archive_channel_for_thread)
                await archive_channel_for_thread(pool, thread_id)
            except Exception as _slack_exc:
                logger.warning("Slack channel for deleted thread %s not archived: %s",
                             thread_id, _slack_exc)
            await conn.execute("DELETE FROM threads WHERE id = $1", thread_id)

    # 3. Evict only this thread — other threads on the same agent stay alive
    session_manager.evict_thread(pid, agent_id, thread_id)

    logger.info("Deleted thread %s (%d messages) for %s/%s", thread_id, message_count, pid, agent_id)

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}",
        status_code=303,
    )


@router.post("/goals/{pid}/chat/{agent_id}/threads/{thread_id}/close")
async def close_thread(
    pid: str,
    agent_id: str,
    thread_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Close a conversation — summarize, promote to goal memory, archive.

    The thread becomes read-only. The SHCA instance (if active) is destroyed.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Check thread exists and is open. goal_id guards cross-tenant IDOR: a
    # member of pid must not close/archive a thread owned by another goal.
    thread = await pool.fetchrow(
        "SELECT status, agent_id, goal_id FROM threads WHERE id = $1", thread_id,
    )
    if not thread or thread["goal_id"] != pid:
        raise HTTPException(404, "Thread not found")
    if thread["status"] == "closed":
        raise HTTPException(400, "Thread is already closed")

    # Close the conversation (summarize → promote → archive)
    from app.memory import close_conversation
    await close_conversation(pool, thread_id, agent_id, pid)

    # Evict the session (destroys SHCA instance if active)
    session_manager.evict_thread(pid, agent_id, thread_id)

    logger.info("Closed conversation %s for %s/%s", thread_id, pid, agent_id)

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}",
        status_code=303,
    )
