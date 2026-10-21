"""Composing one stage's workspace: the documents, and the opening message.

SPLIT OUT OF `provisioning.py`, which was doing three jobs at once --
deciding what a reviewer reads, bounding it, and driving the gateway.
This module owns the first two; `provisioning` keeps the third.

`_bound_docs` PAGES, IT DOES NOT CUT. The budget it enforces used to be a
24k tail-truncation, which silently dropped the end of a long document --
so the last stories in a list vanished and the reviewer counted what was
left. Every count a review states now comes from LEDGER.md, which is SQL,
and anything over budget is split across numbered files rather than lost.
"""

from __future__ import annotations

import json
import logging

from app.database import get_pool
from app.services.council import findings, inputs

log = logging.getLogger(__name__)

async def _recent_messages(go_thread_id, checkpoint) -> str:
    if not go_thread_id:
        return "(this goal has no GO thread.)"
    pool = get_pool()
    if checkpoint is not None:
        rows = await pool.fetch(
            "SELECT m.role, m.content, m.created_at, COALESCE(u.display_name, m.sender) AS sender_name "
            "FROM messages m LEFT JOIN users u ON u.uid = m.sender "
            "WHERE m.thread_id = $1 AND m.created_at > $2 ORDER BY m.created_at",
            go_thread_id, checkpoint,
        )
    else:
        rows = await pool.fetch(
            "SELECT m.role, m.content, m.created_at, COALESCE(u.display_name, m.sender) AS sender_name "
            "FROM messages m LEFT JOIN users u ON u.uid = m.sender "
            "WHERE m.thread_id = $1 ORDER BY m.created_at DESC LIMIT 30",
            go_thread_id,
        )
        rows = list(reversed(rows))
    if not rows:
        return "(no messages after the summary checkpoint.)"
    lines = []
    for m in rows:
        ts = m["created_at"].strftime("%Y-%m-%d %H:%M") if m["created_at"] else ""
        lines.append(f"[{ts} {m['sender_name']} ({m['role']})] {(m['content'] or '')[:1500]}")
    return "\n".join(lines)


async def build_inputs(goal_id: str, stage: str, instance_id: str, review_id: str) -> dict[str, str]:
    """The documents one stage opens on.

    EVERY STAGE GETS OPEN_FINDINGS.md, and that is the dedup mechanism
    rather than an extra input: a reviewer handed the open keys can reuse
    one, which is recognition; a reviewer without them has to invent a
    key for something it has never seen named, and two cycles never agree.

    NOTHING HERE READS A WORKSPACE. The version this replaces resolved one
    through `shca_instance_id` and read SCOPE.md and cycle plans out of
    it. Pod-executed stories have no such workspace, so those documents
    came back empty -- silently -- and the reviewer judged against
    nothing.
    """
    pool = get_pool()
    docs: dict[str, str] = {}

    goal = await pool.fetchrow(
        "SELECT id, name, description, objective, deadline, priority, go_thread_id, go_agent_id "
        "FROM goals WHERE id = $1", goal_id,
    )
    if not goal:
        raise RuntimeError(f"goal {goal_id!r} not found")

    docs["docs/OBJECTIVE.md"] = (
        f"# Goal: {goal['name']}\n\n"
        f"**Objective:** {goal['objective'] or '(none set)'}\n\n"
        f"**Description:** {goal['description'] or '(none)'}\n\n"
        f"**Deadline:** {goal['deadline'] or '(none)'}\n"
        f"**Priority:** {goal['priority'] or '(none)'}\n"
    )

    summ = await pool.fetchrow(
        "SELECT summary, checkpoint_at FROM goal_summary WHERE goal_id = $1", goal_id,
    )
    docs["docs/GOAL_SUMMARY.md"] = (
        (summ["summary"] if summ and summ["summary"] else "")
        or "(no goal summary has been generated yet.)"
    )
    checkpoint = summ["checkpoint_at"] if summ and summ["checkpoint_at"] else None

    docs["docs/OPEN_FINDINGS.md"] = findings.render.open_findings_doc(
        await findings.store.open_findings(goal_id))

    docs["docs/REVIEW_CONTEXT.json"] = json.dumps({
        "goal_id": goal_id,
        "agent_id": instance_id,
        "agent_type": stage,
        "review_id": review_id,
    }, indent=2)

    run_id = await pool.fetchval(
        "SELECT run_id FROM council_review_stages WHERE id = $1", review_id)

    if stage == "story":
        docs["docs/LEDGER.md"] = await inputs.story_state.ledger(goal_id)
        docs["docs/STORY_STATE.md"] = await inputs.story_state.story_state(goal_id)

    elif stage == "goal_health":
        # The facts the story stage produced. Health judges them; it does
        # not re-derive them, and a failed story stage arrives as a stated
        # gap rather than as silence.
        docs["docs/FINDINGS_STORY.md"] = await inputs.stage_output.of(run_id, "story")
        docs["docs/PLANS.md"] = await inputs.plans.plans(goal_id)
        docs["docs/RECENT_MESSAGES.md"] = await _recent_messages(
            goal["go_thread_id"], checkpoint)

        srows = await pool.fetch(
            "SELECT id, title, state, description FROM stories WHERE goal_id = $1 ORDER BY created_at",
            goal_id,
        )
        docs["docs/STORIES.md"] = "\n\n".join(
            f"### {s['id']} \u2014 {s['title']}  [{s['state']}]\n"
            f"{(s['description'] or '').strip() or '(no description)'}"
            for s in srows
        ) if srows else "(no stories created yet)"

        rrows = await pool.fetch(
            "SELECT name, kind, uri FROM goal_resources "
            "WHERE goal_id = $1 AND status = 'active' ORDER BY kind, name",
            goal_id,
        )
        docs["docs/GOAL_RESOURCES.md"] = (
            "\n".join(
                f"- {r['name']} \u2014 {r['kind']}"
                + (f" ({r['uri']})" if (r["uri"] or "").strip() else "")
                for r in rrows
            )
            if rrows else "(no resources attached to this goal)"
        )

    elif stage == "progress":
        story_rows = await pool.fetch(
            "SELECT s.id, s.title, s.state, s.description, s.assigned_to, s.depends_on, "
            "s.created_at, s.started_at, s.completed_at, s.updated_at, s.blocked_reason, "
            "COALESCE(NULLIF(trim(u.display_name), ''), NULLIF(s.assigned_to, ''), 'Unassigned') AS assignee "
            "FROM stories s LEFT JOIN users u ON u.uid = s.assigned_to "
            "WHERE s.goal_id = $1 ORDER BY s.created_at",
            goal_id,
        )
        docs["docs/STORIES.md"] = "\n\n".join(
            f"### {s['id']} \u2014 {s['title']}  [{s['state']}]  (assignee: {s['assignee']})\n"
            f"depends on: {', '.join(s['depends_on']) if s['depends_on'] else 'none'}\n"
            f"{(s['description'] or '').strip() or '(no description)'}"
            for s in story_rows
        ) if story_rows else "(no stories created yet)"
        docs["docs/PROGRESS_DELTA.md"] = await inputs.progress_delta.build(
            goal_id, story_rows)

    elif stage == "convener":
        docs["docs/FINDINGS_STORY.md"] = await inputs.stage_output.of(run_id, "story")
        docs["docs/FINDINGS_GOAL_HEALTH.md"] = await inputs.stage_output.of(run_id, "goal_health")
        docs["docs/FINDINGS_PROGRESS.md"] = await inputs.stage_output.of(run_id, "progress")

    return docs

