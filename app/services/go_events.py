"""GO Event Router — system events trigger contextual messages to the GO agent.

When significant things happen in a goal (task completed, member joined, resource changed),
this module sends a system message to the GO agent so it can react appropriately.
"""
import json
import logging

from app.database import get_pool
from app.openclaw import session_manager, make_goal_session_key
from app.services.go_agent import build_go_context
from app.services.story_card_thread import newest_human_thread

_log = logging.getLogger(__name__)


async def _send_system_message_to_go(
    goal_id: str, message: str, wake_up: bool = False, mirror_to_mesh: bool = True,
):
    """Queue or push a system message for the GO.

    Two modes:

    - wake_up=False (default, transcript-only)
      Appends the message to the GO's session transcript via chat.inject
      WITHOUT running inference. The GO sees it on its next real turn
      (when a human messages it, or the SHCA pings via message_go).
      Cheap — no Gemini tokens. Right for routine notifications: task
      dispatched, resource registered, story moved between non-terminal
      states, etc.

    - wake_up=True (active, GO responds)
      Sends the message via chat.send so the GO runs an inference turn
      and emits a response, which is relayed to the goal Slack channel
      and the control-panel goal thread via the GO's on_message
      callback. Right for milestone transitions where humans expect to
      see an update (approval, completion, failure, rejection, blocked).

    mirror_to_mesh (default True): also cross-post `message` to Agent Mesh
    (orchestrator_thread_id) so humans see it without opening the goal
    thread. Set to False when the caller already delivers its own
    human-facing copy elsewhere (e.g. council/security review dispatchers
    call persist_review_to_orchestrator — a formatted review card in the
    council's OWN thread — before waking the GO here) — otherwise this
    raw wake-up text duplicates that card straight into Agent Mesh.

    Delivery guarantees:
      - If the gateway session exists, both modes succeed regardless of
        whether the Python-side _AgentSession is currently in memory.
      - If the gateway session doesn't exist (goal just created, GO
        never chatted with), we log a warning and drop — no tokens
        burned to warm an empty session for a notification. The first
        human message to the GO will kick it off normally; the full
        dynamic context build picks up anything we couldn't inject.
    """
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT go_agent_id, go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not goal or not goal["go_agent_id"]:
        _log.debug("No GO agent for goal %s — skipping event", goal_id)
        return

    go_agent_id = goal["go_agent_id"]
    go_thread_id = goal["go_thread_id"] or ""

    # Cross-post to Agent Mesh so the team sees task/story/member transitions
    # without opening the goal thread — which only renders the GO's own
    # conversational turns. Independent of the go_thread_id delivery below
    # (best-effort: logs and continues if the goal has no orchestrator_thread_id).
    # Skipped when the caller already has its own human-facing delivery —
    # see mirror_to_mesh in the docstring.
    if mirror_to_mesh:
        from app.services.orchestrator_delivery import persist_system_notice_to_orchestrator
        await persist_system_notice_to_orchestrator(goal_id, message)

    # The control-panel agent_id is often cased differently from the
    # gateway's stored name (goals with ID "BMIWA" have agent_id
    # "go-BMIWA" but the gateway stored "go-bmiwa" because agents.create
    # lowercases). We pull the gateway's actual id from the agents table
    # and use it as the session-key prefix — otherwise chat.inject hits
    # "session not found".
    gateway_agent_id = await pool.fetchval(
        "SELECT gateway_agent_id FROM agents WHERE id = $1", go_agent_id,
    ) or go_agent_id

    # Build the session key the same way send_to_gateway does
    # (agent:{gw_agent_id}:{pid}-{thread_id}). pid is lowercased at this
    # boundary — the gateway canonicalises sessionKeys to lowercase.
    session_key = make_goal_session_key(gateway_agent_id, goal_id, go_thread_id)

    if wake_up:
        # Drive the GO through its existing _AgentSession so the
        # response flows back through on_message → Slack relay + UI.
        # Falls back to a one-shot chat.inject if the session isn't
        # warm — at least the message is recorded.
        try:
            from app.services.agent_driver import ensure_go_session
            session = await ensure_go_session(goal_id)
            if session is not None:
                # send_to_gateway expects JSON envelope. queue_kind="system_wake"
                # is required here — the default ("user") persists this as a
                # normal kind='user' row, which slips past the goal-thread
                # render filter (goal_thread.py excludes kind='system_wake',
                # not sender='system') and shows up as a visible bubble.
                import json as _json
                await session.send_to_gateway(_json.dumps({
                    "type": "message",
                    "content": message,
                    "thread_id": go_thread_id,
                }), queue_kind="system_wake")
                _log.info(
                    "Sent system event (wake_up) to GO for goal %s: %s",
                    goal_id, message[:100],
                )
                return
            # No session and we couldn't create one — fall through to inject.
            _log.warning(
                "wake_up requested but ensure_go_session returned None "
                "for goal %s — falling back to inject",
                goal_id,
            )
        except Exception as exc:
            _log.warning(
                "wake_up send to GO failed for goal %s: %s — "
                "falling back to inject",
                goal_id, exc,
            )

    # Route through the durable queue as a SYSTEM_INJECT so it serializes with
    # turns under the per-thread advisory lock and rides the next turn — instead
    # of a raw chat.inject that can land mid-turn and be clobbered by an in-turn
    # compaction. Falls back to chat.inject when the queue is off / enqueue fails.
    try:
        from app.chat_module.system_ingress import enqueue_system_inject
        msg_id = await enqueue_system_inject(
            thread_id=go_thread_id, goal_id=goal_id, agent_id=go_agent_id,
            gateway_agent_id=gateway_agent_id, agent_type="GO", content=message,
        )
        if msg_id is not None:
            _log.info("Queued system inject for GO goal %s: %s", goal_id, message[:100])
            return
    except Exception as exc:
        _log.warning(
            "GO system inject enqueue failed for goal %s: %s — falling back to chat.inject",
            goal_id, exc,
        )

    from app.openclaw import gateway_admin
    try:
        await gateway_admin.chat_inject(
            session_key=session_key, message=message, label="system",
        )
        _log.info(
            "Injected system event into GO transcript for goal %s: %s",
            goal_id, message[:100],
        )
    except Exception as exc:
        # Every delivery path (wake_up send, queue enqueue, this legacy
        # chat.inject) has now failed. Common cause: "session not found" —
        # the GO's gateway session hasn't been warmed yet — in which case the
        # GO still picks up durable state (stories, tasks, resources) from
        # build_go_context on its next real turn, so this specific event
        # notice is what's lost, not the GO's overall awareness. Logged at
        # error (not warning, like the per-path fallbacks above) so a total
        # delivery failure is grep-able and doesn't blend into routine
        # single-path fallbacks.
        _log.error(
            "GO system event delivery FAILED via ALL paths (wake_up/enqueue/"
            "chat.inject) for goal %s — event notice lost: %s | last error: %s",
            goal_id, message[:200], exc,
        )


