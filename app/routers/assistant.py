"""Assistant template management + user configuration routes."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin, get_goal_role
from app.database import get_pool
from app.flash import flash_redirect
from app.assistant_provisioning import (
    create_assistant_template, list_assistant_templates, get_assistant_template,
    get_user_assistants,
    update_assistant_template_files,
)

_log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Shared helper: provision an SHCA thread (optionally importing a story) ───

async def _ensure_user_assistant_agent(user_uid: str) -> str:
    """Find or auto-provision the user's SHCA agent_id (one per user)."""
    pool = get_pool()
    from app.services.agent_registry.assistant_queries import get_user_assistant_agent_id_by_uid
    existing_id = await get_user_assistant_agent_id_by_uid(pool, user_uid)
    if existing_id:
        return existing_id
    user_row = await pool.fetchrow(
        "SELECT actor_id, display_name, email FROM users WHERE uid = $1", user_uid,
    )
    if not user_row:
        raise RuntimeError(f"User {user_uid} not found")
    from app.assistant_provisioning import ensure_user_assistant
    result = await ensure_user_assistant(
        user_uid, user_row["actor_id"],
        user_row.get("display_name", ""), user_row.get("email", ""),
    )
    agent_id = result.get("agent_id", "")
    if not agent_id:
        raise RuntimeError(f"Failed to provision SHCA for user {user_uid}")
    return agent_id


PLATFORM_GOAL_ID = "__platform__"
PLATFORM_SHCA_AGENT_ID = "platform-shca"


async def _resolve_platform_shca_agent() -> str:
    """The Platform goal's ONLY assistant is the singleton platform-shca — it
    alone carries the apply_* integration tools. Stories/threads here must never
    fall back to a user's personal SHCA (no apply_*, and it would strand the
    story on the wrong agent). Raises if the singleton isn't provisioned yet
    (i.e. PLATFORM_COUNCIL_ENABLED is off)."""
    pool = get_pool()
    exists = await pool.fetchval(
        "SELECT 1 FROM agents WHERE id = $1 AND status = 'active'", PLATFORM_SHCA_AGENT_ID)
    if not exists:
        raise RuntimeError(
            "platform-shca is not provisioned — the Platform Council must be "
            "enabled (PLATFORM_COUNCIL_ENABLED) for this deployment")
    return PLATFORM_SHCA_AGENT_ID


async def _ensure_agent_in_goal(goal_id: str, agent_id: str, added_by: str) -> None:
    """Idempotent: ensure the SHCA agent is registered in this goal's
    agent_contexts. No-op if already present."""
    pool = get_pool()
    await pool.execute("""
        INSERT INTO agent_contexts (goal_id, agent_id, added_by)
        VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
    """, goal_id, agent_id, added_by)


async def _ensure_persistent_user_goal_thread(
    user_uid: str, goal_id: str, agent_id: str,
) -> str:
    """Return the user's persistent SHCA thread for this goal, creating
    it on first call. Lookup order:

      1. slack_user_goal_channels.thread_id (canonical pointer when Slack
         is wired up).
      2. threads table — most recent thread for (goal, agent, user) when
         the user entered via the control panel before Slack.
      3. Create a new thread if neither exists.

    The returned thread_id is stable for the lifetime of (user, goal) —
    every story import rotates through it, conversation history persists
    across stories.
    """
    import uuid as _uuid
    pool = get_pool()

    existing = await pool.fetchval("""
        SELECT thread_id FROM slack_user_goal_channels
         WHERE goal_id = $1 AND user_uid = $2 AND archived_at IS NULL
           AND thread_id <> ''
    """, goal_id, user_uid)
    if existing:
        return existing

    existing = await pool.fetchval("""
        SELECT id FROM threads
         WHERE goal_id = $1 AND agent_id = $2 AND created_by = $3
         ORDER BY created_at LIMIT 1
    """, goal_id, agent_id, user_uid)
    if existing:
        return existing

    thread_id = str(_uuid.uuid4())
    corr_id = str(_uuid.uuid4())
    # Seed the title with the agent's display name so the conversation
    # surfaces as e.g. "Abhishek SHCA" everywhere title is rendered.
    # Falls back to the agent id if the row is missing (shouldn't happen).
    agent_name = await pool.fetchval(
        "SELECT name FROM agents WHERE id = $1", agent_id,
    ) or agent_id
    await pool.execute(
        """INSERT INTO threads (id, goal_id, agent_id, created_by, title, correlation_id)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        thread_id, goal_id, agent_id, user_uid, agent_name, corr_id,
    )

    # Keep platform channel bindings in sync so inbound webhook routing
    # is never left pointing at the old (deleted) thread.
    actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    if actor_id:
        updated = await pool.execute(
            """UPDATE platform_channel_bindings
               SET thread_id = $1, agent_id = $2
               WHERE goal_id     = $3
                 AND actor_id    = $4
                 AND archived_at IS NULL""",
            thread_id, agent_id, goal_id, actor_id,
        )
        # asyncpg.execute() returns a status string like "UPDATE 3"; parse defensively.
        try:
            rows_updated = int(updated.split()[-1])
        except (AttributeError, IndexError, ValueError):
            _log.warning("Unexpected asyncpg status format when updating platform bindings: %r", updated)
            rows_updated = 0
        if rows_updated:
            _log.info(
                "Platform bindings updated for new SHCA thread: goal=%s actor=%s thread=%s",
                goal_id, str(actor_id)[:8], thread_id[:8],
            )

    return thread_id


async def provision_assistant_thread_for_story(
    user_uid: str, goal_id: str, story_id: str = "",
    engine_initiated: bool = False,
) -> tuple[str, str]:
    """Resolve the user's persistent SHCA thread for this goal and,
    optionally, rotate it onto a story.

    Reuses the existing (user, goal) thread when one exists. Creates one
    on first call. When `story_id` is set: appends a fresh "Story Mode
    Active" marker into the persistent thread (multiple markers
    accumulate over time — `_build_identity_md` reads only the most
    recent), updates `stories.active_thread_id`, transitions
    `accepted → in_progress`, and rotates the channel's
    `current_story_id`.

    Returns (thread_id, agent_id). Raises RuntimeError on provisioning
    failure.
    """
    import uuid as _uuid
    pool = get_pool()

    if goal_id == PLATFORM_GOAL_ID:
        # The Platform goal is admins-only and served solely by the singleton
        # platform-shca (already goal-bound at provisioning). Route the story
        # thread to it, never to the user's personal SHCA.
        agent_id = await _resolve_platform_shca_agent()
    else:
        agent_id = await _ensure_user_assistant_agent(user_uid)
        await _ensure_agent_in_goal(goal_id, agent_id, user_uid)
    thread_id = await _ensure_persistent_user_goal_thread(user_uid, goal_id, agent_id)

    if not story_id:
        return thread_id, agent_id

    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id,
    )
    if not story:
        return thread_id, agent_id

    # Kill switch — refuse to START a story while its goal or the system is
    # frozen. This is the single shared chokepoint for every "work on this story"
    # entry point (web "Open in assistant", Slack work-on-story new + legacy, and
    # the git-graph branch-ready listener), so gating here blocks BOTH the
    # story→in_progress transition below AND the downstream kickoff/provisioning
    # the callers run next (they abort on the raised error). The benign no-story
    # thread-resolution paths above are unaffected. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _blk = await execution_control.dispatch_block(goal_id)
    if _blk is not None:
        _log.info(
            "provision_assistant_thread_for_story refused — execution paused "
            "(scope=%s) goal=%s story=%s uid=%s",
            _blk.scope, goal_id, story_id, user_uid,
        )
        raise execution_control.ExecutionPausedError(_blk)

    # Note: the "Story Mode Active" marker that _build_identity_md
    # uses to detect the active story is no longer inserted as a
    # separate system message here. It's now folded into the single
    # combined GO→SHCA message that dispatch_story_kickoff_to_assistant
    # writes — that message contains both the marker text (so
    # _build_identity_md still finds it) and the kickoff instruction
    # (so the SHCA acts). One conversation message instead of two.
    # Deterministic active-story focus. The active-story resolvers (snapshot +
    # the /active-story endpoint + claude_code_communication) read is_active_focus
    # with their existing key (active_thread_id / shca_instance_id) instead of the
    # ambiguous updated_at-DESC heuristic. Clear the flag on this thread's other
    # stories, then mark THIS one as the single current focus. Runs for every
    # assignment path (web / Slack / engine-initiated) since this is the choke point.
    await pool.execute(
        "UPDATE stories SET is_active_focus = false "
        "WHERE active_thread_id = $1 AND id <> $2 AND is_active_focus",
        thread_id, story_id,
    )
    await pool.execute(
        "UPDATE stories SET active_thread_id = $1, is_active_focus = true, "
        "updated_at = now() WHERE id = $2",
        thread_id, story_id,
    )
    # Successful import is the de-facto "start working" signal — move
    # the story to in_progress so the UI reflects that work has begun.
    # Covers both starting states:
    #   - "proposed"  (story exists but human hasn't accepted yet)
    #   - "accepted"  (human accepted via the story board, hasn't started)
    # Both transition straight to in_progress here. The UI's state pill
    # reads stories.state directly so this is enough for visibility.
    # Also emits a TASK_STATUS_CHANGED ledger event (stories share the
    # task event types) and fires the existing GO + Slack notifications
    # so the goal-channel + orchestrator audit is consistent.
    old_state = story.get("state")
    if old_state in ("proposed", "accepted"):
        await pool.execute(
            "UPDATE stories SET state = 'in_progress', started_at = now(), "
            "updated_at = now() WHERE id = $1 AND state IN ('proposed', 'accepted')",
            story_id,
        )
        try:
            from app.ledger.journal import append_event
            from app.ledger.schema import TASK_STATUS_CHANGED
            from app.ledger.payloads import task_status_changed_payload
            from app.task_controller import SYSTEM_ACTOR_ID
            await append_event(
                pool,
                event_type=TASK_STATUS_CHANGED,
                payload=task_status_changed_payload(
                    previous_status=old_state,
                    new_status="in_progress",
                    reason=(f"Engine-dispatched git story {story_id}" if engine_initiated
                            else f"Story imported via /work by {user_uid}"),
                ),
                actor_id=SYSTEM_ACTOR_ID,
                actor_type="system",
                context_goal_id=goal_id,
            )
        except Exception as exc:
            _log.warning(
                "Story state-change ledger emit failed (story=%s %s→in_progress): %s",
                story_id, old_state, exc,
            )

        # Engine-dispatched git stories (sync/fix) skip the user-facing "you started
        # working" notifications + channel focus rotation — the user didn't initiate it
        # and a mechanical sync shouldn't hijack their Slack current-story.
        if not engine_initiated:
            try:
                from app.services import go_events
                await go_events.on_task_state_changed(
                    goal_id, story_id, old_state, "in_progress", user_uid,
                )
            except Exception as exc:
                _log.warning(
                    "GO notification failed for story state change %s %s→in_progress: %s",
                    story_id, old_state, exc,
                )

            # Notify all connected channels (Slack, Notion, etc.)
            try:
                from app.channels.base import relay_lifecycle_event
                await relay_lifecycle_event(goal_id, "story.state_changed", {
                    "story_id": story_id,
                    "story_title": story.get("title", ""),
                    "old_state": old_state,
                    "new_state": "in_progress",
                })
            except Exception as exc:
                _log.warning(
                    "Channel lifecycle relay failed for story in_progress %s: %s", story_id, exc,
                )
    if not engine_initiated:
        try:
            from app.slack import user_goal_channels as ugc
            await ugc.set_current_story(goal_id, user_uid, story_id)
        except Exception as exc:
            _log.warning(
                "set_current_story failed for goal=%s uid=%s story=%s: %s",
                goal_id, user_uid, story_id, exc,
            )

    # Kickoff (GO→SHCA system message that wakes the gateway) is a
    # SEPARATE step — see dispatch_story_kickoff_to_assistant below. Callers
    # must invoke it after they're confident the rest of the flow has
    # had a chance to settle (e.g. Slack channel created, user invited,
    # welcome message posted) so the SHCA's reply lands in a place the
    # user can already see.

    return thread_id, agent_id


_blocked_awareness_at: dict[str, float] = {}   # story_id → last-posted epoch (per-replica dedup)
_BLOCKED_AWARENESS_TTL = 1800.0                 # don't re-post the same hold within 30 min


async def _post_blocked_awareness(
    goal_id: str, story_id: str, title: str, unmet: list,
) -> None:
    """Make the goal aware WHY a story is held at the dependent-import gate (§5.10):
    post a human-readable explanation to the GO goal thread (transcript-only — cheap,
    no inference; the GO surfaces it on its next turn, and it's visible in the goal
    thread). Deduped per story (30-min TTL) so re-dispatch polls don't spam the thread."""
    import time as _time
    now = _time.monotonic()
    last = _blocked_awareness_at.get(story_id, 0.0)
    if now - last < _BLOCKED_AWARENESS_TTL:
        return
    _blocked_awareness_at[story_id] = now
    parts = []
    for u in unmet[:5]:
        kind = "merge" if (u.get("story_kind") == "merge") else "work"
        need = "merged" if kind == "merge" else "done"
        parts.append(f"“{u.get('title') or u.get('id')}” ({kind} not yet {need} "
                     f"— {u.get('state')})")
    msg = (f"⏸️ Story “{title}” can't start yet — it depends on work "
           f"that hasn't landed: {'; '.join(parts)}. It will start automatically once "
           f"those merge/complete.")
    try:
        from app.services.go_events import _send_system_message_to_go
        await _send_system_message_to_go(goal_id, msg, wake_up=False)
    except Exception:
        _log.warning("dispatch_story_kickoff: blocked-awareness post failed story=%s", story_id)


