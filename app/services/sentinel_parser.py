"""Sentinel parser for claude-code-phase task responses (Stage 3-h).

Each task dispatch ends with the main Claude session emitting one of:
  * PHASE_TASK_DONE T<id> — <summary>
  * PHASE_TASK_BLOCKED T<id> — <single specific question>
  * PHASE_TASK_FAILED T<id> — <reason>

This module:
  1. Parses the sentinel from a Claude response
  2. Updates task state in DB (done / blocked / failed)
  3. Writes a "task progress" message card directly to the chat thread
     (bypassing SHCA's LLM — these are mechanical context updates,
     not conversation turns)
  4. On DONE: triggers dependency resolution so newly-ready tasks
     dispatch
  5. On done/failed for ALL subtasks of a DAG: rolls up to the parent
     DAG task — either marks it done directly (when diff_review_via_gate=TRUE,
     meaning an integrate gate handled review mid-DAG) or triggers diff
     review and moves to awaiting_approval (legacy path, Stage 3-l)
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from app.database import get_pool
from app.ledger.journal import append_event
from app.services.credential_brokers import release_credentials_safe
from app.services.task_states import TERMINAL_PHASE_STATES, TERMINAL_TASK_STATES


_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID, story_shca_actor  # canonical actors

# Phases whose failures are relayed to SHCA for human decision rather than
# silently halting the DAG. Any code fix requires a new cycle + approval.
_TEST_PHASES = {"integration-tester", "deployment"}

# Regex patterns for extracting structured fields from a PHASE_TASK_FAILED body
_FAILURE_FIELD_RE = re.compile(
    r'^(FAILURE_TYPE|COMMAND|CRITERION):\s*(.+?)$', re.MULTILINE
)
_FAILURE_ERROR_RE = re.compile(
    r'ERROR:\s*\n(.*?)(?=\n(?:FAILURE_TYPE|COMMAND|CRITERION):|$)', re.DOTALL
)


# Match a sentinel anywhere in the response (Claude sometimes adds
# whitespace or a trailing newline). The task id is the bare suffix
# after the dash; tolerate slight variations.
_SENTINEL_RE = re.compile(
    r"PHASE_TASK_(DONE|BLOCKED|FAILED)\s+T?([A-Za-z0-9_.-]+)\s*[—\-:]?\s*(.*)",
)

# Phase 2a: the sentinel MUST start its own line (the Stop hook enforces it as the
# final line, and the phase prompts put it last, after any evidence/traceback). Anchor
# to line-start so a sentinel quoted mid-prose (e.g. inside a traceback, or an echoed
# instruction) can't be mistaken for the terminal state.
_SENTINEL_LINE_RE = re.compile(
    r"^\s*PHASE_TASK_(DONE|BLOCKED|FAILED)\s+T?([A-Za-z0-9_.-]+)\s*[—\-:]?\s*(.*)$",
    re.MULTILINE,
)


class ParsedSentinel:
    __slots__ = ("kind", "task_id_ref", "tail")

    def __init__(self, kind: str, task_id_ref: str, tail: str):
        self.kind = kind
        self.task_id_ref = task_id_ref
        self.tail = tail

    def __repr__(self) -> str:
        return f"<Sentinel kind={self.kind} ref={self.task_id_ref!r} tail={self.tail[:40]!r}>"


def _parse_failure_details(text: str) -> dict:
    """Extract structured fields from a PHASE_TASK_FAILED response body.

    Expects the agent to have emitted:
        FAILURE_TYPE: code_defect
        COMMAND: <command that was run>
        ERROR:
        <stderr / traceback>
        CRITERION: <which acceptance criterion failed>

    Returns a dict with whatever fields are present; missing fields are absent.
    """
    details = {}
    for m in _FAILURE_FIELD_RE.finditer(text):
        details[m.group(1).lower()] = m.group(2).strip()
    m = _FAILURE_ERROR_RE.search(text)
    if m:
        details["error"] = m.group(1).strip()[:600]
    return details


def parse_sentinel(text: str) -> Optional[ParsedSentinel]:
    """Return the phase's terminal sentinel, or None.

    Prefers a sentinel that starts its own line (the enforced Phase-2a format),
    taking the LAST such line so a sentinel quoted earlier in prose (a traceback, an
    echoed instruction) can't be mistaken for the terminal state. Falls back to the
    legacy match-anywhere scan only when no line-anchored sentinel exists (back-compat
    for turns produced before the Stop hook / prompt rewrite)."""
    last: Optional[ParsedSentinel] = None
    for m in _SENTINEL_LINE_RE.finditer(text):
        last = ParsedSentinel(kind=m.group(1), task_id_ref=m.group(2), tail=m.group(3).strip())
    if last is not None:
        return last
    for m in _SENTINEL_RE.finditer(text):  # legacy fallback (pre-Phase-2a turns)
        last = ParsedSentinel(kind=m.group(1), task_id_ref=m.group(2), tail=m.group(3).strip())
    return last


async def handle_phase_task_response(
    task: dict, claude_text: str, raw_result: dict,
) -> None:
    """Apply the sentinel + write progress to chat. Called by phase_dispatcher
    after sessions_spawn returns."""
    pool = get_pool()
    task_id = task["id"]
    story_id = task["story_id"]
    goal_id = task["goal_id"]

    # Every sentinel/DAG event below is produced by the SHCA executing this
    # story's claude-code phases → attribute to the SHCA instance, not system.
    sh_actor, sh_type = await story_shca_actor(story_id)

    sentinel = parse_sentinel(claude_text)

    # Always emit a chat progress card with Claude's full text (verbatim).
    await write_task_progress_card(
        goal_id=goal_id,
        story_id=story_id,
        task_id=task_id,
        phase=task.get("phase") or "",
        kind="output",
        text=claude_text,
        sentinel_kind=(sentinel.kind if sentinel else "OFF_PROTOCOL"),
    )

    if not sentinel:
        # Off-protocol — no terminal sentinel in the turn. With the Stop hook + prompt
        # rewrite (Phase 2a) this is unreachable for a clean turn; reaching here means
        # the turn was truncated or the hook failed open. Treat it DETERMINISTICALLY:
        # fail the task + cascade the DAG so the story never sits silently in_progress
        # waiting on the 40-min janitor. Recoverable via replan (code is kept).
        await _emit_ledger(
            goal_id, "phase_task.off_protocol",
            {"task_id": task_id, "story_id": story_id,
             "text_preview": claude_text[:300]},
            actor_id=sh_actor, actor_type=sh_type,
        )
        _log.warning(
            "phase task T%s returned without a terminal sentinel — failing (off-protocol)",
            task_id,
        )
        # Prefer the gateway's SPECIFIC error over the generic off-protocol text
        # so the recovery card is actionable. A turn with no sentinel and a
        # status="error" result means the turn never really ran — e.g. spawnCliDirect
        # refused a misrouted cwd ("cwd is not the story workspace…"), the workspace
        # dir was missing, the claude binary/spawn failed, or the 30-min cap killed
        # it. Surfacing that (vs. "off-protocol / truncated") tells the operator
        # whether to reprovision, retry, or replan.
        gw_error = ""
        if isinstance(raw_result, dict) and raw_result.get("status") == "error":
            gw_error = (raw_result.get("error") or "").strip()
            _stderr = (raw_result.get("stderrSnippet") or "").strip()
            if _stderr:
                gw_error = f"{gw_error} [stderr: {_stderr[:300]}]" if gw_error else _stderr[:300]
        reason = (
            f"Phase turn did not complete: {gw_error}"
            if gw_error
            else "Phase ended without a terminal sentinel (off-protocol / truncated turn)."
        )
        await _mark_task_failed(task_id, reason)
        await release_credentials_safe(task_id, "sentinel_parser:off_protocol")
        await _fail_dag_cascade(goal_id, story_id, task.get("parent_task_id"), task_id, reason)
        return

    short_ref = sentinel.task_id_ref
    summary = sentinel.tail or ""

    if sentinel.kind == "DONE":
        await _mark_task_done(task_id, summary)
        await release_credentials_safe(task_id, "sentinel_parser:DONE")
        await _emit_ledger(
            goal_id, "phase_task.done",
            {"task_id": task_id, "summary": summary, "phase": task.get("phase")},
            actor_id=sh_actor, actor_type=sh_type,
        )
        # Wake dep resolver — any pending tasks whose deps just became
        # done flip to ready, and task_controller picks them up next tick.
        await _resolve_dependents(story_id)
        await _on_dag_tasks_complete(goal_id, story_id, task.get("parent_task_id"))
    elif sentinel.kind == "BLOCKED":
        await _mark_task_blocked(task_id, summary)
        # Revoke credentials at BLOCKED too — the resume path
        # (dispatch_resume_with_answer) mints fresh credentials, so the
        # current key would be orphaned. Strict cleanup keeps the IAM
        # key surface tight.
        await release_credentials_safe(task_id, "sentinel_parser:BLOCKED")
        await _emit_ledger(
            goal_id, "phase_task.blocked",
            {"task_id": task_id, "question": summary, "phase": task.get("phase")},
            actor_id=sh_actor, actor_type=sh_type,
        )
        # Write a dedicated "❓ Awaiting answer" card so the human knows
        # the next chat message will be forwarded to this blocked task.
        await write_task_progress_card(
            goal_id=goal_id,
            story_id=story_id,
            task_id=task_id,
            phase=task.get("phase") or "",
            kind="blocked-question",
            text=summary,
            sentinel_kind="BLOCKED",
        )
        # Also post a recovery card (blocked mode): answering to resume is the primary
        # path (above), but the human may prefer to re-run/change course — Retry / Replan
        # / Abandon. Same buttons as the failed card; the resume path stamps it decided so
        # a stale button can't disrupt a task that later resumed via an answer.
        try:
            from app.services.story_recovery import post_recovery_card  # noqa: PLC0415
            await post_recovery_card(
                goal_id, story_id, task.get("parent_task_id") or "",
                reason=summary, mode="blocked",
            )
        except Exception:
            _log.exception("sentinel_parser: blocked recovery-card post failed (task=%s)", task_id)
    elif sentinel.kind == "FAILED":
        await _mark_task_failed(task_id, summary)
        await release_credentials_safe(task_id, "sentinel_parser:FAILED")
        await _emit_ledger(
            goal_id, "phase_task.failed",
            {"task_id": task_id, "reason": summary, "phase": task.get("phase")},
            actor_id=sh_actor, actor_type=sh_type,
        )
        # For test/deployment phases, relay structured failure details to SHCA
        # so the human can decide next steps. No automatic fix — any code change
        # goes through a new cycle and requires approval.
        if task.get("phase") in _TEST_PHASES:
            await _relay_test_failure_to_shca(task, claude_text, goal_id, story_id)
        # Halt the DAG hard: fail the following subtasks + CANCEL the parent DAG
        # (previously the DAG just stalled because dependents never became ready).
        await _fail_dag_cascade(
            goal_id, story_id, task.get("parent_task_id"), task_id, summary,
        )
    else:
        _log.warning("unknown sentinel kind %r on task %s", sentinel.kind, task_id)


# ── State transitions ────────────────────────────────────────────────────────

async def _mark_task_done(task_id: str, summary: str) -> None:
    pool = get_pool()
    # Guard against clobbering a task that was force-killed (halted) or
    # cancelled while the turn was in flight. A late shca.claude.invoke
    # response must never overwrite operator intent.
    await pool.execute(
        """UPDATE tasks SET
               state = 'done',
               result_summary = $1,
               completed_at = NOW(),
               last_blocker_question = NULL,
               updated_at = NOW()
           WHERE id = $2
             AND state NOT IN ('halted', 'cancelled', 'done')""",
        summary, task_id,
    )


async def _mark_task_blocked(task_id: str, question: str) -> None:
    pool = get_pool()
    await pool.execute(
        """UPDATE tasks SET
               state = 'blocked',
               blocked_reason = $1,
               last_blocker_question = $1,
               last_blocker_at = NOW(),
               blocker_count = blocker_count + 1,
               updated_at = NOW()
           WHERE id = $2
             AND state NOT IN ('halted', 'cancelled', 'done')""",
        question, task_id,
    )


async def _mark_task_failed(task_id: str, reason: str) -> None:
    pool = get_pool()
    await pool.execute(
        """UPDATE tasks SET
               state = 'failed',
               blocked_reason = $1,
               last_blocker_question = NULL,
               completed_at = NOW(),
               updated_at = NOW()
           WHERE id = $2
             AND state NOT IN ('halted', 'cancelled', 'done')""",
        reason, task_id,
    )


async def _resolve_dependents(story_id: str) -> None:
    """Flip pending tasks whose deps are all done/approved → ready.

    Covers both claude-code-phase and system-gate tasks so that G1/G2 gate
    tasks are unblocked correctly when their upstream phase tasks complete.
    """
    pool = get_pool()
    await pool.execute(
        """UPDATE tasks t SET
               state = 'ready',
               updated_at = NOW()
          WHERE t.story_id = $1
            AND t.state = 'pending'
            AND t.executor IN ('claude-code-phase', 'system-gate')
            AND NOT EXISTS (
                SELECT 1 FROM unnest(t.depends_on) AS dep_id
                  LEFT JOIN tasks dt ON dt.id = dep_id
                 WHERE dt.id IS NULL OR dt.state NOT IN ('done','approved')
            )""",
        story_id,
    )
    # Wake task_controller so it dispatches next tick (trigger is async — await it).
    try:
        from app.task_controller import trigger as tc_trigger
        await tc_trigger()
    except Exception:
        pass


async def _on_dag_tasks_complete(
    goal_id: str, story_id: str, dag_task_id: Optional[str],
) -> None:
    """Fires when all claude-code-phase subtasks of a DAG reach a terminal state.

    Two paths:
      diff_review_via_gate=TRUE  — an integrate gate already handled review + merge
                                   mid-DAG; mark DAG done directly, no second review.
      diff_review_via_gate=FALSE — legacy path: trigger diff review and move to
                                   awaiting_approval for human sign-off.
    """
    if not dag_task_id:
        return
    pool = get_pool()
    # DAG rollup events are driven by the SHCA's phase completions → SHCA instance.
    sh_actor, sh_type = await story_shca_actor(story_id)
    rows = await pool.fetch(
        "SELECT state FROM tasks WHERE parent_task_id = $1 "
        "AND executor = 'claude-code-phase'",
        dag_task_id,
    )
    states = [r["state"] for r in rows]
    if not states:
        return

    # Any still-running? Bail. (single terminal set — task_states.TERMINAL_PHASE_STATES)
    if any(s not in TERMINAL_PHASE_STATES for s in states):
        return

    # Any failed? Fail the DAG deterministically via the shared parent-fail path so
    # the parent state + GO notification are identical whether we arrive here (rollup)
    # or via the sentinel cascade. Idempotent — no double-notify.
    if any(s == "failed" for s in states):
        await _fail_dag_parent(goal_id, story_id, dag_task_id, states=states)
        return

    # All subtasks terminal & none failed. Read the suppression flag set by dag_ingest
    # when an integrate gate is present (main's integrate-gate feature). A missing row is
    # treated as FALSE to preserve legacy behaviour.
    #
    # Both rollup branches use a CAS-flip out of the running state (dev's idempotency guard):
    # ONLY the caller that actually flips proceeds. Two callers can reach here for the SAME
    # last subtask — the sentinel-DONE path AND the board "Done" path
    # (check_parent_completion) — and begin_diff_review unconditionally INSERTs a fresh
    # approval + card, so without this guard a race posts TWO cards/approvals for one DAG.
    # The conditional UPDATE + RETURNING makes the rollup idempotent: the loser bails.
    dag_task = await pool.fetchrow(
        "SELECT diff_review_via_gate FROM tasks WHERE id = $1",
        dag_task_id,
    )
    via_gate = bool(dag_task["diff_review_via_gate"]) if dag_task else False

    if via_gate:
        # Review + merge already completed at the integrate gate mid-DAG.
        # Move straight to done — no second diff review.
        flipped = await pool.fetchval(
            """UPDATE tasks SET state = 'done', updated_at = NOW()
                WHERE id = $1
                  AND state NOT IN ('awaiting_approval', 'done', 'approved',
                                    'cancelled', 'rejected', 'failed')
             RETURNING id""",
            dag_task_id,
        )
        if not flipped:
            return  # another caller already rolled this DAG up
        await pool.execute(
            "UPDATE stories SET current_phase = 'awaiting_next', updated_at = NOW() WHERE id = $1",
            story_id,
        )
        await _emit_ledger(
            goal_id, "dag.complete",
            {"dag_task_id": dag_task_id, "story_id": story_id,
             "subtask_count": len(states), "via_gate": True},
            actor_id=sh_actor, actor_type=sh_type,
        )
        _log.info(
            "sentinel_parser: DAG %s complete via integrate gate — skipping diff review (story=%s)",
            dag_task_id, story_id,
        )
    else:
        # Legacy path: CAS → awaiting_approval, then trigger diff capture / card.
        flipped = await pool.fetchval(
            """UPDATE tasks SET state = 'awaiting_approval', updated_at = NOW()
                WHERE id = $1
                  AND state NOT IN ('awaiting_approval', 'done', 'approved',
                                    'cancelled', 'rejected', 'failed')
             RETURNING id""",
            dag_task_id,
        )
        if not flipped:
            return  # another caller already rolled this DAG up — don't double-post
        await pool.execute(
            """UPDATE stories SET current_phase = 'awaiting_review', updated_at = NOW()
                WHERE id = $1""",
            story_id,
        )
        await _emit_ledger(
            goal_id, "dag.tasks_complete",
            {"dag_task_id": dag_task_id, "story_id": story_id,
             "subtask_count": len(states)},
            actor_id=sh_actor, actor_type=sh_type,
        )
        # Capture diff + write diff-review card.
        try:
            from app.services.diff_review import begin_diff_review
            await begin_diff_review(goal_id, story_id, dag_task_id)
        except Exception:
            _log.exception("diff_review trigger failed for DAG %s", dag_task_id)


async def _fail_dag_parent(
    goal_id: str, story_id: str, dag_task_id: str,
    *, states=None, reason: str = "",
) -> None:
    """Deterministically mark a DAG parent 'failed' and notify the GO.

    This is the ONE place a DAG parent becomes 'failed'. Both the sentinel cascade
    (_fail_dag_cascade) and the rollup (_on_dag_tasks_complete) call it, so they can
    never disagree — previously the cascade set the parent 'cancelled' with no GO
    notify while the rollup set it 'failed' with a notify, a race that made the final
    state and GO awareness path-dependent. CAS-guarded: if the parent already settled,
    this is a no-op and does NOT re-emit / re-notify."""
    pool = get_pool()
    sh_actor, sh_type = await story_shca_actor(story_id)
    flipped = await pool.fetchval(
        "UPDATE tasks SET state = 'failed', updated_at = NOW() "
        "WHERE id = $1 AND state <> ALL($2) RETURNING id",
        dag_task_id, list(TERMINAL_TASK_STATES),
    )
    if not flipped:
        return  # already terminal — don't double-emit or double-notify
    payload = {"dag_task_id": dag_task_id, "story_id": story_id}
    if reason:
        payload["reason"] = reason[:300]
    if states is not None:
        payload["subtask_states"] = {s: states.count(s) for s in set(states)}
    await _emit_ledger(goal_id, "dag.failed", payload,
                       actor_id=sh_actor, actor_type=sh_type)
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(goal_id, dag_task_id, "in_progress", "failed", "system")
    except Exception as _exc:
        _log.warning("on_task_state_changed failed after DAG failure: %s", _exc)
    # Phase 3: post the recovery card (Replan / Retry / Abandon) to the story thread.
    if story_id:
        try:
            from app.services.story_recovery import post_recovery_card
            await post_recovery_card(goal_id, story_id, dag_task_id, reason=reason or "")
        except Exception as _exc:
            _log.warning("post_recovery_card failed after DAG failure: %s", _exc)


async def _fail_dag_cascade(
    goal_id: str, story_id: str, dag_task_id: Optional[str], failed_task_id: str,
    reason: str = "",
) -> None:
    """A claude-code-phase subtask FAILED → fail every not-yet-terminal sibling subtask
    and fail the parent DAG (deterministically, via _fail_dag_parent).

    claude-code DAGs run sequentially on ONE Claude session, so a failure means the
    remaining (pending/ready/blocked) subtasks can't proceed — the following subtasks
    are marked 'failed' and the parent DAG is 'failed', so the failure is explicit and
    the workspace can be reaped. The human/GO sees it (the FAILED progress card already
    posted) and can re-plan a fresh DAG.

    NOTE: the story `current_phase` is intentionally left as-is here. On failure the
    story stays in its execution phase and the failure is carried by the `next_action`
    overlay (Phase 2b) — not by flipping the phase to awaiting_next, which would route
    the assistant to the wrong skill and drop the "what's next" prompt."""
    if not dag_task_id:
        return
    pool = get_pool()
    # Fail every not-yet-terminal sibling subtask of this DAG (single terminal set).
    cascaded = await pool.fetch(
        """UPDATE tasks SET state = 'failed', updated_at = NOW()
            WHERE parent_task_id = $1 AND executor = 'claude-code-phase'
              AND id <> $2
              AND state <> ALL($3)
         RETURNING id""",
        dag_task_id, failed_task_id, list(TERMINAL_PHASE_STATES),
    )
    # Fail the parent DAG deterministically (+ GO notify), same path as the rollup.
    await _fail_dag_parent(goal_id, story_id, dag_task_id, reason=reason)
    _log.info(
        "DAG %s failed after subtask %s failed — cascaded %d sibling(s) to failed",
        dag_task_id, failed_task_id, len(cascaded),
    )


# ── Chat-card writing (bypasses SHCA LLM) ────────────────────────────────────

def _format_progress_for_shca(
    *, task_id: str, phase: str, sentinel: str, text: str,
) -> str:
    """Build the plain-text task-controller message that gets forwarded
    to SHCA's gateway session. Matches the format SHCA's
    assistant_progress_interpretation skill is written to recognize.
    Compact and unambiguous; the leading [task-controller] tag is the
    signal SHCA's LLM uses to apply the "default silent" rule.
    """
    sentinel_label = (sentinel or "").upper() or "OUTPUT"
    body = (text or "").strip()
    return (
        f"[task-controller] T{task_id} · phase={phase} · {sentinel_label}\n\n"
        f"{body}"
    )


async def write_task_progress_card(
    *, goal_id: str, story_id: str, task_id: str,
    phase: str, kind: str, text: str, sentinel_kind: str = "",
) -> None:
    """Write a message row directly into the thread that the human is
    looking at, marked as sender='task-controller'. The chat UI renders
    it as a card via card_data; SHCA's LLM gets a forwarded copy via
    the gateway (so it can proactively surface failures etc.)."""
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT active_thread_id FROM stories WHERE id = $1",
        story_id,
    )
    if not story or not story["active_thread_id"]:
        _log.warning(
            "task progress card: no active_thread_id on story %s — skipping",
            story_id,
        )
        return
    thread_id = story["active_thread_id"]

    import json as _json
    card_data = {
        "kind": "phase_task_progress",
        "task_id": task_id,
        "phase": phase,
        "card_subkind": kind,            # 'output' | 'blocked-question' | ...
        "sentinel": sentinel_kind,
    }
    msg_id = str(uuid.uuid4())
    # Store the fully-formatted SHCA-facing payload (header + full body)
    # as the row's content. The chat UI renders via card_data, so the
    # in-content [task-controller] header doesn't affect display; this
    # lets the queue forward reuse THIS row via persisted_message_id
    # (no duplicate enqueue row, full body — not [:200] — reaches the SHCA).
    formatted = _format_progress_for_shca(
        task_id=task_id, phase=phase, sentinel=sentinel_kind, text=text,
    )
    await pool.execute(
        """INSERT INTO messages (
               id, thread_id, role, sender,
               content, card_data, created_at
           ) VALUES ($1, $2, 'agent', 'task-controller',
                     $3, $4::JSONB, NOW())""",
        msg_id, thread_id, formatted, _json.dumps(card_data),
    )

    # Broadcast via WebSocket so open chat tabs see the card live.
    # Persistent row above means a refresh also shows it; broadcast is
    # purely for live update.
    try:
        from app.openclaw import session_manager
        thread_row = await pool.fetchrow(
            "SELECT agent_id FROM threads WHERE id = $1", thread_id,
        )
        if thread_row:
            frame = {
                "type": "phase_task_progress",
                "task_id": task_id,
                "phase": phase,
                "card_subkind": kind,
                "sentinel": sentinel_kind,
                "content": text,
            }
            await session_manager.broadcast_to_thread(
                goal_id, thread_row["agent_id"], thread_id, frame,
            )
    except Exception:
        _log.exception(
            "phase_task_progress: WS broadcast failed (task=%s) — card "
            "is in DB, refresh will show it", task_id,
        )

    # Forward the task-controller message to SHCA's gateway session so the
    # SHCA's LLM sees the progress in real-time. Goes through the chat_module
    # queue (when CHAT_QUEUE_ENABLED) for durability + per-thread serialization
    # with concurrent human messages. persisted_message_id reuses the row we
    # just INSERTed (no double-persist; full body — not [:200] — flows through).
    # queue_kind=system_wake → SHCA runs a turn and decides reply vs [NO_REPLY]
    # per SHCA's SOUL.md + assistant_progress_interpretation skill.
    try:
        from app.openclaw import session_manager as _sm
        _agent_row = await pool.fetchrow(
            "SELECT agent_id FROM threads WHERE id = $1", thread_id,
        )
        if _agent_row:
            from app.services.agent_driver import ensure_shca_session
            _session = await ensure_shca_session(
                goal_id, _agent_row["agent_id"], thread_id,
            )
            if _session:
                envelope = {
                    "type": "message",
                    "content": formatted,
                    "thread_id": thread_id,
                    "origin": "task-controller",
                    "sender_name": "task-controller",
                }
                await _session.send_to_gateway(
                    _json.dumps(envelope),
                    persisted_message_id=msg_id,
                    queue_kind="system_wake",
                )
                _log.info(
                    "phase_task_progress: forwarded to SHCA T%s phase=%s sentinel=%s (full body, %d chars)",
                    task_id, phase, sentinel_kind, len(formatted),
                )
            else:
                _log.info(
                    "phase_task_progress: no live SHCA session for goal=%s agent=%s thread=%s "
                    "— SHCA will pick it up via recent_progress in next snapshot",
                    goal_id, _agent_row["agent_id"], thread_id,
                )
    except Exception:
        _log.exception(
            "phase_task_progress: forward-to-SHCA failed (task=%s) — "
            "card is in DB + recent_progress in next snapshot",
            task_id,
        )

    # Relay compact progress notification to the bound Slack channel.
    # Only relay milestone sentinels (DONE / FAILED / BLOCKED) to avoid
    # flooding the channel with every streaming output chunk.
    if sentinel_kind.upper() in ("DONE", "FAILED", "BLOCKED", "PHASE_TASK_DONE",
                                  "PHASE_TASK_FAILED", "PHASE_TASK_BLOCKED"):
        try:
            from app.channels.slack.outbound import relay_phase_task_progress
            import asyncio as _asyncio_sp
            _asyncio_sp.create_task(
                relay_phase_task_progress(
                    goal_id=goal_id,
                    thread_id=thread_id,
                    story_id=story_id,
                    task_id=task_id,
                    phase=phase,
                    sentinel=sentinel_kind,
                    text=text,
                )
            )
        except Exception:
            _log.debug("phase_task_progress: Slack relay scheduling failed (non-fatal)")


# ── Test / deployment failure relay ─────────────────────────────────────────

async def _relay_test_failure_to_shca(
    task: dict,
    claude_text: str,
    goal_id: str,
    story_id: str,
) -> None:
    """Write an action-required card and forward structured failure details
    to SHCA when an integration-tester or deployment phase fails.

    No automatic fix is triggered — the human decides via SHCA:
      - Give fix instructions → SHCA creates a new cycle for approval
      - Ask SHCA to replan the story
    """
    import json as _json

    pool = get_pool()
    task_id = task["id"]
    phase = task.get("phase") or ""
    details = _parse_failure_details(claude_text)

    # Find direct upstream coder tasks so SHCA knows what needs fixing
    depends_on = task.get("depends_on") or []
    coder_tasks: list[dict] = []
    if depends_on:
        rows = await pool.fetch(
            """SELECT id, title, phase FROM tasks
                WHERE id = ANY($1) AND story_id = $2""",
            depends_on, story_id,
        )
        coder_tasks = [
            dict(r) for r in rows
            if r["phase"] in ("coder", "code-explorer")
        ]

    # Build the relay message SHCA will see and surface to the human
    lines = [
        f"[task-controller] T{task_id} · phase={phase} · FAILED — action required",
        "",
        f"**{phase}** failed. The DAG is paused. Human decision needed before work continues.",
        "",
    ]
    if details.get("criterion"):
        lines.append(f"**Failing criterion:** {details['criterion']}")
    if details.get("command"):
        lines.append(f"**Command:** `{details['command']}`")
    if details.get("error"):
        lines.append(f"**Error:**\n```\n{details['error']}\n```")
    if coder_tasks:
        lines.append("")
        lines.append("**Upstream coder task(s):**")
        for t in coder_tasks:
            lines.append(f"- T{t['id']} ({t['phase']}): {t['title']}")
    lines += [
        "",
        "Options:",
        "- Tell me what the coder needs to fix — I will create a new fix cycle for your approval.",
        "- Ask me to replan this story.",
    ]
    relay_text = "\n".join(lines)

    # Write card to the story thread (visible in UI and forwarded to SHCA)
    story = await pool.fetchrow(
        "SELECT active_thread_id FROM stories WHERE id = $1", story_id,
    )
    if not story or not story["active_thread_id"]:
        _log.warning(
            "_relay_test_failure_to_shca: no active_thread_id on story %s — cannot relay",
            story_id,
        )
        return
    thread_id = story["active_thread_id"]

    card_data = _json.dumps({
        "kind": "phase_task_progress",
        "task_id": task_id,
        "phase": phase,
        "card_subkind": "test-failure-action-required",
        "sentinel": "FAILED",
        "failure_details": details,
        "upstream_coder_tasks": [
            {"id": str(t["id"]), "title": t["title"]} for t in coder_tasks
        ],
    })
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages
               (id, thread_id, role, sender, content, card_data, created_at)
           VALUES ($1, $2, 'agent', 'task-controller', $3, $4::JSONB, NOW())""",
        msg_id, thread_id, relay_text, card_data,
    )

    from app.openclaw import session_manager as _sm
    thread_row = await pool.fetchrow(
        "SELECT agent_id FROM threads WHERE id = $1", thread_id,
    )

    # Broadcast via WebSocket for live update in open chat tabs
    try:
        if thread_row:
            await _sm.broadcast_to_thread(
                goal_id, thread_row["agent_id"], thread_id,
                {
                    "type": "phase_task_progress",
                    "task_id": task_id,
                    "phase": phase,
                    "card_subkind": "test-failure-action-required",
                    "sentinel": "FAILED",
                    "content": relay_text,
                },
            )
    except Exception:
        _log.exception(
            "_relay_test_failure_to_shca: WS broadcast failed (task=%s) — card is in DB",
            task_id,
        )

    # Forward full relay text to SHCA's live session
    try:
        if thread_row:
            session = _sm.find(goal_id, thread_row["agent_id"], thread_id)
            if session:
                envelope = _json.dumps({
                    "type": "message",
                    "content": relay_text,
                    "thread_id": thread_id,
                    "origin": "task-controller",
                    "sender_name": "task-controller",
                })
                await session.send_to_gateway(envelope)
                _log.info(
                    "_relay_test_failure_to_shca: forwarded to SHCA (task=T%s phase=%s "
                    "coder_tasks=%s)",
                    task_id, phase, [t["id"] for t in coder_tasks],
                )
            else:
                _log.info(
                    "_relay_test_failure_to_shca: no live SHCA session — "
                    "card in DB, SHCA picks up via next snapshot (goal=%s)",
                    goal_id,
                )
    except Exception:
        _log.exception(
            "_relay_test_failure_to_shca: SHCA forward failed (task=%s) — card is in DB",
            task_id,
        )


# ── Ledger helper ────────────────────────────────────────────────────────────

async def _emit_ledger(goal_id: str, event_type: str, payload: dict, *,
                       actor_id=SYSTEM_ACTOR_ID, actor_type="system") -> None:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    actor_id=actor_id, actor_type=actor_type,
                    event_type=event_type,
                    payload=payload,
                    context_goal_id=goal_id,
                )
    except Exception:
        _log.exception("ledger emit failed: %s", event_type)