async def _send_system_message_to_shca(
    goal_id: str, source_thread_id: str, message: str, wake_up: bool = False,
):
    """Queue or push a system message for the SHCA that owns
    `source_thread_id`. Used when a task the SHCA dispatched changes
    state (approved / rejected / completed / failed / blocked) — the
    SHCA needs to know so it can keep the user informed in their chat
    thread.

    `source_thread_id` is taken from `tasks.source_thread_id`, which
    points at the SHCA's chat thread where the task was created via
    create_task. If the task has no source_thread_id (rare —
    manually-created task), this no-ops.

    Same wake_up semantics as _send_system_message_to_go:
      - False → chat.inject (transcript-only, no inference)
      - True  → chat.send (active; SHCA replies in its chat thread,
                which propagates to the user via the goal_thread/UI
                relay).
    """
    pool = get_pool()
    if not source_thread_id:
        return

    # Find the SHCA agent that owns this thread via the thread's creator.
    shca = await pool.fetchrow(
        """SELECT a.id AS agent_id
             FROM threads t
             JOIN users u ON u.uid = t.created_by
             JOIN agents a ON a.owner_actor_id = u.actor_id
            WHERE t.id = $1
              AND a.type = 'SHCA'
              AND a.status = 'active'
            ORDER BY a.created_at
            LIMIT 1""",
        source_thread_id,
    )
    if not shca:
        _log.debug(
            "No SHCA owns thread %s — skipping SHCA notification",
            source_thread_id[:12] if source_thread_id else "-",
        )
        return

    template_agent_id = shca["agent_id"]

    # Locate the per-conversation gateway instance for this thread.
    instance = await pool.fetchrow(
        """SELECT gateway_agent_id FROM agent_instances
            WHERE template_id = $1 AND thread_id = $2 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1""",
        template_agent_id, source_thread_id,
    )
    if not instance:
        _log.debug(
            "No active SHCA gateway instance for thread %s — skipping",
            source_thread_id[:12] if source_thread_id else "-",
        )
        return

    gateway_agent_id = instance["gateway_agent_id"]
    session_key = make_goal_session_key(gateway_agent_id, goal_id, source_thread_id)

    if wake_up:
        try:
            from app.services.agent_driver import ensure_shca_session
            session = await ensure_shca_session(
                goal_id, template_agent_id, source_thread_id,
            )
            if session is not None:
                import json as _json
                await session.send_to_gateway(_json.dumps({
                    "type": "message",
                    "content": message,
                    "thread_id": source_thread_id,
                }))
                _log.info(
                    "Sent system event (wake_up) to SHCA for goal=%s thread=%s: %s",
                    goal_id, source_thread_id[:12], message[:100],
                )
                return
            _log.warning(
                "wake_up requested but ensure_shca_session returned None "
                "for goal=%s thread=%s — falling back to inject",
                goal_id, source_thread_id[:12] if source_thread_id else "-",
            )
        except Exception as exc:
            _log.warning(
                "wake_up send to SHCA failed for goal=%s thread=%s: %s — "
                "falling back to inject",
                goal_id,
                source_thread_id[:12] if source_thread_id else "-",
                exc,
            )

    # Route through the durable queue as a SYSTEM_INJECT (serialized under the
    # per-thread advisory lock, rides the next turn) instead of a raw chat.inject
    # that can land mid-turn / be clobbered by compaction. Falls back to
    # chat.inject when the queue is off / enqueue fails.
    try:
        from app.chat_module.system_ingress import enqueue_system_inject
        msg_id = await enqueue_system_inject(
            thread_id=source_thread_id, goal_id=goal_id, agent_id=template_agent_id,
            gateway_agent_id=gateway_agent_id, agent_type="SHCA", content=message,
        )
        if msg_id is not None:
            _log.info(
                "Queued system inject for SHCA goal=%s thread=%s: %s",
                goal_id, source_thread_id[:12], message[:100],
            )
            return
    except Exception as exc:
        _log.warning(
            "SHCA system inject enqueue failed for goal=%s thread=%s: %s — "
            "falling back to chat.inject",
            goal_id, source_thread_id[:12] if source_thread_id else "-", exc,
        )

    from app.openclaw import gateway_admin
    try:
        await gateway_admin.chat_inject(
            session_key=session_key, message=message, label="system",
        )
        _log.info(
            "Injected system event into SHCA transcript: goal=%s thread=%s: %s",
            goal_id, source_thread_id[:12], message[:100],
        )
    except Exception as exc:
        # Every delivery path (wake_up send, queue enqueue, this legacy
        # chat.inject) has now failed for this SHCA notice. Error (not
        # warning, like the per-path fallbacks above) so a total delivery
        # failure is grep-able and doesn't blend into routine single-path
        # fallbacks.
        _log.error(
            "SHCA system event delivery FAILED via ALL paths (wake_up/enqueue/"
            "chat.inject) for goal=%s thread=%s — event notice lost: %s",
            goal_id,
            source_thread_id[:12] if source_thread_id else "-",
            exc,
        )