async def dispatch_story_kickoff_to_assistant(
    goal_id: str, story_id: str, thread_id: str, agent_id: str,
    user_uid: str = "",
) -> bool:
    """Send the single GO→SHCA story-assignment message.

    This is ONE message that does three things:
      • Carries the "Story Mode Active" marker (so _build_identity_md
        finds it and IDENTITY.md scopes correctly to this story).
      • Tells the SHCA what to do (read RESOURCES.md/IDENTITY.md,
        propose a task spec).
      • Is attributed to the goal's GO agent — visible in the ledger
        and in Slack as a Goal Orchestrator message.

    Called ONLY AFTER:
      • provision_assistant_thread_for_story (DB rows, no marker insert)
      • ensure_shca_session (gateway session ready)
      • ensure_user_goal_channel (Slack channel + welcome posted)
      • refresh_story_resources (RESOURCES.md + IDENTITY.md updated
        for the active story — fresh OR rotated session)

    For an existing session being rotated to a new story, the
    refresh_story_resources call upstream is what makes the rotation
    safe — this function never recreates the session, only sends one
    message into the now-correctly-provisioned conversation.

    Returns True on success, False on any soft failure.
    """
    import json as _json
    import uuid as _uuid

    from app.auth import get_agent_actor_id
    from app.database import get_pool
    from app.ledger.journal import append_event
    from app.ledger.schema import MESSAGE_POSTED
    from app.task_controller import SYSTEM_ACTOR_ID

    pool = get_pool()

    # Idempotency guard: the same import can fire concurrently — a
    # double-clicked / two-tab pick-shca?import_story=<sid>, or Slack /work
    # overlapping a UI import. Without this guard, two parallel chat.send calls
    # race on the same gateway session, corrupting run state and causing the
    # response envelope to be lost. (Workspace cloning is separately serialized
    # by operator_provisioning._claim_or_wait_provisioning.)
    #
    # We check for an existing "# Story Mode Active" message in this
    # thread carrying THIS story's id. A recent match means a kickoff
    # has already been (or is being) dispatched — skip.
    marker_check = await pool.fetchval(
        """SELECT 1 FROM messages
            WHERE thread_id = $1
              AND content LIKE '# Story Mode Active%'
              AND content LIKE '%' || $2 || '%'
              AND created_at > NOW() - INTERVAL '2 minutes'
            LIMIT 1""",
        thread_id, story_id,
    )
    if marker_check:
        _log.info(
            "dispatch_story_kickoff: SKIP — kickoff already dispatched for "
            "story=%s thread=%s within last 2 minutes",
            story_id, thread_id[:12] if thread_id else "-",
        )
        return True

    story = await pool.fetchrow(
        """SELECT title, execution_mode, workspace_state, workspace_path,
                  shca_instance_id, current_phase
             FROM stories WHERE id = $1 AND goal_id = $2""",
        story_id, goal_id,
    )
    if not story:
        _log.warning(
            "dispatch_story_kickoff: story %s not found in goal %s — abort",
            story_id, goal_id,
        )
        return False

    # Dependent-IMPORT gate (§5.9): on git-engine goals, do NOT kick off a story until
    # ALL its dependencies are genuinely satisfied — a MERGE dep only once it actually
    # MERGED ('done'/'reviewed'), never cancelled/rejected. Hold + make the goal aware
    # of WHY. (The engine's lazy-cut already gates branch creation on the same rule;
    # this is the kickoff-boundary belt-and-suspenders + the human-readable reason.)
    try:
        from app.config import get_settings
        if get_settings().gitgraph_enabled:
            from app.services.story_transitions import unmet_story_dependencies
            unmet = await unmet_story_dependencies(pool, story_id)
            if unmet:
                _log.info("dispatch_story_kickoff: HOLD story=%s — unmet deps %s",
                          story_id, [u["id"] for u in unmet])
                await _post_blocked_awareness(goal_id, story_id, story["title"], unmet)
                return False
    except Exception:
        _log.exception(
            "dispatch_story_kickoff: dependency-gate check failed story=%s", story_id)

    # Phase 4: branch on execution_mode. Default 'ia_dispatch' uses the
    # legacy PM-mode kickoff (task spec → planner-ia pipeline). 'operator'
    # uses the new kickoff that triggers SHCA's operator loop (spawn
    # Claude Code subprocess directly).
    execution_mode = story.get("execution_mode") or "ia_dispatch"

    # Phase 4 invariant: SHCA never starts operating on an operator-mode
    # story whose workspace is not present & fresh. "Fresh" means the
    # workspace_state is 'ready' AND the workspace was provisioned for
    # the CURRENT SHCA instance (same agent+goal+thread). If the SHCA
    # has been replaced (thread deletion + re-import), the path in
    # workspace_path points at a dead workspace and we re-provision.
    current_instance_id = f"{agent_id}-{goal_id}-{thread_id}"
    workspace_is_fresh = (
        story["workspace_state"] == "ready"
        and (story.get("shca_instance_id") or "") == current_instance_id
    )
    if execution_mode == "operator" and not workspace_is_fresh:
        from app.services.operator_provisioning import provision_story_workspace
        stale_reason = (
            "workspace_state != ready"
            if story["workspace_state"] != "ready"
            else f"shca_instance_id mismatch "
                 f"(stored={story.get('shca_instance_id') or '<none>'!r}, "
                 f"current={current_instance_id!r})"
        )
        _log.info(
            "dispatch_story_kickoff: provisioning operator workspace for story=%s "
            "(%s)",
            story_id, stale_reason,
        )
        provision_result = await provision_story_workspace(
            goal_id=goal_id, story_id=story_id,
            agent_id=agent_id, thread_id=thread_id,
        )
        if not provision_result.get("ok"):
            if provision_result.get("waiting"):
                # Git engine hasn't cut the workstream branch yet. NOT an error —
                # the branch-ready listener (Component C) re-runs this kickoff once
                # the engine cuts + links the branch. Defer quietly.
                _log.info(
                    "dispatch_story_kickoff: story=%s deferred — waiting for the git "
                    "engine to cut its workstream branch; will auto-resume on "
                    "branch-ready", story_id,
                )
                return False
            _log.error(
                "dispatch_story_kickoff: ABORTING — workspace provisioning failed "
                "for story=%s in operator mode: %s",
                story_id, provision_result.get("error"),
            )
            return False
        # Re-fetch to pick up workspace_path the provisioner just set.
        story = await pool.fetchrow(
            """SELECT title, execution_mode, workspace_state, workspace_path,
                      shca_instance_id, current_phase
                 FROM stories WHERE id = $1 AND goal_id = $2""",
            story_id, goal_id,
        )

    # Phase 4 Stage 4: when the workspace is being re-provisioned (i.e.
    # this story was previously bound to a different SHCA instance and
    # is now coming back via a fresh thread / SHCA), clear any stale
    # claude_session_id. The old session file lived in the old SHCA's
    # workspace dir (which is now gone, deleted by the cleanup
    # watchdog when the old SHCA was reaped), so resuming that id from
    # the new SHCA's workspace would fail. The pre-spawn below creates
    # a fresh session in the new workspace.
    if execution_mode == "operator" and not workspace_is_fresh:
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = NULL, "
            "updated_at = NOW() WHERE id = $1 "
            "AND current_claude_session_id IS NOT NULL",
            story_id,
        )

    # Phase 4 Stage 4: pre-spawn the Claude Code session for code stories
    # right after workspace provisioning. This populates
    # stories.current_claude_session_id BEFORE SHCA's first planning
    # turn — eliminating the failure mode where SHCA forgets to spawn
    # or passes wrong params. The pre-spawn task is a no-op session-init
    # ("reply READY") — NOT the full planning briefing. SHCA still owns
    # the planning conversation; this just makes sure the session id
    # exists for it to resume.
    # Generic stories (no repo/branch resource) skip pre-spawn entirely.
    _log.info(
        "dispatch_story_kickoff: PROBE prespawn-condition story=%s "
        "execution_mode=%r workspace_path=%r story_is_dict=%s "
        "shca_instance_id=%r",
        story_id,
        execution_mode,
        (story.get("workspace_path") if story else "<no story>") if hasattr(story, "get") else "<not-mapping>",
        isinstance(story, dict) or hasattr(story, "get"),
        (story.get("shca_instance_id") if story else None) if hasattr(story, "get") else None,
    )
    if execution_mode == "operator" and story and (story.get("workspace_path") or ""):
        try:
            from app.services.operator_provisioning import prespawn_claude_for_code_story
            prespawn_result = await prespawn_claude_for_code_story(
                goal_id=goal_id,
                story_id=story_id,
                story_title=story.get("title") or "",
                instance_id=story.get("shca_instance_id") or current_instance_id,
                workspace_path=story.get("workspace_path") or "",
                thread_id=thread_id,
            )
            if prespawn_result.get("ok"):
                _log.info(
                    "dispatch_story_kickoff: prespawn ok for story=%s: %s",
                    story_id, {k: v for k, v in prespawn_result.items() if k != "text"},
                )
            else:
                _log.warning(
                    "dispatch_story_kickoff: prespawn failed for story=%s: %s — "
                    "SHCA will fall back to lazy spawn on first planning turn",
                    story_id, prespawn_result.get("error"),
                )
        except Exception:
            _log.exception(
                "dispatch_story_kickoff: prespawn raised unexpectedly for story=%s",
                story_id,
            )

    # Resolve the GO agent for this goal so the message is attributed
    # to it (sender + ledger actor). Falls back to system actor if the
    # goal somehow has no GO wired up.
    goal_row = await pool.fetchrow(
        "SELECT go_agent_id FROM goals WHERE id = $1", goal_id,
    )
    go_agent_id = (goal_row["go_agent_id"] if goal_row else "") or ""
    go_actor_id = await get_agent_actor_id(go_agent_id) if go_agent_id else None
    if not go_actor_id:
        _log.warning(
            "dispatch_story_kickoff: no GO actor_id for goal=%s "
            "(go_agent_id=%r) — falling back to SYSTEM_ACTOR_ID",
            goal_id, go_agent_id,
        )
        go_actor_id = SYSTEM_ACTOR_ID
        actor_type = "system"
        sender = f"go-{goal_id.lower()}"
    else:
        actor_type = "goal_orchestrator"
        sender = go_agent_id

    # ONE combined message: Story Mode marker + kickoff instruction.
    # The marker text "Story Mode Active" + the **ID:** field is what
    # _build_identity_md scans for to detect the active story; the
    # kickoff prose is what tells the SHCA to act.
    if execution_mode == "operator":
        # Operator-mode kickoff. The story workspace has already been
        # provisioned on the openclaw VM (clone, scaffolding, credentials)
        # before this message is composed — SHCA's job is just to spawn
        # Claude Code in that workspace and drive it.
        ws_path = (story["workspace_path"] if story else "") or "(workspace not provisioned — investigate)"

        # The first-turn message we send to Claude. Embedded as a Python
        # literal so the shape stays controlled; SHCA inlines this verbatim
        # in the `task` field of the first sessions_spawn call.
        claude_first_turn = (
            "# Story: {title}\n"
            "ID: {sid}\n\n"
            "You are the operator for this story. You will:\n"
            "1. Plan the work (with the human) by writing a plan file "
            "with an explicit Task DAG.\n"
            "2. After the human approves the plan, execute each task "
            "in the DAG yourself — **in this same session**, one task "
            "per turn, in the order the control panel dispatches.\n\n"
            "## Phase \"roles\"\n\n"
            "The DAG you produce will assign each task a `phase`. When a "
            "task arrives, the control panel will tell you which role to "
            "adopt **for that task only** and will inline the role's "
            "behavioural rules. The available phases are:\n\n"
            "- **`code-explorer`** — read-only exploration of the repo. "
            "Use it as a DAG step when a chunk of work needs upfront "
            "discovery whose findings should be visible to the human as "
            "their own task card.\n"
            "- **`coder`** — write/modify source files per the task spec.\n"
            "- **`tester`** — write and run tests; iterate on failures.\n"
            "- **`code-reviewer`** — review the staged diff with "
            "confidence-scored findings.\n\n"
            "You DON'T spawn subagents (no `Task` tool invocations). "
            "Everything happens in this main session so you keep full "
            "context — the plan you wrote, every prior task's edits, "
            "and the conversation history — across all phases.\n\n"
            "## Phase 1 — gather context (do this first, in THIS turn)\n\n"
            "- Read `RESOURCES.md` and `IDENTITY.md` (they live at your "
            "`cwd`; small files)\n"
            "- Scan `./repo` directly — `ls`, `Glob`, `Read` key files — "
            "to understand the existing codebase, conventions, similar "
            "features, and anything that affects how this story should "
            "be built. You're the explorer right now; no need to "
            "delegate.\n\n"
            "## Phase 2 — ask clarifying questions BEFORE drafting a plan\n\n"
            "After exploration, ASK the human 2–4 focused questions. "
            "Examples:\n"
            "- *\"I see X in the repo — should the new work coexist or "
            "replace it?\"*\n"
            "- *\"Desired behavior for edge case Y?\"*\n"
            "- *\"Convention A or B for this?\"*\n"
            "- *\"What's explicitly out of scope?\"*\n\n"
            "Prefer asking over guessing. 2–4 high-leverage questions "
            "per turn — not 10. Iterate as many rounds as needed.\n\n"
            "## Phase 3 — write the plan WITH A TASK DAG\n\n"
            f"Once you have enough context, write the plan to "
            f"`repo/stories/{story_id}/plans/plan_<feature-name>.md` — "
            f"**inside the git repo** under the story's own subdirectory. "
            f"If `repo/stories/{story_id}/plans/` doesn't exist yet, create it. "
            f"After plan approval, the control panel commits the plan file "
            f"to the user's branch alongside the eventual implementation so "
            f"the plan + code ship together in git history.\n\n"
            "The plan file MUST contain these sections:\n\n"
            "- **Goal** — one paragraph\n"
            "- **Scope** — explicit in / out lists\n"
            "- **Approach** — file changes, key patterns, design choices\n"
            "- **Test strategy** — what gets tested\n"
            "- **Risks + mitigations**\n"
            "- **Task DAG** — sequential plan as a YAML block (see format below)\n\n"
            "### Task DAG format\n\n"
            "The plan file MUST contain a single fenced YAML block under "
            "the heading `## Task DAG`. The control panel parses this "
            "block when the plan is approved and creates one task row "
            "per entry. Format exactly:\n\n"
            "````\n"
            "## Task DAG\n\n"
            "```yaml\n"
            "tasks:\n"
            "  - id: T1\n"
            "    phase: coder\n"
            "    title: <short title>\n"
            "    description: |\n"
            "      <multi-line task description; include file paths,\n"
            "      function names, acceptance criteria>\n"
            "    acceptance: <one-line acceptance criterion>\n"
            "    depends_on: []\n"
            "  - id: T2\n"
            "    phase: unit-tester\n"
            "    title: <short title>\n"
            "    description: |\n"
            "      ...\n"
            "    acceptance: ...\n"
            "    depends_on: [T1]\n"
            "  - id: T3\n"
            "    phase: integration-tester\n"
            "    title: <short title>\n"
            "    description: |\n"
            "      ...\n"
            "    acceptance: ...\n"
            "    depends_on: [T1, T2]\n"
            "  - id: T4\n"
            "    phase: code-reviewer\n"
            "    title: Review the diff\n"
            "    description: |\n"
            "      ...\n"
            "    acceptance: No blocking issues.\n"
            "    depends_on: [T1, T2, T3]\n"
            "  - id: T5\n"
            "    phase: deployment\n"
            "    title: Deploy to staging\n"
            "    description: |\n"
            "      ...\n"
            "    acceptance: Service healthy after deploy.\n"
            "    depends_on: [T4]\n"
            "```\n"
            "````\n\n"
            "Rules for the DAG:\n"
            "- Each task's `phase` MUST be one of: `code-explorer`, "
            "`coder`, `unit-tester`, `integration-tester`, "
            "`code-reviewer`, `deployment`.\n"
            "- `id` is short (e.g. T1, T2). `depends_on` references "
            "earlier ids.\n"
            "- Keep the DAG focused — typical stories need 3–8 tasks. "
            "If you're at >10, the plan is probably too granular.\n"
            "- Always include a `code-reviewer` task before the end. "
            "Always include `integration-tester` after `coder`/`unit-tester` "
            "for any story that touches runnable code — this includes logic "
            "changes, logging, config wiring, app-level env var changes, or "
            "anything that affects executed code paths. Skip it only for pure "
            "documentation or pure infrastructure files (e.g. cloudbuild.yaml, "
            "k8s manifests) that do not affect the running application.\n"
            "- **`deployment` phase decision — check for CI/CD first, then "
            "ASK before omitting**. Before writing the DAG, scan the repo for "
            "CI/CD config that already handles deploys: `.github/workflows/`, "
            "`cloudbuild.yaml`, `cloudbuild-*.yaml`, `.gitlab-ci.yml`, "
            "`Jenkinsfile`, `bitbucket-pipelines.yml`, `fly.toml`, "
            "`vercel.json`, `netlify.toml`, `render.yaml`, deploy scripts under "
            "`scripts/deploy*`, `Makefile` targets named deploy/release. "
            "Then handle one of three cases:\n"
            "  (a) Story scope explicitly requires a deploy step AND the human "
            "asked for one → include `deployment`.\n"
            "  (b) CI/CD config is present and the story's changes will be "
            "deployed automatically when the PR merges → DO NOT silently "
            "omit `deployment`. First tell the human in chat: 'I see CI/CD "
            "at <path> that auto-deploys on merge to <branch>. I'm planning "
            "to omit a `deployment` task from the DAG since CI handles it — "
            "OK?' Wait for human confirmation, then proceed.\n"
            "  (c) No CI/CD config and no explicit deploy ask → omit "
            "`deployment` (default safe choice for library/script changes).\n"
            "Never silently skip `deployment` in case (b) — the human must "
            "acknowledge.\n"
            "- **Task descriptions are phase-scoped — never blend phase "
            "techniques.** Each task's `description` must specify *what to do "
            "in the language of THAT phase's role*. Concretely:\n"
            "  - `unit-tester` descriptions can reference test files, pytest / "
            "jest / vitest, mocks, fakes, monkey-patching, PYTHONPATH shadow "
            "modules — these are legitimate unit-test scaffolding.\n"
            "  - `integration-tester` descriptions must NOT mention any of "
            "those. Integration-tester runs the real deliverable as a real "
            "subprocess against real dependencies; mocks/fakes are forbidden "
            "by its role. Describe what to *validate* (acceptance criteria + "
            "observed-against-expected), never how to *scaffold tests*. If "
            "you find yourself writing 'use a fake X for scenario Y' in an "
            "integration-tester task, you are mis-routing it to the wrong "
            "phase — move that scaffolding to a `unit-tester` task instead.\n"
            "  - `code-reviewer` descriptions describe what to look for in "
            "the diff (correctness, style, edge cases), not how to test.\n"
            "  - `deployment` descriptions describe the deploy mechanism + "
            "post-deploy smoke checks, not unit/integration validation.\n"
            "- **`integration-tester` tasks must enumerate runtime fixtures.** "
            "Before writing the task, list every value the validator will need "
            "to exercise each acceptance criterion: bucket names, object keys, "
            "API endpoints, expected response snippets, sample inputs, env "
            "variables beyond credentials, etc. If any fixture value is unknown "
            "(e.g. 'which bucket should we test against?', 'what's a PDF "
            "object name in that bucket?'), **ASK the human in chat during "
            "planning** and embed the answer in the task description. Do not "
            "leave fixtures to be discovered by the validator — that triggers "
            "an avoidable BLOCKED-iteration loop at execution time. Things "
            "the validator can derive at runtime (a UUID for a known-missing "
            "object name, a timestamp suffix) don't need to be enumerated.\n"
            "- **YAML quoting rules** (the block is parsed as YAML):\n"
            "  - Do NOT wrap values in backticks (markdown inline-code "
            "is invalid YAML). Plain text or double-quoted strings only.\n"
            "  - If a value contains `:`, `#`, or starts with `*`, `&`, "
            "`!`, `|`, `>`, wrap it in `\"double quotes\"`.\n"
            "  - Multi-line values: use the `|` block-scalar indicator "
            "(as shown in the `description:` examples above).\n\n"
            "After writing the file, tell the human in chat: which file "
            "you wrote, a one-paragraph summary, and ask them to review "
            "in the file viewer.\n\n"
            "## Phase 4 — iterate on the plan\n\n"
            "The human may have feedback. Update the SAME plan file in "
            "place (don't bump versions unless asked) and briefly say "
            "what changed.\n\n"
            "## Phase 5 — wait for approval. DO NOT EXECUTE YET.\n\n"
            "After plan approval, the control panel parses your DAG and "
            "dispatches tasks back to you one at a time as new turns. "
            "Each dispatch will look like:\n\n"
            "```\n"
            "[DISPATCH] Task T<id> · phase=<phase>\n"
            "Title: <title>\n"
            "Description: <description>\n"
            "Acceptance: <acceptance>\n\n"
            "Adopt the <phase> role for this task only:\n"
            "<the phase's behavioural rules, inlined here>\n"
            "```\n\n"
            "When you receive a dispatch:\n"
            "- Do the work yourself, in this session, following the "
            "inlined phase rules.\n"
            "- End your reply with EXACTLY one of these sentinels on its "
            "own line:\n"
            "  - `PHASE_TASK_DONE T<id> — <one-line summary>` (success)\n"
            "  - `PHASE_TASK_BLOCKED T<id> — <one specific question for "
            "the human>` (need input — one question per round, no multi-"
            "part; you can be blocked multiple times on the same task)\n"
            "  - `PHASE_TASK_FAILED T<id> — <one-line failure reason>` "
            "(hard error you can't recover from; gives up on this task)\n"
            "- If you BLOCK, the next turn arrives as:\n\n"
            "  ```\n"
            "  [ANSWER T<id>] You asked: \"<question>\"\n"
            "  Human answered: \"<answer>\"\n"
            "  Continue.\n"
            "  ```\n\n"
            "  You keep full session context — pick up where you left "
            "off. The plan, prior task results, and workspace files are "
            "all still on disk if you need to re-read them. Multi-round "
            "Q&A is fine; ask as many questions as you need (one per "
            "round) until you can finish.\n"
            "- During execution, **do NOT commit or push**. The control "
            "panel handles the final commit + push after a separate "
            "diff-review gate.\n\n"
            "Begin now: read RESOURCES.md and IDENTITY.md, scan `./repo`, "
            "then ask your first 2–4 clarifying questions."
        ).format(title=story["title"], sid=story_id)

        # Phase 4 Stage 4: the operator-mode kickoff message is now a
        # short pointer. The behavioral content (how to plan, sentinel
        # grammar, etc.) lives in skills/shca_*/SKILL.md, dispatched
        # via AGENTS.md routing based on the state snapshot. The full
        # claude_first_turn briefing — what SHCA pastes to Claude on
        # the first sessions_spawn call — lives inside
        # skills/assistant_code_planning/SKILL.md and is fetched by SHCA at
        # planning time.
        # Use the actual current_phase from the DB rather than hardcoding,
        # since the dispatcher may have already transitioned the story
        # (e.g. discovery → planning) before this kickoff lands.
        _current_phase = (story.get("current_phase") if isinstance(story, dict) else None) or "discovery"
        combined = (
            f"# Story Mode Active\n\n"
            f"Story `{story_id}` — \"{story['title']}\"\n\n"
            f"Type: code (Claude Code involved)\n"
            f"Phase: {_current_phase}\n"
            f"Workspace: `{ws_path}`\n\n"
            f"A `story_state_snapshot` block accompanies the next human turn with "
            f"the authoritative current state. Route yourself using the skill-"
            f"routing table in your AGENTS.md: read the phase-specific skill it "
            f"names for the current `next_action.state` / phase (e.g. "
            f"`skills/assistant_discovery/SKILL.md` while in discovery), then act. "
            f"The snapshot is the source of truth — prefer it over any value in "
            f"this kickoff."
        )

        # Legacy long-form kickoff kept disabled below for reference.
        # Pre-refactor it carried the entire operator playbook inline;
        # that content has moved into the skill registry (migration
        # 048). Do not re-enable without first redeploying the old
        # template_files.AGENTS.md content from 'SHCA context old/'.
        _legacy_unused_combined = (
            f"# Story Mode Active\n\n"
            f"## Your Story\n"
            f"**Title:** {story['title']}\n"
            f"**ID:** `{story_id}`\n"
            f"**Mode:** Operator (Claude Code subprocess)\n"
            f"**Workspace:** `{ws_path}`\n"
            f"**Repo:** `{ws_path}/repo`\n\n"
            f"The story workspace is already provisioned on the VM. The repo "
            f"branch is cloned at `./repo`, credentials are configured for "
            f"git push/pull, and the scaffolding (RESOURCES.md, IDENTITY.md, "
            f"docs/, .claude/) is in place. **Do NOT call `custom_worktree`** — "
            f"that tool is deprecated; the control panel did this work for "
            f"you.\n\n"
            f"Do NOT produce a task spec or call `create_task` — that "
            f"path is for `ia_dispatch` mode only.\n\n"
            f"## Your operator job\n\n"
            f"You are a thin coordinator between the human (this chat) and a "
            f"Claude Code subprocess running in the story workspace. During "
            f"planning, you relay the human's chat verbatim to Claude and "
            f"Claude's output verbatim to the human. The plan is a file "
            f"the human reads; the chat is the conversation that shapes it.\n\n"
            f"After plan approval, **Claude does NOT implement on its own**. "
            f"The control panel parses Claude's Task DAG and dispatches tasks "
            f"one at a time back to the same Claude session — each task "
            f"invokes a specialised subagent (coder, tester, code-reviewer). "
            f"Your only role during execution is the relay.\n\n"
            f"### Step 1 — story state and phase\n\n"
            f"Call `story_state` to transition the story to `in_progress`. "
            f"Call `story_phase` to set phase to `planning`.\n\n"
            f"### Step 2 — spawn Claude (first turn = the discovery kickoff)\n\n"
            f"Call `sessions_spawn` with **these EXACT arguments** (only "
            f"`task` is templated — copy the rest verbatim):\n\n"
            f"```json\n"
            f"{{\n"
            f'  "runtime": "cli",\n'
            f'  "cwd": "{ws_path}",\n'
            f'  "storyId": "{story_id}",\n'
            f'  "task": "<see below — paste the kickoff text exactly>"\n'
            f"}}\n"
            f"```\n\n"
            f"`storyId` is required so Claude's PostToolUse hook can "
            f"surface live activity (\"🔍 Reading X\", \"✏️ Editing Y\") "
            f"to this chat as Claude works.\n\n"
            f"The `task` field must be **exactly this text** (copy verbatim, "
            f"including newlines):\n\n"
            f"````\n"
            f"{claude_first_turn}\n"
            f"````\n\n"
            f"`cwd` is the **story workspace dir**, NOT the repo subdir — "
            f"Claude needs to see RESOURCES.md, IDENTITY.md, docs/, and "
            f"`.claude/settings.json`. Do NOT use `runtime=\"acp\"` or "
            f"`runtime=\"subagent\"`. The result will include `sessionId` — "
            f"**remember it**; you'll need it for every subsequent turn.\n\n"
            f"### Step 3 — relay each human turn to Claude\n\n"
            f"After the first spawn returns, post Claude's `text` field back "
            f"to this chat verbatim (so the human sees Claude's questions). "
            f"For every subsequent message the human sends in this chat "
            f"while phase is `planning`, call `sessions_spawn` AGAIN with:\n\n"
            f"```json\n"
            f"{{\n"
            f'  "runtime": "cli",\n'
            f'  "cwd": "{ws_path}",\n'
            f'  "storyId": "{story_id}",\n'
            f'  "resumeSessionId": "<sessionId from the first spawn>",\n'
            f'  "task": "<the human\'s message verbatim>"\n'
            f"}}\n"
            f"```\n\n"
            f"and post Claude's response back. You are a **transparent relay** "
            f"during the planning conversation. Don't paraphrase or "
            f"summarize Claude's output — paste it.\n\n"
            f"### Step 4 — surface when a plan file lands\n\n"
            f"When Claude tells you it has written/updated a plan file "
            f"(e.g. `repo/stories/{story_id}/plans/plan_<feature>.md`), include that path explicitly in "
            f"your reply to the human. They'll click it in the file viewer "
            f"to review.\n\n"
            f"### Step 5 — plan approval (control panel handles this)\n\n"
            f"The human has an **\"✓ Approve plan\"** button in their UI "
            f"phase banner. When clicked, the control panel reads the "
            f"current plan file, parses the Task DAG, creates task rows, "
            f"commits + pushes the plan, and starts dispatching tasks "
            f"automatically. **Do NOT tell Claude to implement** — Claude "
            f"does NOT implement on its own. The dispatcher sends "
            f"individual `[DISPATCH] Task T<id>` turns and Claude executes "
            f"each in its inlined phase role.\n\n"
            f"If the human says \"approved\" / \"looks good\" in chat "
            f"instead of clicking the button, reply with: *\"Click "
            f"'✓ Approve plan' in the phase banner to start execution.\"* "
            f"and stop — don't try to dispatch yourself.\n\n"
            f"### Step 6 — execution-phase relay\n\n"
            f"Once execution starts, you're a **transparent relay** for "
            f"human ↔ Claude messages. Most relay happens *mechanically* "
            f"in the control panel — when the human types a chat while a "
            f"phase task is blocked, the control panel auto-forwards it "
            f"to Claude as `[ANSWER T<id>]`. You don't need to do "
            f"anything for those. Just stay quiet during execution "
            f"unless the human asks a meta-question (\"what's claude "
            f"doing?\", \"how many tasks left?\").\n\n"
            f"### Error handling\n\n"
            f"If `sessions_spawn` returns `\"invalid runtime\"` or any schema "
            f"rejection, the openclaw build is stale — say so explicitly and "
            f"STOP. Do NOT fall back to other runtimes or to writing a task "
            f"spec."
        )
    else:
        # Default 'ia_dispatch' mode — the existing PM-mode kickoff.
        combined = (
            f"# Story Mode Active\n\n"
            f"## Your Story\n"
            f"**Title:** {story['title']}\n"
            f"**ID:** `{story_id}`\n\n"
            f"Your IDENTITY.md and RESOURCES.md have been refreshed for this "
            f"story. Read them, then propose a task spec — the human will "
            f"approve before you dispatch."
        )

    msg_id = str(_uuid.uuid4())

    # messages.role CHECK constraint only allows 'human' | 'agent'.
    # The marker has historically been stored as role='human' so
    # _build_identity_md picks it up; we keep that convention.
    #
    # Idempotency (race-safe): SELECT FOR UPDATE on the story row
    # serialises concurrent kickoff dispatches. The second caller waits
    # for the first to commit, then re-checks for an existing marker
    # and bails out. Without this, two callers that both passed the
    # top-of-function check (race window between check and INSERT)
    # would both persist the kickoff message.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT id FROM stories WHERE id = $1 FOR UPDATE",
                story_id,
            )
            existing = await conn.fetchval(
                """SELECT 1 FROM messages
                    WHERE thread_id = $1
                      AND content LIKE '# Story Mode Active%'
                      AND content LIKE '%' || $2 || '%'
                      AND created_at > NOW() - INTERVAL '2 minutes'
                    LIMIT 1""",
                thread_id, story_id,
            )
            if existing:
                _log.info(
                    "dispatch_story_kickoff: SKIP at persist — concurrent "
                    "caller already inserted kickoff for story=%s thread=%s",
                    story_id, thread_id[:12] if thread_id else "-",
                )
                return True
            await conn.execute(
                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                   VALUES ($1, $2, $3, 'human', $4, 0)""",
                msg_id, thread_id, sender, combined,
            )
            await append_event(
                conn,
                event_type=MESSAGE_POSTED,
                payload={
                    "msg_id":         msg_id,
                    "role":           "system",   # semantic role; UI/parsers can render as system
                    "content":        combined,
                    "sender":         sender,
                    "sender_agent":   go_agent_id,
                    "recipient":      agent_id,
                    "kind":           "story_assignment",
                    "story_id":       story_id,
                    "triggered_by":   user_uid,
                },
                actor_id=go_actor_id,
                actor_type=actor_type,
                context_goal_id=goal_id,
                context_conversation_id=thread_id,
            )

    _log.info(
        "Logged GO→SHCA story-assignment in ledger: goal=%s story=%s "
        "agent=%s thread=%s sender=%s msg=%s",
        goal_id, story_id, agent_id,
        thread_id[:12] if thread_id else "-",
        sender, msg_id[:8],
    )

    # Mirror GO story-assignment to the SHCA's Slack channel so the user
    # sees the handoff message before the SHCA's reply arrives.
    try:
        from app.channels.base import relay_to_thread_channel
        await relay_to_thread_channel(goal_id, thread_id, combined, username="Goal Orchestrator")
    except Exception as exc:
        _log.warning(
            "Channel thread relay for GO story-assignment failed (goal=%s story=%s): %s",
            goal_id, story_id, exc,
        )

    # Send to the gateway so the SHCA processes it and replies. By the
    # time we get here refresh_story_resources upstream has already
    # written RESOURCES.md / IDENTITY.md for the current story — the
    # SHCA sees a consistent workspace.
    try:
        from app.services.agent_driver import ensure_shca_session
        session = await ensure_shca_session(goal_id, agent_id, thread_id)
        if session is None:
            _log.warning(
                "dispatch_story_kickoff: ensure_shca_session returned None "
                "for goal=%s thread=%s — skipping gateway send",
                goal_id, thread_id[:12] if thread_id else "-",
            )
            return False
        # persisted_message_id: the row was INSERTed above (msg_id). Without
        # this, the chat_module queue path would INSERT a *second* row and the
        # chat UI would render the kickoff twice.
        await session.send_to_gateway(
            _json.dumps({
                "type":      "message",
                "content":   combined,
                "thread_id": thread_id,
            }),
            persisted_message_id=msg_id,
            queue_kind="system_wake",
        )
        _log.info(
            "Dispatched GO story-assignment to SHCA gateway: goal=%s story=%s thread=%s",
            goal_id, story_id, thread_id[:12] if thread_id else "-",
        )
        return True
    except Exception as exc:
        _log.warning(
            "Story-kickoff gateway send failed (goal=%s story=%s thread=%s): %s",
            goal_id, story_id, thread_id[:12] if thread_id else "-", exc,
        )
        return False


# ── Admin: Template Management ────────────────────────────────────────────────

@router.get("/admin/assistant-templates", response_class=HTMLResponse)
async def assistant_templates_page(request: Request, user: dict = Depends(require_admin)):
    """Redirects to consolidated Agent Registry filtered by SHCA type."""
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url="/settings/agent-registry?view=templates&type=SHCA", status_code=302)


@router.post("/admin/assistant-templates")
async def create_template(request: Request, user: dict = Depends(require_admin)):
    """Create a new SHCA template."""
    form = await request.form()
    from app.llm_models import normalize_model_id
    template_id = form.get("template_id", "").strip()
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    # Fall back to empty so create_assistant_template applies DEFAULT_SHCA_MODEL
    # — keeps a single source of truth in app/llm_models.py.
    model = normalize_model_id(form.get("model", "").strip())
    agents_md = form.get("agents_md", "")
    soul_md = form.get("soul_md", "")

    if not template_id or not name:
        raise HTTPException(status_code=400, detail="template_id and name are required")

    try:
        await create_assistant_template(
            template_id, name, description, model,
            agents_md, soul_md,
            user["uid"], user["actor_id"],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return RedirectResponse(url="/admin/assistant-templates", status_code=303)


@router.get("/admin/assistant-templates/{template_id}", response_class=HTMLResponse)
async def template_detail(template_id: str, request: Request, user: dict = Depends(require_admin)):
    """View/edit a template."""
    tpl = await get_assistant_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "admin/assistant_template_detail.html",
        {"request": request, "user": user, "template": tpl},
    )


@router.post("/admin/assistant-templates/{template_id}/files")
async def update_template_files(template_id: str, request: Request, user: dict = Depends(require_admin)):
    """Update template files and fan out to per-user templates and live instances."""
    form = await request.form()
    files = {fn: form[fn] for fn in ["AGENTS.md", "SOUL.md"] if form.get(fn) is not None}
    if files:
        await update_assistant_template_files(template_id, files, changed_by=user["uid"])
    return RedirectResponse(url=f"/admin/assistant-templates/{template_id}", status_code=303)


# ── User: Assistant Configuration ────────────────────────────────────────────

@router.get("/settings/my-agents", response_class=HTMLResponse)
async def my_agents_page(request: Request, user: dict = Depends(get_current_user)):
    """Read-only view of the user's assistant.

    Assistants are fully automatic: `ensure_user_assistant` auto-creates one per
    user from the default template on login, with USER.md/IDENTITY.md generated
    from the user's account. There is no create / edit / model-change here."""
    user_assistants = await get_user_assistants(user["actor_id"])
    return templates.TemplateResponse(
        "settings/my_agents.html",
        {
            "request": request, "user": user,
            "user_assistants": user_assistants,
        },
    )


# NOTE: The assistant create / edit / model-change routes were removed —
# assistants are now fully automatic. `ensure_user_assistant` auto-creates one
# per user from the default template on login, with USER.md/IDENTITY.md
# generated from the user's account. Users cannot create, edit, or re-model
# their assistant; /settings/my-agents is a read-only view. (The GO model
# picker is unaffected — see app/routers/goals.py.)


# ── SHCA Picker (for new conversations) ──────────────────────────────────────

@router.get("/goals/{pid}/chat/pick-assistant", response_class=HTMLResponse)
async def pick_assistant_for_chat(pid: str, request: Request, user: dict = Depends(get_current_user)):
    """Open the user's persistent SHCA conversation for this goal.

    Post-pivot behaviour: each user has ONE SHCA conversation per goal,
    reused across stories. This route resolves (or creates) that
    persistent thread and redirects to it. If `?import_story=…` is set,
    the story is rotated in via a fresh "Story Mode Active" marker —
    the conversation history persists.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != "admin":
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Authorisation guard: when importing a story, only the assigned
    # user may do so.
    import_story_id = request.query_params.get("import_story", "")
    if import_story_id:
        story_row = await pool.fetchrow(
            "SELECT assigned_to, story_kind FROM stories WHERE id = $1 AND goal_id = $2",
            import_story_id, pid,
        )
        # A merge story is a git-land, not assistant work — importing it would spawn an
        # SHCA to "work" something the engine performs on PR-merge. Refuse.
        if story_row and (story_row["story_kind"] or "code") == "merge":
            return flash_redirect(
                f"/goals/{pid}/stories",
                "Merge stories are landed by the Git Engine — they can't be opened in an assistant.",
                flash_type="warning",
            )
        if story_row and story_row["assigned_to"] and story_row["assigned_to"] != user["uid"]:
            _log.warning(
                "BLOCKED: user %s tried to import story %s assigned to %s",
                user["uid"], import_story_id, story_row["assigned_to"],
            )
            return flash_redirect(
                f"/goals/{pid}/stories",
                "This story is assigned to another user.",
                flash_type="warning",
            )
        # Dependent-IMPORT gate (§5.9): on git-engine goals, refuse the import (before
        # binding/marking in_progress) until every dependency has truly landed — a MERGE
        # dep only when MERGED. Prevents starting a consumer against a trunk missing the
        # producer's work.
        try:
            from app.config import get_settings as _get_settings
            if _get_settings().gitgraph_enabled:
                from app.services.story_transitions import unmet_story_dependencies
                _unmet = await unmet_story_dependencies(pool, import_story_id)
                if _unmet:
                    names = ", ".join((u["title"] or u["id"]) for u in _unmet[:5])
                    return flash_redirect(
                        f"/goals/{pid}/stories",
                        (f"Can't start this story yet — waiting on: {names}. "
                         f"It becomes available once those merge/complete."),
                        flash_type="warning",
                    )
                # NOTE: we deliberately do NOT gate on a cut branch here. Operator
                # provisioning lazy-waits for the engine to cut the workstream branch:
                # provision_story_workspace auto-reconciles (Component B — derives the
                # git_node + wakes the engine) and, while the node is PLANNED/BRANCHING,
                # returns {"waiting": True} so dispatch_story_kickoff defers quietly; the
                # branch-ready listener (Component C) re-provisions once the branch exists.
                # An earlier branch-readiness gate here DEADLOCKED brand-new stories: it
                # refused the import before provisioning could run the auto-reconcile, so
                # the branch (only cut once a member reaches in_progress) was never cut.
        except HTTPException:
            raise
        except Exception:
            _log.exception(
                "pick-shca: dependency-gate check failed for story=%s", import_story_id)

    from app.services import execution_control  # noqa: PLC0415
    try:
        thread_id, agent_id = await provision_assistant_thread_for_story(
            user["uid"], pid, import_story_id,
        )
    except execution_control.ExecutionPausedError as exc:
        # Kill switch engaged — toast instead of raising, since this is a
        # full-page GET nav (raising here renders as a raw JSON page, not a UI toast).
        return flash_redirect(f"/goals/{pid}/stories", str(exc), flash_type="warning")
    except RuntimeError as exc:
        return flash_redirect(f"/goals/{pid}/stories", str(exc), flash_type="error")

    # When importing a story, run the same kickoff flow as Slack's /work
    # command:
    #   1. refresh_story_resources — re-renders RESOURCES.md + IDENTITY.md
    #      for the new story (without this, IDENTITY says story=no even
    #      after rotation because _build_identity_md scans messages for
    #      the marker — see openclaw.py).
    #   2. dispatch_story_kickoff_to_assistant — posts the "# Story Mode
    #      Active" marker message (visible to the user as a context-loaded
    #      banner) and wakes the gateway session.
    # Both calls are best-effort: provisioning succeeded, so we redirect
    # to the chat regardless. Failures are logged but not fatal.
    if import_story_id:
        try:
            from app.resource_provisioning import refresh_story_resources
            await refresh_story_resources(pid, import_story_id)
        except Exception as exc:
            _log.warning(
                "pick-shca: refresh_story_resources failed (goal=%s story=%s): %s",
                pid, import_story_id, exc,
            )
        try:
            await dispatch_story_kickoff_to_assistant(
                goal_id=pid,
                story_id=import_story_id,
                thread_id=thread_id,
                agent_id=agent_id,
                user_uid=user["uid"],
            )
        except Exception as exc:
            _log.warning(
                "pick-shca: dispatch_story_kickoff_to_assistant failed "
                "(goal=%s story=%s thread=%s): %s",
                pid, import_story_id, thread_id[:12] if thread_id else "-", exc,
            )

    return RedirectResponse(
        url=f"/goals/{pid}/chat/{agent_id}?thread_id={thread_id}",
        status_code=303,
    )


# ── Story workspace file viewer (Phase 4 Stage 1) ────────────────────────────
#
# Read-only proxies to the gateway's shca.workspace.{listFiles,readFile} RPCs.
# The chat UI's right-rail file viewer uses these to render the story workspace
# tree and individual file contents.

from fastapi.responses import JSONResponse  # noqa: E402  (intentionally after imports)


async def _resolve_story_workspace_instance(
    user_uid: str, goal_id: str, story_id: str,
) -> tuple[str, str]:
    """Resolve (shca_instance_id, agent_template_id) for a story the user can
    access. Raises HTTPException on auth or lookup failure."""
    role = await get_goal_role(user_uid, goal_id)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT shca_instance_id, workspace_path FROM stories
            WHERE id = $1 AND goal_id = $2""",
        story_id, goal_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")
    instance_id = (row["shca_instance_id"] or "").strip()
    if not instance_id:
        raise HTTPException(
            status_code=409,
            detail="story workspace has not been provisioned yet",
        )
    return instance_id, row.get("workspace_path") or ""


@router.get("/goals/{pid}/stories/{sid}/workspace/tree")
async def get_workspace_tree(
    pid: str, sid: str,
    user: dict = Depends(get_current_user),
):
    """Return JSON list of files in the story workspace.

    Shape: { entries: [{path, type, size, mtimeMs}, ...], truncated: bool }
    """
    instance_id, ws_path = await _resolve_story_workspace_instance(
        user["uid"], pid, sid,
    )

    from app.openclaw import gateway_admin, GatewayError
    # Pass the story's real provisioned dir so the gateway walks it directly
    # instead of re-deriving from the (lowercased/truncated) agent id — which
    # misses the worktree for long / uppercase-goal-id agents (e.g. RLAGT).
    _params = {"agentId": instance_id, "storyId": sid}
    if ws_path:
        _params["workspacePath"] = ws_path
    try:
        result = await gateway_admin.call(
            "shca.workspace.listFiles",
            _params,
            timeout=30,
        )
    except GatewayError as exc:
        _log.warning(
            "workspace tree fetch failed (story=%s instance=%s): %s",
            sid, instance_id, exc,
        )
        raise HTTPException(status_code=502, detail=f"gateway error: {exc}")

    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=str(result))
    return JSONResponse({
        "entries": result.get("entries") or [],
        "truncated": bool(result.get("truncated")),
    })