# A COUNCIL RA NOW HAS A MILLION-TOKEN WINDOW, so this budget stopped
# being about capacity and became about not wasting one. 400k characters
# is roughly 100k tokens -- a tenth of the window for a single document,
# which no goal-review input needs and every one of them can have.
#
# The old 24k was the dangerous number, because it CUT from the end: the
# last stories in a long list vanished and the reviewer counted what was
# left. `_split_doc` pages instead, and every count a review states comes
# from LEDGER.md, which is SQL.
_MAX_DOC_CHARS = 400_000


def _split_doc(path: str, content: str, max_chars: int) -> dict[str, str]:
    """Split one oversized doc into continuation files at line boundaries.

    Part 1 keeps the original `path` (so prompts that reference it still resolve);
    overflow goes to `<stem>__2<ext>`, `<stem>__3<ext>`, … Each non-final part
    ends with an explicit `[CONTINUES IN ...]` footer pointing at the next, so the
    reviewer follows the chain. Lossless. A single line longer than the budget is
    hard-split as a last resort.
    """
    if len(content) <= max_chars:
        return {path: content}
    if "." in path.rsplit("/", 1)[-1]:
        stem, _, ext = path.rpartition(".")
        ext = "." + ext
    else:
        stem, ext = path, ""
    budget = max(1000, max_chars - 300)  # reserve room for the footer
    chunks: list[str] = []
    cur = ""
    for ln in content.split("\n"):
        if len(ln) > budget:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(ln), budget):
                chunks.append(ln[i:i + budget])
            continue
        candidate = ln if not cur else cur + "\n" + ln
        if len(candidate) > budget:
            chunks.append(cur)
            cur = ln
        else:
            cur = candidate
    if cur:
        chunks.append(cur)

    out: dict[str, str] = {}
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        fn = path if i == 0 else f"{stem}__{i + 1}{ext}"
        if i + 1 < total:
            nxt = f"{stem}__{i + 2}{ext}"
            chunk += f"\n\n[CONTINUES IN `{nxt}` — read it next to finish this document (part {i + 1}/{total}).]"
        else:
            chunk += f"\n\n[END — part {i + 1}/{total} of `{path}`.]"
        out[fn] = chunk
    return out


def bound_docs(docs: dict[str, str], max_chars: int = _MAX_DOC_CHARS) -> dict[str, str]:
    """Pass every doc through _split_doc so no pushed file exceeds the read limit.

    Applied to the docs/ inputs only — bootstrap files (AGENTS.md/SOUL.md/…) are
    injected into context, not read with the `read` tool, so they are not split.
    """
    out: dict[str, str] = {}
    for path, content in docs.items():
        out.update(_split_doc(path, content or "", max_chars))
    return out