# ── Helpers ─────────────────────────────────────────────────────────────────

async def recalculate_goal_completion(goal_id: str, pool=None) -> int:
    """Recalculate and persist goal completion_pct from root task states.

    Counts root tasks only (no parent, no review_target) so subtask churn
    doesn't distort the number. Terminal done states are 'done' and 'approved';
    'failed' and 'cancelled' are excluded from the numerator so they don't
    artificially inflate progress. Returns the new pct written to the DB.
    """
    if pool is None:
        pool = get_pool()

    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE state IN ('done', 'approved')
            ) AS done_count,
            COUNT(*) AS total_count
        FROM tasks
        WHERE goal_id        = $1
          AND status         = 'active'
          AND parent_task_id IS NULL
          AND review_target_id IS NULL
        """,
        goal_id,
    )

    total = row["total_count"] if row else 0
    done = row["done_count"] if row else 0
    pct = round(done / total * 100) if total > 0 else 0

    await pool.execute(
        "UPDATE goals SET completion_pct = $1 WHERE id = $2",
        pct, goal_id,
    )
    _log.info(
        "Goal %s completion recalculated: %d/%d tasks done → %d%%",
        goal_id, done, total, pct,
    )
    return pct


# ── Event handlers ──────────────────────────────────────────────────────────

async def on_task_dispatched(goal_id: str, task_id: str) -> None:
    """Task moved ready → dispatching / in_progress. Tell GO what's being
    worked on so it can factor it into planning / status reports."""
    pool = get_pool()
    task = await pool.fetchrow(
        """SELECT t.title, t.description, t.template_id, t.assigned_to,
                  t.parent_task_id, t.story_id, t.source_thread_id,
                  s.title AS story_title
             FROM tasks t
             LEFT JOIN stories s ON s.id = t.story_id
            WHERE t.id = $1""",
        task_id,
    )
    if not task:
        return

    title = task["title"] or task_id
    description = (task.get("description") or "").strip()
    template_id = task.get("template_id") or "agent"
    story_title = task.get("story_title") or ""
    parent = task.get("parent_task_id")

    parts = [
        f"[System] Task dispatched: {task_id[:8]} — \"{title}\"",
        f"Agent: `{template_id}`",
    ]
    if story_title:
        parts.append(f"Story: \"{story_title}\"")
    if parent:
        parts.append(f"Parent task: {parent[:8]}")
    if description:
        parts.append(f"\nDescription:\n{description[:1500]}")
    parts.append(
        "\nNo action needed — this is informational. You'll be pinged "
        "again when the task completes, is blocked, or needs human input."
    )
    await _send_system_message_to_go(goal_id, "\n".join(parts))


async def on_task_feedback(
    goal_id: str, task_id: str, feedback: str, actor_uid: str,
    phase: str = "review",
) -> None:
    """Human gave feedback on a task (typically via Request Changes on an
    approval / review card). GO needs to know so it can update the plan,
    the story's acceptance criteria, or coordinate with the assistant."""
    if not feedback or not feedback.strip():
        return

    pool = get_pool()
    task = await pool.fetchrow(
        """SELECT t.title, t.template_id, t.story_id, s.title AS story_title
             FROM tasks t
             LEFT JOIN stories s ON s.id = t.story_id
            WHERE t.id = $1""",
        task_id,
    )
    title = (task or {}).get("title") or task_id
    story_title = (task or {}).get("story_title") or ""

    actor_name = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", actor_uid,
    ) or actor_uid

    parts = [
        f"[System] Human feedback on task {task_id[:8]} — \"{title}\"",
        f"From: {actor_name}",
        f"Phase: {phase}",
    ]
    if story_title:
        parts.append(f"Story: \"{story_title}\"")
    parts.append(f"\nFeedback:\n{feedback[:2000]}")
    parts.append(
        "\nEvaluate whether this changes the plan, the story's acceptance "
        "criteria, or the assignee's next steps. Respond via the goal "
        "thread and/or `reply_assistant` if the assistant needs to "
        "adjust course. Use `update_story` to record any scope change."
    )
    await _send_system_message_to_go(goal_id, "\n".join(parts))