@router.get("/goals/{pid}/stories/{sid}/workspace/file")
async def get_workspace_file(
    pid: str, sid: str, path: str,
    user: dict = Depends(get_current_user),
):
    """Return a single file's contents from the story workspace.

    Shape: { path, content, mime, size, truncated }
    `path` is a query param (relative path under the story workspace root).
    """
    if not path or len(path) > 1024:
        raise HTTPException(status_code=400, detail="missing or oversized path")

    instance_id, ws_path = await _resolve_story_workspace_instance(
        user["uid"], pid, sid,
    )

    from app.openclaw import gateway_admin, GatewayError
    _params = {"agentId": instance_id, "storyId": sid, "path": path}
    if ws_path:
        _params["workspacePath"] = ws_path  # see get_workspace_tree
    try:
        result = await gateway_admin.call(
            "shca.workspace.readFile",
            _params,
            timeout=30,
        )
    except GatewayError as exc:
        # Distinguish "file not found / bad path" (gateway INVALID_REQUEST)
        # from real failures.
        msg = str(exc)
        if "file not found" in msg or "not a file" in msg or "escapes" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "must be relative" in msg or "INVALID_REQUEST" in msg:
            raise HTTPException(status_code=400, detail=msg)
        _log.warning(
            "workspace file read failed (story=%s path=%s): %s",
            sid, path, exc,
        )
        raise HTTPException(status_code=502, detail=msg)

    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=str(result))
    return JSONResponse({
        "path": result.get("path") or path,
        "content": result.get("content") or "",
        "mime": result.get("mime") or "application/octet-stream",
        "size": int(result.get("size") or 0),
        "truncated": bool(result.get("truncated")),
    })


