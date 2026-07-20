"""Pre-turn state snapshot for SHCA (Phase 4 Stage 4).

Before every human message reaches SHCA, the chat router inserts a
`story_state_snapshot` message into the thread. The snapshot is the
authoritative source of truth for SHCA's current story, phase, type,
workspace, Claude session, and recent progress — built fresh from the
DB at the millisecond the human's message is processed.

SHCA's skills read the snapshot at the start of every turn and route
behavior accordingly. The skill-routing table lives in
`template_files.AGENTS.md` (rewritten in phase5a); the kickoff message
routes via that table (the old `assistant_operator_kickoff` entry-point
skill was folded into AGENTS.md and deprecated).

This module:
  - `build_story_state_snapshot(thread_id)` -> dict
        Returns the structured snapshot for the active story on this
        thread. If no story is active, returns a "general mode"
        snapshot (all fields None / "none").
  - `format_snapshot_message(snapshot)` -> str
        Renders the snapshot as a fenced markdown code block that's
        easy for SHCA's LLM to parse.
  - `insert_state_snapshot_message(thread_id)` -> str | None
        Convenience: builds snapshot for thread, formats it, INSERTs a
        message row attributed to 'task-controller' with
        card_data.kind='story_state_snapshot'. Returns the message id,
        or None if no thread / story.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from app.database import get_pool


_log = logging.getLogger(__name__)


_NON_TERMINAL_STATES = ("accepted", "in_progress", "blocked")


def claude_code_status(
    session_id: str | None,
    *,
    prespawn_in_flight: bool,
    phase_in_flight: bool,
) -> str:
    """Opaque Claude Code session status for the assistant's state snapshot —
    C7. The assistant NEVER sees the raw session id (it can't misuse or
    mis-resolve one it never has); the ``claude_code_communication`` tool
    resolves the real session server-side. The four states stay distinct so
    the skills can branch send-vs-wait on them:

      not_started  — no session yet; the assistant's first message starts one.
      provisioning — workspace/session still being set up (prespawn sentinel); wait.
      busy         — a phase turn (or system gate) owns the session; don't message,
                     tell the human to resend later.
      ready        — a live session exists and is idle; safe to message.
    """
    sid = session_id or ""
    if prespawn_in_flight or sid.startswith("__prespawn_"):
        return "provisioning"
    if phase_in_flight:
        return "busy"
    if not sid:
        return "not_started"
    return "ready"


async def resolve_active_focus_story(
    pool, *, thread_id: str | None = None, instance_id: str | None = None,
):
    """Deterministic active story for an SHCA thread or instance.

    Returns ``(story_row | None, ambiguous: bool)``. Prefers the story flagged
    ``is_active_focus`` (set at assignment in provision_assistant_thread_for_story);
    falls back to the single non-terminal story when no focus flag is set; returns
    ``(None, True)`` when >1 non-terminal story exists with no focus flag (genuinely
    ambiguous — callers should refuse rather than guess with updated_at).

    Both the state snapshot (thread-keyed) and the /active-story endpoint
    (instance-keyed) call this, so they always resolve to the SAME story. Since
    shca_instance_id embeds thread_id, the two keys can never disagree on focus.
    """
    if thread_id:
        key_pred, key_val = "active_thread_id = $1", thread_id
    else:
        key_pred, key_val = "LOWER(shca_instance_id) = LOWER($1)", instance_id
    focus = await pool.fetchrow(
        f"SELECT * FROM stories WHERE {key_pred} "
        "AND state = ANY($2::text[]) AND is_active_focus "
        "ORDER BY updated_at DESC NULLS LAST LIMIT 1",
        key_val, list(_NON_TERMINAL_STATES),
    )
    if focus:
        return focus, False
    rows = await pool.fetch(
        f"SELECT * FROM stories WHERE {key_pred} AND state = ANY($2::text[])",
        key_val, list(_NON_TERMINAL_STATES),
    )
    if len(rows) == 1:
        return rows[0], False
    if not rows:
        return None, False
    return None, True  # >1 non-terminal, no focus flag → ambiguous


async def _compute_next_action(pool, story_id: str, phase: str) -> dict:
    """Phase 2b: compute the `next_action` overlay for a code story — the single
    signal the assistant routes on so it always knows the next step (and never has to
    infer it from the phase alone). Derived from the DAG parent + its phase children:

      - a blocked child      → needs_answer   (assistant answers/escalates)
      - the DAG failed        → failed         (assistant surfaces reason + options)
      - the DAG awaiting review→ needs_review   (assistant surfaces the diff gate)
      - otherwise             → ok

    See ASSISTANT_WORKFLOW_REFACTOR_PLAN.md §4. NB: option A's sentinel carries no
    blocker `kind`, so needs_answer defaults kind='question'; kind enrichment (resource/
    approval) is a follow-up that needs the sentinel/phase_report to declare it."""
    dag = await pool.fetchrow(
        "SELECT id, state FROM tasks WHERE story_id = $1 AND executor = 'claude-code-dag' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",  # id DESC = deterministic tiebreaker
        story_id,
    )
    if not dag:
        return {"state": "ok"}

    # A blocked phase child needs a human/external input before it can continue.
    blocked = await pool.fetchrow(
        "SELECT id, last_blocker_question, blocked_reason FROM tasks "
        "WHERE parent_task_id = $1 AND executor = 'claude-code-phase' AND state = 'blocked' "
        "ORDER BY last_blocker_at DESC NULLS LAST LIMIT 1",
        dag["id"],
    )
    if blocked:
        return {
            "state": "needs_answer",
            "kind": "question",
            "task": blocked["id"],
            "reason": blocked["last_blocker_question"] or blocked["blocked_reason"] or "",
            "human_decision_required": True,
            # Answering in chat resumes the task (primary). The recovery buttons are an
            # escape hatch when answering isn't the right move — surface both to the human.
            # `review_keep` salvages completed-phase work; `abandon` discards it.
            "options": ["answer", "retry", "review_keep", "replan", "abandon"],
        }

    # DAG failed → surface the failing child's reason + recovery options.
    if dag["state"] == "failed":
        failed = await pool.fetchrow(
            "SELECT id, blocked_reason, phase FROM tasks "
            "WHERE parent_task_id = $1 AND executor = 'claude-code-phase' AND state = 'failed' "
            "ORDER BY completed_at DESC NULLS LAST LIMIT 1",
            dag["id"],
        )
        options = ["replan", "retry", "review_keep", "abandon"]
        if failed and (failed["phase"] or "") == "code-reviewer":
            options = ["replan", "view_diff", "review_keep", "abandon"]
        return {
            "state": "failed",
            "task": (failed and failed["id"]) or dag["id"],
            "reason": (failed and failed["blocked_reason"]) or "",
            "human_decision_required": True,
            "options": options,
        }

    # DAG waiting on the human diff-review gate.
    if dag["state"] == "awaiting_approval" or phase == "awaiting_review":
        return {"state": "needs_review", "task": dag["id"], "human_decision_required": True}

    return {"state": "ok"}


async def build_story_state_snapshot(thread_id: str) -> dict:
    """Build a state snapshot for the active story on this thread.

    If no story is active, returns a snapshot with story_id='none' so
    SHCA can detect general mode.
    """
    pool = get_pool()

    # Deterministic active story (is_active_focus + single-non-terminal fallback).
    story, _ambiguous = await resolve_active_focus_story(pool, thread_id=thread_id)

    if not story:
        return {
            "story_id": "none",
            "story_title": "none",
            "story_type": "none",
            "phase": "none",
            "workspace_path": "none",
            "recent_progress": [],
        }

    story_id = story["id"]

    # Infer story_type from attached resources. Has any kind in
    # ('repo', 'branch')? → code. Else → generic.
    code_resource = await pool.fetchval(
        """SELECT 1
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.kind IN ('repo', 'branch')
            LIMIT 1""",
        story_id,
    )
    story_type = "code" if code_resource else "generic"

    snapshot: dict = {
        "story_id": story_id,
        "story_title": story["title"] or "",
        "story_type": story_type,
        "phase": story["current_phase"] or "discovery",
        "workspace_path": story["workspace_path"] or "none",
        "execution_paused": bool(story.get("execution_paused")),
    }

    if story_type == "code":
        # Pull repo + branch from attached resources.
        repo_row = await pool.fetchrow(
            """SELECT gr.repo_name, gr.branch_name
                 FROM story_resources sr
                 JOIN goal_resources gr ON gr.id = sr.resource_id
                WHERE sr.story_id = $1
                  AND gr.kind IN ('repo', 'branch')
                  -- Never surface the repo's TRUNK designation as the story's branch.
                  AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
                -- Parity with the other resolvers: prefer the feature branch, then any
                -- branch over the bare repo, so the snapshot's branch matches what the
                -- workspace actually clones.
                ORDER BY (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC
                LIMIT 1""",
            story_id,
        )
        snapshot["repo"] = (repo_row and repo_row["repo_name"]) or "none"
        snapshot["branch"] = (repo_row and repo_row["branch_name"]) or "none"

        # Plan path lookup. plan_versions has the canonical record.
        plan_path = await pool.fetchval(
            """SELECT plan_doc_path FROM plan_versions
                WHERE id = $1""",
            story.get("current_plan_id"),
        ) if story.get("current_plan_id") else None
        snapshot["plan_path"] = plan_path or "(not written yet)"

        # C7: show an OPAQUE Claude Code status, never the raw session id. The
        # assistant talks to Claude only via claude_code_communication, which
        # resolves the real session server-side. "busy" needs to know whether a
        # phase task owns the session — same busy_stories predicate the
        # dispatcher and /active-story use.
        _csid = story["current_claude_session_id"] or ""
        _phase_in_flight = bool(await pool.fetchval(
            """SELECT EXISTS (
                 SELECT 1 FROM tasks
                  WHERE story_id = $1
                    AND ((executor = 'claude-code-phase'
                          AND state IN ('in_progress', 'dispatching', 'blocked'))
                         OR (executor = 'system-gate'
                             AND state IN ('in_progress', 'dispatching', 'awaiting_approval')))
               )""",
            story_id,
        ))
        snapshot["claude_code"] = claude_code_status(
            _csid,
            prespawn_in_flight=_csid.startswith("__prespawn_"),
            phase_in_flight=_phase_in_flight,
        )

        # Phase 2b: the next_action overlay — the assistant's single "what's next"
        # signal (routes on this before phase). Failure/blocked are carried here, not
        # by the story phase, so a failed DAG on an 'executing' story is still surfaced.
        snapshot["next_action"] = await _compute_next_action(pool, story_id, snapshot["phase"])

    # Recent progress — last 5 phase_task_progress / diff_review events
    # for this thread, oldest-first so the list reads chronologically.
    progress_rows = await pool.fetch(
        """SELECT card_data, content, created_at
             FROM messages
            WHERE thread_id = $1
              AND sender = 'task-controller'
              AND card_data IS NOT NULL
              AND card_data->>'kind' IN ('phase_task_progress', 'diff_review')
            ORDER BY created_at DESC
            LIMIT 5""",
        thread_id,
    )
    recent: list[str] = []
    for row in reversed(progress_rows):  # oldest -> newest
        cd = row["card_data"] if isinstance(row["card_data"], dict) else json.loads(row["card_data"] or "{}")
        kind = cd.get("kind")
        if kind == "phase_task_progress":
            task_id = cd.get("task_id") or ""
            phase = cd.get("phase") or ""
            sentinel = (cd.get("sentinel") or "").upper()
            # First line of the content for one-liner summary
            summary = (row["content"] or "").split("\n", 1)[0][:120]
            recent.append(
                f"[T{task_id} · phase={phase} · {sentinel}] {summary}"
            )
        elif kind == "diff_review":
            stat = cd.get("stat") or ""
            recent.append(
                f"[DAG {cd.get('dag_task_id', '')} · diff_review · READY] {stat}"
            )
    snapshot["recent_progress"] = recent

    return snapshot


def format_snapshot_message(snapshot: dict) -> str:
    """Render the snapshot as a fenced markdown block + plain text wrapper.

    The fenced block uses the language tag `story_state_snapshot` so
    SHCA's skills can find it deterministically.
    """
    lines: list[str] = ["```story_state_snapshot"]

    # Order matters for human readability.
    field_order = [
        "story_id", "story_title", "story_type", "phase",
        "execution_paused",
        "workspace_path", "repo", "branch", "plan_path", "claude_code",
    ]
    for field in field_order:
        if field in snapshot:
            value = snapshot[field]
            lines.append(f"{field}: {value}")

    # next_action overlay (Phase 2b) — the assistant routes on this first.
    na = snapshot.get("next_action")
    if isinstance(na, dict):
        lines.append("next_action:")
        lines.append(f"  state: {na.get('state', 'ok')}")
        for k in ("kind", "task"):
            if na.get(k):
                lines.append(f"  {k}: {na[k]}")
        if na.get("reason"):
            _r = str(na["reason"]).replace("\n", " ")[:300]
            lines.append(f'  reason: "{_r}"')
        if "human_decision_required" in na:
            lines.append(f"  human_decision_required: {str(bool(na['human_decision_required'])).lower()}")
        if na.get("options"):
            lines.append(f"  options: [{', '.join(na['options'])}]")

    # recent_progress is a list rendered as YAML-style block
    recent = snapshot.get("recent_progress") or []
    if recent:
        lines.append("recent_progress:")
        for entry in recent:
            # Quote-wrap to keep YAML-like parseability if anyone tries
            lines.append(f'  - "{entry}"')
    else:
        lines.append("recent_progress: []")

    lines.append("```")
    return "\n".join(lines)


# Markers used to delimit the appended state context inside a human
# message's content. The control panel appends state context between
# these markers; the UI + Slack relay strip everything from the BEGIN
# marker onward when rendering. SHCA's LLM sees the full content
# including the context — that's the whole point.
#
# Format chosen to be:
#   - Unmistakable (won't appear in normal chat)
#   - Easy to parse with a simple split
#   - Survives markdown rendering without leaking visible glyphs

STATE_CONTEXT_BEGIN_MARKER = "<!--shca-state-context-->"
STATE_CONTEXT_END_MARKER = "<!--/shca-state-context-->"


async def append_state_context_to_content(thread_id: str, content: str) -> str:
    """Append a state-context block to the human's chat content.

    The augmented content is stored in the messages table verbatim and
    forwarded to the gateway. UI + Slack relays strip everything from
    STATE_CONTEXT_BEGIN_MARKER onward before display. SHCA's LLM sees
    the whole thing.

    If anything goes wrong building the snapshot, the original content
    is returned untouched (SHCA falls back to IDENTITY.md).
    """
    if not thread_id:
        return content

    try:
        snapshot = await build_story_state_snapshot(thread_id)
        block = format_snapshot_message(snapshot)
    except Exception:
        _log.exception(
            "append_state_context: snapshot build failed (thread=%s) — "
            "sending message without state context",
            thread_id,
        )
        return content

    return (
        f"{content}\n\n"
        f"{STATE_CONTEXT_BEGIN_MARKER}\n"
        f"{block}\n"
        f"{STATE_CONTEXT_END_MARKER}"
    )


def strip_state_context(content: str) -> str:
    """Strip the state-context block (and everything after the BEGIN
    marker) from a message content. Used by UI + Slack rendering paths
    so the human only sees their own text."""
    if not content:
        return content
    idx = content.find(STATE_CONTEXT_BEGIN_MARKER)
    if idx == -1:
        return content
    # Strip trailing whitespace/newlines that were added as a separator
    return content[:idx].rstrip()