async def on_task_state_changed(
    goal_id: str,
    task_id: str,
    from_state: str,
    to_state: str,
    actor_uid: str,
):
    """Task/story state changed — notify GO so it can react appropriately."""
    pool = get_pool()

    # Check stories table first, then tasks
    story = await pool.fetchrow(
        "SELECT title, assigned_to, result_summary, depends_on, "
        "acceptance_criteria, blocked_reason, progress_summary FROM stories WHERE id = $1",
        task_id,
    )
    is_story = story is not None
    is_subtask = False
    if is_story:
        task = story
        source_thread_id = ""  # Stories don't have a source thread; they're created by GO directly.
    else:
        task = await pool.fetchrow(
            "SELECT title, assigned_to, result_summary, depends_on, "
            "acceptance_criteria, blocked_reason, source_thread_id, "
            "parent_task_id FROM tasks WHERE id = $1",
            task_id,
        )
        source_thread_id = (task and task.get("source_thread_id")) or ""
        # Subtasks (planner / dev / test / review) belong to a root task
        # chain. Their state changes shouldn't wake the GO + dispatching
        # SHCA on every transition — that would be a chat per child task,
        # multiplied by every state change. Only the ROOT task firing 'done'
        # (auto-completed by check_parent_completion in task_controller.py)
        # should wake them.
        is_subtask = bool(task and (task.get("parent_task_id") or "").strip())
    if not task:
        return
    actor_name = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", actor_uid,
    ) or actor_uid

    title = task["title"]
    assignee = task["assigned_to"] or "unassigned"

    # Transitions where the GO should ACTIVELY respond (post a brief
    # update in the goal channel, not just record in transcript).
    # Routine state churn (e.g. pending → in_progress) stays passive.
    # Subtasks NEVER wake — their parent firing 'done' is the single
    # ack point; chat-per-subtask is just noise.
    wake_up_states = {"approved", "rejected", "done", "failed", "blocked"}
    wake_up = (to_state in wake_up_states) and not is_subtask

    if is_story:
        msg = await _build_story_event_message(
            pool, goal_id, task_id, task, from_state, to_state, actor_name)
    elif to_state in ("done", "approved"):
        # Pull outputs (artifacts) from the latest completion card we
        # stored for this task — the completion_card was posted to the
        # SHCA's source thread via messages.card_data on task report.
        artifacts_lines: list[str] = []
        try:
            import json as _json
            card_row = await pool.fetchrow("""
                SELECT card_data FROM messages
                 WHERE card_data IS NOT NULL
                   AND card_data->>'task_id' = $1
                   AND card_data->>'card_type' = 'completion'
                 ORDER BY created_at DESC LIMIT 1
            """, task_id)
            if card_row and card_row["card_data"]:
                card = card_row["card_data"]
                if isinstance(card, str):
                    card = _json.loads(card)
                for art in (card.get("artifacts") or [])[:20]:
                    kind = art.get("type") or art.get("kind") or "artifact"
                    ref = (
                        art.get("ref") or art.get("path")
                        or art.get("url") or art.get("uri") or ""
                    )
                    if ref:
                        artifacts_lines.append(f"  - {kind}: {ref}")
        except Exception as exc:
            _log.debug("Artifact lookup failed for task %s: %s", task_id, exc)

        # Collect human feedback given during review. Two sources:
        #   1. tasks.blocked_reason set to "HUMAN FEEDBACK: …" by
        #      card_responses.py in the Request Changes flow.
        #   2. Ledger QUESTION_ANSWERED payloads scoped to this task.
        feedback_lines: list[str] = []
        blocked_reason = (task.get("blocked_reason") or "").strip()
        if blocked_reason.startswith("HUMAN FEEDBACK:"):
            feedback_lines.append(blocked_reason[len("HUMAN FEEDBACK:"):].strip())
        try:
            ans_rows = await pool.fetch("""
                SELECT payload FROM ledger_events
                 WHERE context_task_id = $1
                   AND event_type = 'question.answered'
                 ORDER BY timestamp DESC
                 LIMIT 5
            """, task_id)
            for r in ans_rows:
                p = r["payload"]
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except Exception:
                        continue
                answer = (p or {}).get("answer") or (p or {}).get("value") or ""
                if answer and answer.strip():
                    feedback_lines.append(answer.strip())
        except Exception as exc:
            _log.debug("Feedback lookup failed for task %s: %s", task_id, exc)

        # Dedupe while preserving order
        seen: set[str] = set()
        feedback_lines = [
            f for f in feedback_lines
            if not (f in seen or seen.add(f))
        ]

        # Unblocking info (what downstream can now run)
        blocked = await pool.fetch("""
            SELECT id, title, assigned_to FROM tasks
            WHERE goal_id = $1 AND $2 = ANY(depends_on)
              AND status = 'active' AND state = 'pending'
        """, goal_id, task_id)

        result = task["result_summary"] or "No result summary provided"

        verb = "approved" if to_state == "approved" else "completed"
        parts = [
            f"[System] Task {verb}: {task_id} — \"{title}\"",
            f"{'Approved' if to_state == 'approved' else 'Completed'} by: {actor_name}",
            f"",
            f"What was done:",
            f"{result}",
        ]
        if artifacts_lines:
            parts.append("")
            parts.append("Outputs produced:")
            parts.extend(artifacts_lines)
        else:
            parts.append("")
            parts.append("Outputs produced: (none recorded on the completion card)")

        if feedback_lines:
            parts.append("")
            parts.append("Human feedback received during review:")
            for f in feedback_lines[:5]:
                parts.append(f"  - {f[:1000]}")

        if blocked:
            names = ", ".join(f"{t['id'][:8]} ({t['title']})" for t in blocked)
            parts.append("")
            parts.append(f"This may unblock: {names}")

        parts.append("")
        if to_state == "approved":
            parts.append(
                "Post a brief update in the goal channel: confirm the approval, "
                "name what's now unblocked (or note that nothing is). "
                "**DO NOT auto-start the next story or pre-create tasks for it.** "
                "The user will type `work on <story>` when they're ready. "
                "Don't repeat the full result; the human just read it."
            )
        else:
            parts.append(
                "Post a brief update in the goal channel: confirm completion, "
                "list outputs in one line each, and call out anything that's now "
                "unblocked. **DO NOT start the next story / dispatch its planner-ia.** "
                "If the team wants to move on, they'll type `work on <story>` in "
                "the goal channel. Verify dependent tasks pick up correctly. "
                "Update the plan if anything changed."
            )
        msg = "\n".join(parts)
    elif to_state == "failed":
        msg = (
            f"[System] Task failed: {task_id} — \"{title}\"\n"
            f"Reported by: {actor_name}\n\n"
            f"Post a brief update in the goal channel: name the failure, "
            f"propose a recovery path (retry, reassign, drop), and ask the "
            f"team for direction if the choice isn't obvious. "
            f"**Do not start unrelated work** while this is unresolved."
        )
    elif to_state == "rejected":
        msg = (
            f"[System] Task rejected: {task_id} — \"{title}\"\n"
            f"Rejected by: {actor_name}\n\n"
            f"Post a brief update in the goal channel acknowledging the "
            f"rejection. If the team needs a revised approach, propose one "
            f"and **wait for approval** before any new tasks fire. "
            f"Don't pre-emptively start a different story."
        )
    elif to_state == "blocked":
        reason = task.get("blocked_reason") or "No reason given"
        msg = (
            f"[System] Task blocked: {task_id} — \"{title}\"\n"
            f"Assigned to: {assignee}\nReason: {reason}\n\n"
            f"Post a brief update in the goal channel naming the blocker. "
            f"If it's something you can resolve (re-allocate a resource, "
            f"answer a planning question), do it. Otherwise call out who "
            f"needs to act. **Don't pivot to a different story** while this "
            f"one is blocked unless the team explicitly asks you to."
        )
    else:
        msg = (
            f"[System] Task {task_id} — \"{title}\" moved from {from_state} to {to_state} "
            f"by {actor_name}."
        )

    await _send_system_message_to_go(goal_id, msg, wake_up=wake_up)

    # Also notify the SHCA that originally dispatched this task — they
    # need to keep the user informed in their chat thread. Stories
    # don't have a source thread (created by GO directly), so this
    # only applies to tasks.
    if source_thread_id:
        await _send_system_message_to_shca(
            goal_id, source_thread_id, msg, wake_up=wake_up,
        )

    # Recalculate goal completion whenever a root task reaches a terminal
    # state so the dashboard always reflects current progress without
    # requiring the GO agent to post an explicit update.
    if to_state in {"done", "approved", "failed", "cancelled"} and not is_story:
        try:
            await recalculate_goal_completion(goal_id)
        except Exception as exc:
            _log.warning(
                "Failed to recalculate completion for goal %s after task %s → %s: %s",
                goal_id, task_id, to_state, exc,
            )