# ── Plan approval endpoint (Phase 4 Stage 3-d) ───────────────────────────────

@router.post("/goals/{pid}/stories/{sid}/plan/approve")
async def approve_plan_endpoint(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Human approves the latest plan file. Triggers DAG ingestion +
    plan-file commit + task dispatch. Returns JSON suitable for HTMX
    to swap into a status card."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    form = await request.form() if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded") else None
    plan_path = (form.get("plan_path") if form else None) or None

    from app.services.plan_approval import approve_plan
    result = await approve_plan(
        goal_id=pid,
        story_id=sid,
        approved_by_uid=user["uid"],
        plan_path=plan_path,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/goals/{pid}/stories/{sid}/diff/decide")
async def decide_diff_endpoint(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Human approves or rejects the implementation diff."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    form = await request.form()
    approval_id = form.get("approval_id", "").strip()
    decision = form.get("decision", "").strip()
    feedback = form.get("feedback", "")
    if not approval_id or decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="approval_id + decision (approve|reject) required")

    from app.services.diff_review import decide_diff_review
    result = await decide_diff_review(
        approval_id=approval_id,
        decision=decision,
        feedback=feedback,
        decided_by_uid=user["uid"],
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/goals/{pid}/stories/{sid}/recover")
async def story_recover_endpoint(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Human-triggered claude-code recovery for a failed/current story DAG
    (assistant-workflow refactor Phase 3): replan | retry | abandon | reject_plan.
    Backs the failure card buttons and next_action.options."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    # Verify the story belongs to THIS goal — role is only checked on `pid`, and the
    # recovery functions derive goal_id from the story itself, so without this a member
    # of one goal could mutate another goal's DAG by passing a foreign sid (IDOR).
    from app.database import get_pool as _get_pool
    _story_goal = await _get_pool().fetchval("SELECT goal_id FROM stories WHERE id = $1", sid)
    if _story_goal is None or _story_goal != pid:
        raise HTTPException(status_code=404, detail="story not found for goal")

    form = await request.form()
    action = form.get("action", "").strip()
    feedback = form.get("feedback", "")
    force = str(form.get("force", "")).lower() in ("1", "true", "yes", "on")

    from app.services import story_recovery as _sr
    if action == "replan":
        result = await _sr.replan_dag(sid, feedback=feedback, by_uid=user["uid"])
    elif action == "retry":
        result = await _sr.retry_dag(sid, by_uid=user["uid"])
    elif action == "abandon":
        result = await _sr.abandon_dag(sid, by_uid=user["uid"], force=force)
    elif action == "review_keep":
        result = await _sr.review_and_keep_dag(sid, by_uid=user["uid"])
    elif action == "reject_plan":
        result = await _sr.reject_plan(sid, feedback=feedback, by_uid=user["uid"])
    else:
        raise HTTPException(status_code=400,
                            detail="action must be replan|retry|review_keep|abandon|reject_plan")

    # Abandon with uncommitted completed-phase work → not an error; ask the UI to confirm
    # (offer Review & keep vs discard). Return 200 so the client reads the payload cleanly.
    if result.get("needs_confirm"):
        return JSONResponse(result)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    # Mark the recovery card decided so its buttons don't re-appear on reload.
    if result.get("dag_task_id"):
        await _sr.mark_recovery_cards_decided(result["dag_task_id"], action)
    # Wake the controller so re-dispatch (retry) / re-planning is picked up promptly.
    try:
        from app.task_controller import trigger as tc_trigger
        await tc_trigger()
    except Exception:
        pass
    return JSONResponse(result)


@router.post("/goals/{pid}/stories/{sid}/diff/retry")
async def retry_diff_review_endpoint(
    pid: str, sid: str,
    user: dict = Depends(get_current_user),
):
    """(Re)generate the diff-review card for a story parked at awaiting_review.

    begin_diff_review captures the diff via shca.workspace.gitDiff FIRST; if that
    RPC fails (it used to die on the 1 MiB WS frame limit for large diffs) the
    card is never created and the story is stranded with no UI. This re-fires it.
    Guarded + idempotent: a no-op if a diff approval is already pending, and only
    acts when a DAG root task is actually awaiting approval.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    from app.database import get_pool
    pool = get_pool()

    pending = await pool.fetchrow(
        "SELECT id FROM approvals WHERE story_id=$1 AND kind='diff' AND decided_at IS NULL "
        "ORDER BY proposed_at DESC LIMIT 1",
        sid,
    )
    if pending:
        return JSONResponse({"ok": True, "regenerated": False,
                             "note": "diff review already pending",
                             "approval_id": str(pending["id"])})

    dag = await pool.fetchrow(
        "SELECT id FROM tasks WHERE story_id=$1 AND COALESCE(parent_task_id,'')='' "
        "AND state='awaiting_approval' AND status='active' ORDER BY created_at DESC LIMIT 1",
        sid,
    )
    if not dag:
        return JSONResponse({"ok": False, "regenerated": False,
                             "error": "no DAG task is awaiting approval for this story"},
                            status_code=400)

    from app.services.diff_review import begin_diff_review
    result = await begin_diff_review(pid, sid, dag["id"])
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    return JSONResponse({**result, "regenerated": True})


# ─── Phase 4 Stage 5: pause / resume / replan / mark-done ─────────────────


async def _write_task_controller_announcement(
    *, goal_id: str, story_id: str, task_id_tag: str,
    phase: str, sentinel_label: str, body: str,
) -> None:
    """Helper to write a task-controller chat message via the same
    plumbing as phase task progress cards (so SHCA gets the gateway
    forward + the card_data shows up in chat history)."""
    try:
        from app.services.sentinel_parser import write_task_progress_card
        await write_task_progress_card(
            goal_id=goal_id, story_id=story_id,
            task_id=task_id_tag, phase=phase, kind="control_event",
            text=body, sentinel_kind=sentinel_label,
        )
    except Exception:
        _log.exception(
            "task-controller announcement write failed (story=%s tag=%s)",
            story_id, task_id_tag,
        )


@router.post("/goals/{pid}/stories/{sid}/pause")
async def pause_story_endpoint(
    pid: str, sid: str,
    user: dict = Depends(get_current_user),
):
    """Pause phase-task dispatch for this story AND abort any in-flight Claude turn.

    Sets execution_paused (no new dispatches) and, if a phase task is mid-flight, calls
    the gateway's shca.claude.abort to SIGTERM/SIGKILL the claude-cli child so the turn
    actually STOPS and the session is freed — otherwise the still-running turn collides
    with the next dispatch on resume ("Gateway connection lost / session already
    running"). The aborted task is left BLOCKED so the human chooses done/retry.
    No new tasks dispatch until /resume or /replan."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    # Kill switch — while frozen, defer to the control panel rather than mutating
    # this story's state (and aborting a live turn) out from under an active
    # system/goal freeze. Same guard as /resume and /replan below.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(pid)
    if _frozen is not None:
        _log.info("pause_story_endpoint refused — execution paused, goal=%s story=%s uid=%s",
                   pid, sid, user["uid"])
        return JSONResponse(_frozen, status_code=423)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT current_phase, execution_paused FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")

    if row["execution_paused"]:
        return JSONResponse({"ok": True, "already_paused": True})

    # Only meaningful during executing — but allow the flag to be set
    # any time; the dispatcher filter doesn't care about phase.
    if row["current_phase"] != "executing":
        _log.info(
            "pause: setting execution_paused=TRUE for story=%s but "
            "current_phase=%s (no in-flight tasks expected)",
            sid, row["current_phase"],
        )

    # Guard on current_phase = 'executing' so a race with force_kill_task (which resets
    # the story to planning) can't leave a planning+execution_paused=TRUE stray state (L3).
    updated = await pool.execute(
        "UPDATE stories SET execution_paused = TRUE, updated_at = NOW() "
        "WHERE id = $1 AND current_phase = 'executing'",
        sid,
    )
    if updated == "UPDATE 0":
        # Story already left executing (force-kill reset it to planning, or it was never
        # executing). Return without aborting in-flight tasks — there are none.
        _log.info(
            "pause: S%s execution_paused write skipped — current_phase is not 'executing'", sid,
        )
        return JSONResponse({"ok": True, "skipped": True,
                             "reason": "story not in executing phase"})

    # Abort any in-flight Claude turn(s) for this story so pause actually STOPS work and
    # frees the session (the gateway's shca.claude.abort SIGTERM/SIGKILLs the claude-cli
    # child by taskId). The aborted task is left BLOCKED so the human chooses done/retry.
    # Without this, a still-running turn collides with the next dispatch on resume.
    inflight_tasks = await pool.fetch(
        """SELECT id FROM tasks
            WHERE story_id = $1 AND executor = 'claude-code-phase'
              AND state IN ('in_progress', 'dispatching')""",
        sid,
    )
    paused_by = user.get("email") or user["uid"]
    for t in inflight_tasks:
        tid = t["id"]
        try:
            from app.openclaw import gateway_admin
            res = await gateway_admin.call("shca.claude.abort", {"taskId": tid}, timeout=30)
            _log.info("pause: shca.claude.abort task=%s aborted=%s", tid, res.get("aborted"))
        except Exception as exc:
            _log.warning("pause: shca.claude.abort failed for task=%s: %s", tid, exc)
        # Release the aborted turn's credentials (the key was minted but the turn is dead).
        try:
            from app.services.credential_brokers import release_credentials_safe
            await release_credentials_safe(tid, "pause:abort")
        except Exception:
            pass
        # Mark BLOCKED. Guarded to in_progress/dispatching so the dispatch coroutine's
        # own post-invoke handling (which returns on the aborted invoke) can't fight us;
        # its UPDATE is also state-guarded, so whichever lands first, the task ends blocked.
        await pool.execute(
            "UPDATE tasks SET state = 'blocked', blocked_reason = $2, updated_at = NOW() "
            "WHERE id = $1 AND state IN ('in_progress', 'dispatching')",
            tid,
            f"Paused mid-turn by {paused_by} — Claude turn aborted. "
            "Mark done or retry from the task board.",
        )
    inflight = len(inflight_tasks)
    if inflight > 0:
        body = (
            "⏸ Execution paused. The in-flight Claude turn was aborted and that task is "
            "now blocked — mark it done or retry it from the task board. No new tasks "
            "will dispatch. SHCA: ask the human what they'd like to change before resuming."
        )
    else:
        body = (
            "⏸ Execution paused at human request. No tasks are in-flight "
            "and no new tasks will dispatch. SHCA: ask the human what "
            "they'd like to change before resuming or replanning."
        )

    await _write_task_controller_announcement(
        goal_id=pid, story_id=sid,
        task_id_tag=f"{sid}-pause",
        phase="executing",
        sentinel_label="EXECUTION_PAUSED",
        body=body,
    )

    return JSONResponse({"ok": True})


@router.post("/goals/{pid}/stories/{sid}/resume")
async def resume_story_endpoint(
    pid: str, sid: str,
    user: dict = Depends(get_current_user),
):
    """Clear the execution_paused flag. Dispatcher resumes picking up
    ready tasks on the next tick. Used when the human just wants to
    continue without replanning."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    # Kill switch — refuse to clear execution_paused while the goal or system is
    # frozen: real dispatch is still blocked downstream (dispatch_block fails
    # closed), so clearing the flag here would just leave the story falsely
    # showing "executing" instead of honestly reflecting that nothing can run.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(pid)
    if _frozen is not None:
        _log.info("resume_story_endpoint refused — execution paused, goal=%s story=%s uid=%s",
                   pid, sid, user["uid"])
        return JSONResponse(_frozen, status_code=423)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT execution_paused FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")
    if not row["execution_paused"]:
        return JSONResponse({"ok": True, "already_running": True})

    await pool.execute(
        "UPDATE stories SET execution_paused = FALSE, updated_at = NOW() WHERE id = $1",
        sid,
    )

    await _write_task_controller_announcement(
        goal_id=pid, story_id=sid,
        task_id_tag=f"{sid}-resume",
        phase="executing",
        sentinel_label="EXECUTION_RESUMED",
        body=(
            "▶ Execution resumed at human request. The next ready task "
            "will dispatch on the next controller tick."
        ),
    )

    # Trigger the controller so the next task picks up immediately (trigger is async — await it).
    try:
        from app.task_controller import trigger as tc_trigger
        await tc_trigger()
    except Exception:
        pass

    return JSONResponse({"ok": True})


@router.post("/goals/{pid}/stories/{sid}/replan")
async def replan_story_endpoint(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Start a replan flow. Cancels pending phase tasks (state in
    pending/ready), keeps in-flight ones running (they'll finish but
    the dispatcher won't pick up next ones because we also flip phase
    back to planning). Clears execution_paused. SHCA + Claude will
    iterate on a new plan; human approves → new DAG ingests on top."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    # Kill switch — same guard as /pause and /resume above.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(pid)
    if _frozen is not None:
        _log.info("replan_story_endpoint refused — execution paused, goal=%s story=%s uid=%s",
                   pid, sid, user["uid"])
        return JSONResponse(_frozen, status_code=423)

    form = await request.form()
    feedback = (form.get("feedback") or "").strip()

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT current_phase FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")

    # Cancel pending tasks for this story (keep in-flight as-is — they
    # finish naturally; their sentinel just won't trigger next dispatch).
    cancelled_count = await pool.fetchval(
        """UPDATE tasks
              SET state = 'cancelled', updated_at = NOW()
            WHERE story_id = $1
              AND executor = 'claude-code-phase'
              AND state IN ('pending', 'ready')
            RETURNING (SELECT COUNT(*) FROM tasks WHERE story_id = $1
                       AND executor = 'claude-code-phase'
                       AND state = 'cancelled')""",
        sid,
    )

    # Move story back to planning, clear paused flag.
    await pool.execute(
        """UPDATE stories
              SET current_phase = 'planning',
                  current_plan_path = NULL,
                  execution_paused = FALSE,
                  updated_at = NOW()
            WHERE id = $1""",
        sid,
    )

    body_lines = [
        f"♻ Replan requested at human request. Pending tasks for this "
        f"story have been cancelled (count: {cancelled_count or 0}).",
        f"Phase transitioned: {row['current_phase']} → planning.",
    ]
    if feedback:
        body_lines.append(f"\nHuman feedback: {feedback}")
    body_lines.append(
        "\nSHCA: discuss the new direction with Claude (existing session "
        "still alive — resume via sessions_spawn). When a revised plan is "
        "ready, surface it to the human for approval."
    )

    await _write_task_controller_announcement(
        goal_id=pid, story_id=sid,
        task_id_tag=f"{sid}-replan",
        phase="planning",
        sentinel_label="REPLAN_REQUESTED",
        body="\n".join(body_lines),
    )

    return JSONResponse({"ok": True, "cancelled_pending": cancelled_count or 0})


@router.post("/goals/{pid}/stories/{sid}/mark-done")
async def mark_story_done_endpoint(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Explicit human action to close the story. Should only be invoked
    from awaiting_next phase (after at least one DAG has shipped) or
    after the human decides to abandon planning. SHCA does NOT auto-call
    this — only the human button or an explicit human instruction."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT state, current_phase, title, shca_instance_id FROM stories "
        "WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")
    if row["state"] == "done":
        return JSONResponse({"ok": True, "already_done": True})

    # Warn if closing the story would discard uncommitted work (phases don't commit —
    # completed-phase changes only reach git via the diff-review gate). Best-effort +
    # skippable via force so a diff-check hiccup never blocks a close.
    try:
        _form = await request.form()
        _force = str(_form.get("force", "")).lower() in ("1", "true", "yes", "on")
    except Exception:
        _force = False
    if not _force:
        from app.services.story_recovery import workspace_has_changes  # noqa: PLC0415
        has_changes, stat = await workspace_has_changes(
            row["shca_instance_id"] or "", sid)
        if has_changes:
            return JSONResponse({
                "ok": False, "needs_confirm": True, "action": "mark_done",
                "story_id": sid, "stat": stat,
                "reason": (
                    f"There is uncommitted work in the workspace that will be DISCARDED "
                    f"when the story closes{(': ' + stat) if stat else ''}. If you have an "
                    f"open phase, use 'Review & keep' to push it first. Mark done anyway?"
                ),
            })

    old_state = row["state"]
    # Wrap UPDATE + ledger emit in one transaction so audit trail commits
    # together with the state mutation. The ledger event also drives the
    # memory_v2 event bridge + story-complete flush trigger.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE stories
                  SET state = 'done',
                      current_phase = 'done',
                      execution_paused = FALSE,
                      completed_at = NOW(),
                      -- Clear active_thread_id on terminal transition so the
                      -- SHCA cleanup watchdog can see the story is finished
                      -- when joining stories→agent_instances by thread_id.
                      -- Without this, the next story sharing this thread would
                      -- pick up an ambiguous join match.
                      active_thread_id = NULL,
                      updated_at = NOW()
                WHERE id = $1""",
            sid,
        )
        from app.ledger.journal import append_event
        from app.ledger.schema import STORY_STATE_CHANGED
        from app.ledger.actors import human_actor
        # Marking a story done is a human action (system if uid unknown).
        _md_actor, _md_type = await human_actor(user["uid"])
        await append_event(
            conn,
            actor_id=_md_actor,
            actor_type=_md_type,
            event_type=STORY_STATE_CHANGED,
            payload={
                "story_id": sid,
                "title": row["title"],
                "from_state": old_state,
                "to_state": "done",
                "action": "mark-done",
                "actor_uid": user["uid"],
            },
            context_goal_id=pid,
        )

    await _write_task_controller_announcement(
        goal_id=pid, story_id=sid,
        task_id_tag=f"{sid}-done",
        phase="done",
        sentinel_label="STORY_DONE",
        body=(
            "✓ Story marked done at human request. SHCA: write a brief "
            "completion summary per assistant_completion skill."
        ),
    )

    return JSONResponse({"ok": True})