_INPUT_DESCRIPTIONS = {
    "docs/REVIEW_CONTEXT.json": "your run context (goal_id, agent_id, agent_type, review_id) - pass these to submit_review unchanged",
    "docs/OPEN_FINDINGS.md": "findings ALREADY OPEN on this goal, each with its key. REUSE a key when you see the same thing again rather than inventing a new one, and account for every one in your scope under '## Previously open'",
    "docs/OBJECTIVE.md": "the goal's name, objective, description, deadline, priority - the intent",
    "docs/GOAL_SUMMARY.md": "the current structured goal summary (scope / completed / remaining, with story ids)",
    "docs/RECENT_MESSAGES.md": "recent user<->GO messages AFTER the summary checkpoint - the summary may lag these, so use them for the LATEST scope direction",
    "docs/STORIES.md": "the story list; for goal_health each story includes its description (what it proposes to build)",
    "docs/GOAL_RESOURCES.md": "the goal's attached resources as name + type (repo/branch/doc/dataset/...) - what's available to build with",
    "docs/PROGRESS_DELTA.md": "PRE-COMPUTED movement since the last review: window, created/completed lists, the CURRENTLY STUCK (>12h) list (id + assignee NAME + state + hours-stuck + reason), stalled list, the READY TO START list, and moved-this-window set. Authoritative numbers; sub-headers are bold labels, NOT '##' headings",
    "docs/LEDGER.md": "counts by shape/state/verdict, computed in SQL. AUTHORITATIVE - if your reading of the detail disagrees with these numbers, the numbers are right",
    "docs/STORY_STATE.md": "every story and errand: what it was ASKED to do, what it DELIVERED (stories.result_summary - an artifact list plus the executor's completion claim, serialised with its JSON quoting intact), and its review VERDICT. Unfinished and finished-badly work is listed first",
    "docs/PLANS.md": "the decision record, read from APPROVED PLAN PARKS (not goal_decisions, which nothing writes). An approved plan is a decision with an approver's name on it",
    "docs/FINDINGS_STORY.md": "the Story-state reviewer's reconciliation - what was asked against what was delivered",
    "docs/FINDINGS_GOAL_HEALTH.md": "the Goal-Health reviewer's findings for you to synthesize",
    "docs/FINDINGS_PROGRESS.md": "the Progress reviewer's findings",
}


def build_kickoff_message(stage: str, goal_id: str, instance_id: str, review_id: str,
                           docs: dict[str, str]) -> str:
    """The full, self-contained turn prompt: who the agent is, every input file +
    where it is, and exactly how to finish (always via submit_review)."""
    keys = sorted(docs.keys(), key=lambda k: (k != "docs/REVIEW_CONTEXT.json", k))
    file_lines = "\n".join(
        f"  - {k} — {_INPUT_DESCRIPTIONS.get(k, 'input file')}" for k in keys
    )
    research = (
        "\n- Where useful, use web_search / web_fetch to ground \"what could have been "
        "done better\" in real alternatives (research informs suggestions only, not facts "
        "about this goal)."
        if stage == "goal_health" else ""
    )
    return (
        f"You are the '{stage}' reviewer for goal {goal_id}. This is your single review run.\n\n"
        f"Run context (also in docs/REVIEW_CONTEXT.json) — pass ALL FOUR to submit_review "
        f"UNCHANGED:\n"
        f"  goal_id={goal_id}\n  agent_id={instance_id}\n  agent_type={stage}\n  review_id={review_id}\n\n"
        f"Your inputs are already in your workspace as files. Read each one with the `read` tool:\n"
        f"{file_lines}\n\n"
        f"What to do:\n"
        f"- Read every file listed above (start with docs/REVIEW_CONTEXT.json).{research}\n"
        # Phrased to demand the tool call itself rather than "form your review, then
        # call", which invited the model to write the whole review as prose first.
        #
        # This does make the agent skip the prose (verified), but be clear about what
        # it does NOT do: it does NOT fix Gemini's MALFORMED_FUNCTION_CALL. Measured
        # over 5 council runs / 15 stages on 2026-08-16, malformed calls continued at
        # the same ~1-in-3 rate, and the failing turns emitted no prose at all. See
        # research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md — do not re-derive the
        # prose theory, it is falsified. Kept only because it is clearer instruction.
        f"- Do NOT write the review out in your reply. Put the whole review INSIDE the "
        f"submit_review call — your turn should be the tool call itself:\n"
        f"    • success: status=\"success\" with a complete review_details object "
        f"(the exact schema is in your AGENTS.md).\n"
        f"    • if you CANNOT complete (missing critical inputs, unable to judge, or an error): "
        f"you MUST STILL call submit_review with status=\"failed\" and reason=\"<what went wrong>\".\n"
        f"- If submit_review returns ok:false with errors, fix exactly those and call it again.\n\n"
        f"You MUST end your run by calling submit_review (status \"success\" or \"failed\"). "
        f"Never end any other way and never leave the review unreported."
    )