async def _build_story_event_message(
    pool, goal_id: str, story_id: str, story, from_state: str, to_state: str, actor_name: str,
) -> str:
    """Build rich event message for story state changes."""
    title = story["title"]

    if to_state == "done":
        # Rich completion review — the key event for plan evolution
        acceptance = story.get("acceptance_criteria") or "None specified"
        result = story.get("result_summary") or "No result summary"

        # ── TWO LOOKUPS THAT WERE DEAD FOR EVERY LOCAL STORY ────────
        #
        # `threads WHERE task_id = <story id>` found nothing: a story thread
        # is keyed by `story_id`, and the local lane never wrote `task_id` at
        # all. `role = 'assistant'` found nothing either: local turns are
        # recorded as `'agent'`, the vocabulary `/report-message` has always
        # used.
        #
        # So the work-report section was empty for every locally-run story, on
        # BOTH counts, and the GO planned the next story from the board's
        # titles and states. Neither lookup ever errored.
        #
        # `stories.active_thread_id` first, because that is the pointer every
        # lane maintains; the `task_id` join stays as the fallback for rows
        # that predate story threads.
        thread = await pool.fetchrow(
            "SELECT COALESCE("
            "  (SELECT active_thread_id FROM stories WHERE id = $1),"
            "  " + newest_human_thread("$1") + ","
            "  (SELECT id FROM threads WHERE task_id = $1 LIMIT 1)"
            ") AS id", story_id,
        )
        work_report = ""
        if thread and thread["id"]:
            # BOTH ROLES. The gateway writes `assistant`, the local lane writes
            # `agent`, and until the two vocabularies are reconciled a reader
            # has to know both — rewriting history is not worth a migration.
            report_msgs = await pool.fetch("""
                SELECT content FROM messages
                WHERE thread_id = $1 AND role IN ('assistant', 'agent')
                ORDER BY created_at DESC LIMIT 5
            """, thread["id"])
            if report_msgs:
                reports = "\n".join(m["content"][:300] for m in reversed(report_msgs))
                work_report = f"\nWork report from assistant:\n{reports}"

        # Child tasks summary
        child_tasks = await pool.fetch("""
            SELECT title, state, result_summary FROM tasks
            WHERE story_id = $1 AND status = 'active'
            ORDER BY created_at
        """, story_id)
        task_summary = ""
        if child_tasks:
            lines = []
            for t in child_tasks:
                r = f" — {t['result_summary'][:100]}" if t.get("result_summary") else ""
                lines.append(f"  - {t['title']} [{t['state']}]{r}")
            task_summary = f"\nTasks completed under this story:\n" + "\n".join(lines)

        # Remaining stories
        remaining = await pool.fetch("""
            SELECT id, title, state, assigned_to, depends_on FROM stories
            WHERE goal_id = $1
            AND state NOT IN ('done', 'cancelled', 'rejected') AND id != $2
            ORDER BY created_at
        """, goal_id, story_id)
        remaining_info = ""
        if remaining:
            lines = [f"  - {s['id']} [{s['state']}] {s['title']} (assigned: {s['assigned_to'] or 'unassigned'})"
                     for s in remaining]
            remaining_info = f"\nRemaining stories:\n" + "\n".join(lines)

        # Stories unblocked by this completion
        unblocked = await pool.fetch("""
            SELECT id, title, assigned_to FROM stories
            WHERE goal_id = $1 AND $2 = ANY(depends_on)
            AND state IN ('proposed', 'accepted')
        """, goal_id, story_id)
        unblocked_info = ""
        if unblocked:
            names = ", ".join(f"{s['title']} ({s['id']})" for s in unblocked)
            unblocked_info = f"\nNow unblocked: {names}"

        progress = story.get("progress_summary", "") if hasattr(story, "get") else ""
        progress_section = f"\nProgress summary:\n{progress}" if progress else ""

        return (
            f"[System] Story completed: \"{title}\" (id: {story_id})\n"
            f"Completed by: {actor_name}\n\n"
            f"Acceptance criteria: {acceptance}\n"
            f"Result: {result}\n"
            f"{progress_section}"
            f"{work_report}"
            f"{task_summary}"
            f"{remaining_info}"
            f"{unblocked_info}\n\n"
            f"Post a brief acknowledgement in the goal channel: confirm the "
            f"story is done, list what's now unblocked. "
            f"**DO NOT auto-start the next story or pre-create its tasks.** "
            f"The user will type `work on <story>` in the goal channel to "
            f"pick the next one — that's what triggers the new SHCA channel "
            f"and assistant. Until they do, the queue stays paused. "
            f"If the result revealed something that changes the remaining "
            f"plan, propose the change and ask for approval — don't fire it."
        )

    elif to_state == "accepted":
        return (
            f"[System] Story accepted: \"{title}\" (id: {story_id})\n"
            f"Accepted by: {actor_name}\n\n"
            f"They're ready to start when dependencies are met. "
            f"**Do not start it automatically** — the user will type "
            f"`work on <title>` in the goal channel when they're ready."
        )

    elif to_state == "rejected":
        return (
            f"[System] Story rejected: \"{title}\" (id: {story_id})\n"
            f"Rejected by: {actor_name}\n\n"
            f"Acknowledge in the goal channel and ask the team why — should "
            f"it be revised or dropped? **Do not pivot to a different story** "
            f"unless explicitly asked."
        )

    elif to_state == "blocked":
        reason = story.get("blocked_reason") or "No reason given"
        return (
            f"[System] Story blocked: \"{title}\" (id: {story_id})\n"
            f"Blocked by: {actor_name}\n"
            f"Reason: {reason}\n\n"
            f"Help resolve this. Is this a cross-story dependency? "
            f"Can another story's output help?"
        )

    elif to_state == "in_progress":
        return (
            f"[System] Story started: \"{title}\" (id: {story_id})\n"
            f"Started by: {actor_name}. They'll be working with their assistant."
        )

    else:
        return (
            f"[System] Story \"{title}\" (id: {story_id}) moved from "
            f"{from_state} to {to_state} by {actor_name}."
        )


