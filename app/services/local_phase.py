"""Local Execution Mode — phase-status intake (M3).

A local runner (Claude Code / Codex) reports phase completion via the MCP tool
`report_phase_status(done|blocked|failed)` instead of emitting the stdout sentinel
a gateway story would. This module turns that STRUCTURED report back into the
canonical sentinel text and feeds it to the UNCHANGED `sentinel_parser`
(`handle_phase_task_response`), so the DAG rollup, diff-review trigger, progress
cards, ledger and SHCA forwarding all behave exactly as for a gateway story
(LOCAL_EXECUTION_MODE_PLAN.md §4.3, §14.4 — the 8th, new, governed action).
"""
from __future__ import annotations

from app.database import get_pool
from app.services.sentinel_parser import handle_phase_task_response, parse_sentinel

_KIND = {"done": "DONE", "blocked": "BLOCKED", "failed": "FAILED"}


def _synthesize_sentinel(task_id: str, status: str, tail: str, failure: dict) -> str:
    """Rebuild the exact line-anchored sentinel `parse_sentinel` expects, plus the
    structured FAILURE_* block so `_parse_failure_details` works for test phases."""
    kind = _KIND[status]
    lines = [f"PHASE_TASK_{kind} T{task_id} — {tail}".rstrip()]
    if status == "failed" and failure:
        # order/labels mirror _FAILURE_FIELD_RE / _FAILURE_ERROR_RE in sentinel_parser
        if failure.get("failure_type"):
            lines.append(f"FAILURE_TYPE: {failure['failure_type']}")
        if failure.get("command"):
            lines.append(f"COMMAND: {failure['command']}")
        if failure.get("error"):
            lines.append("ERROR:")
            lines.append(str(failure["error"]))
        if failure.get("criterion"):
            lines.append(f"CRITERION: {failure['criterion']}")
    return "\n".join(lines)


async def report_phase(
    story_id: str, task_id: str, status: str, tail: str,
    failure: dict | None = None, full_text: str = "",
) -> dict:
    """Apply a local phase-status report through the shared sentinel machinery.

    `full_text` is the runner's complete turn output (Claude Code path) — it
    already contains the terminal sentinel, so it is fed verbatim to the parser,
    which both extracts the sentinel AND records the full output as the thread
    progress card (parity with a gateway story). When absent (a structured MCP
    caller — Codex/Cursor), the canonical sentinel is synthesized from status+tail.
    Returns {ok} or {ok:false, reason}. Downstream transitions are CAS-guarded and
    skip already-terminal tasks (idempotent)."""
    if status not in _KIND:
        return {"ok": False, "reason": "bad_status"}
    pool = get_pool()
    task = await pool.fetchrow(
        """SELECT id, story_id, goal_id, phase, parent_task_id, depends_on
             FROM tasks WHERE id = $1 AND story_id = $2""",
        task_id, story_id,
    )
    if not task:
        return {"ok": False, "reason": "task_not_found"}
    text = full_text.strip() if (full_text and parse_sentinel(full_text)) else \
        _synthesize_sentinel(task_id, status, tail or "", failure or {})
    task_row = dict(task)
    await handle_phase_task_response(task_row, text, {"status": "ok"})

    # Tell Slack/Notion the phase finished. AFTER the transition, so the platforms
    # never show a completion the shared machinery rejected. The task row selected
    # above has no `title`, so fetch it for the card — best-effort, since a missing
    # title must not cost the relay.
    title = await pool.fetchval("SELECT title FROM tasks WHERE id = $1", task_id)
    from app.services.local_lifecycle import relay_phase_reported  # noqa: PLC0415
    relay_phase_reported(
        task_row.get("goal_id") or "", {**task_row, "title": title or ""}, status, tail,
    )
    return {"ok": True}