async def on_member_added(
    goal_id: str,
    uid: str,
    role: str,
    added_by: str,
):
    """New member joined — GO should welcome them and consider assignments."""
    pool = get_pool()

    user = await pool.fetchrow("SELECT display_name, email FROM users WHERE uid = $1", uid)
    user_name = user["display_name"] if user else uid

    adder = await pool.fetchval("SELECT display_name FROM users WHERE uid = $1", added_by)
    adder_name = adder or added_by

    msg = (
        f"[System] New member joined: {user_name} (role: {role}), added by {adder_name}.\n\n"
        f"Welcome them. Summarize the current goal state briefly. "
        f"Consider if any tasks could be assigned to them."
    )

    await _send_system_message_to_go(goal_id, msg)


async def on_member_removed(
    goal_id: str,
    uid: str,
    removed_by: str,
):
    """Member left — GO should flag orphaned tasks."""
    pool = get_pool()

    user = await pool.fetchval("SELECT display_name FROM users WHERE uid = $1", uid)
    user_name = user or uid

    # Find their active tasks
    orphaned = await pool.fetch("""
        SELECT id, title, state FROM tasks
        WHERE goal_id = $1 AND assigned_to = $2
          AND status = 'active' AND state NOT IN ('done', 'failed', 'cancelled')
    """, goal_id, uid)

    task_warning = ""
    if orphaned:
        task_list = ", ".join(f"{t['id']} ({t['title']})" for t in orphaned)
        task_warning = (
            f"\nIMPORTANT: {user_name} had {len(orphaned)} active tasks "
            f"that need reassignment: {task_list}"
        )

    msg = (
        f"[System] {user_name} has left the goal.{task_warning}\n\n"
        f"Propose reassignment for any orphaned tasks."
    )

    await _send_system_message_to_go(goal_id, msg)


async def on_task_result_submitted(
    goal_id: str,
    task_id: str,
    result_summary: str,
    submitted_by: str,
):
    """Human or SHCA submitted a task result — GO should review and decide next steps."""
    pool = get_pool()

    task = await pool.fetchrow(
        "SELECT title, assigned_to FROM tasks WHERE id = $1", task_id,
    )
    if not task:
        return

    submitter_name = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", submitted_by,
    ) or submitted_by

    msg = (
        f"[System] Task result submitted for {task_id} — \"{task['title']}\"\n"
        f"Submitted by: {submitter_name}\n"
        f"Result: {result_summary[:500]}\n\n"
        f"Review this result. Does the plan need to evolve based on what was learned? "
        f"Are there new tasks to create or existing tasks to update?"
    )

    await _send_system_message_to_go(goal_id, msg)


async def on_resource_registered(
    goal_id: str,
    resource_id: str,
    created_by: str,
):
    """New resource added to the goal — GO should acknowledge and consider its impact."""
    pool = get_pool()

    resource = await pool.fetchrow(
        "SELECT name, kind, uri, human_context FROM goal_resources WHERE id = $1",
        resource_id,
    )
    if not resource:
        return

    creator_name = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", created_by,
    ) or created_by

    context_note = f"\nContext: {resource['human_context']}" if resource.get("human_context") else ""

    msg = (
        f"[System] New resource added: {resource['name']} ({resource['kind']})\n"
        f"Added by: {creator_name}\n"
        f"{'URI: ' + resource['uri'] if resource.get('uri') else ''}"
        f"{context_note}\n\n"
        f"Use resource_list and resource_summary to inspect it. "
        f"Consider if this affects the current plan or task assignments."
    )

    await _send_system_message_to_go(goal_id, msg)


async def on_resource_changed(goal_id: str, resource_id: str, change_type: str, details: str = ""):
    """Resource changed — notify GO about affected stories."""
    pool = get_pool()

    resource = await pool.fetchrow(
        "SELECT name, kind FROM goal_resources WHERE id = $1", resource_id,
    )
    if not resource:
        return

    affected = await pool.fetch("""
        SELECT sr.story_id, s.title, s.assigned_to
        FROM story_resources sr
        JOIN stories s ON s.id = sr.story_id
        WHERE sr.resource_id = $1
          AND s.state IN ('in_progress', 'accepted', 'awaiting_merge')
    """, resource_id)

    affected_info = ""
    if affected:
        names = ", ".join(f"\"{s['title']}\"" for s in affected)
        affected_info = f"\nAffected stories: {names}"

    msg = (
        f"[System] Resource '{resource['name']}' ({resource['kind']}) updated: {change_type}."
        f"{' ' + details if details else ''}"
        f"{affected_info}"
    )
    await _send_system_message_to_go(goal_id, msg)


async def calculate_blocking_cascade(goal_id: str, blocked_story_id: str) -> list[dict]:
    """Find all stories transitively blocked by this one."""
    pool = get_pool()
    blocked = []
    visited = set()
    queue = [blocked_story_id]

    while queue:
        sid = queue.pop(0)
        dependents = await pool.fetch("""
            SELECT id, title, state FROM stories
            WHERE goal_id = $1 AND $2 = ANY(depends_on)
            AND state NOT IN ('done', 'cancelled')
        """, goal_id, sid)
        for d in dependents:
            if d["id"] not in visited:
                visited.add(d["id"])
                blocked.append(dict(d))
                queue.append(d["id"])

    return blocked


async def on_story_blocked(goal_id: str, story_id: str, reason: str):
    """Story blocked — calculate cascade and notify GO with full downstream impact."""
    pool = get_pool()
    story = await pool.fetchrow("SELECT title FROM stories WHERE id = $1", story_id)
    if not story:
        return

    cascade = await calculate_blocking_cascade(goal_id, story_id)

    cascade_info = ""
    if cascade:
        lines = [f"  - \"{s['title']}\" [{s['state']}]" for s in cascade]
        cascade_info = (
            f"\nCascade impact: {len(cascade)} stories transitively blocked:\n"
            + "\n".join(lines)
        )

    msg = (
        f"[System] Story \"{story['title']}\" is blocked.\n"
        f"Reason: {reason}\n"
        f"{cascade_info}\n\n"
        f"Help resolve this blocker. Consider the impact on downstream stories."
    )
    await _send_system_message_to_go(goal_id, msg)
