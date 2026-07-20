"""Provision + run a single council reviewer stage as a transient Pi RA agent.

For one stage we: load the RA template (model, tools_allow, AGENTS.md/SOUL.md),
build the static `docs/` inputs, create a fresh gateway agent, push files, set
model+tools, kick off one run, and clean up. The reviewer ends its run by calling
the submit_review tool, which hits /internal/council/review and marks the
stage 'done'. We then confirm 'done' (else mark 'failed') and delete the instance.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.database import get_pool
from app.openclaw import gateway_admin

log = logging.getLogger(__name__)


# The gateway "agent" RPC returns on ACK (runId), NOT when the turn finishes.
# So we ack the kickoff with a short timeout, then POLL the stage until the
# submit_review endpoint marks it terminal (the RA runs asynchronously).
_AGENT_ACK_TIMEOUT = 60     # wait for the run ack only
_POLL_INTERVAL = 3

# Gemini Flash sometimes ends a turn WITHOUT calling the tool. If the agent
# hasn't submitted within _NUDGE_INTERVAL, prod it to call submit_review and
# wait again — up to _MAX_NUDGES times before giving up. Total budget ≈
# (1 + _MAX_NUDGES) * _NUDGE_INTERVAL.
_NUDGE_INTERVAL = 90
_MAX_NUDGES = 3
_NUDGE_MESSAGE = (
    "You have NOT called submit_review yet, so your review is not recorded and your run "
    "is not finished. Call submit_review NOW — status=\"success\" with your full review "
    "(every required section), or status=\"failed\" with a reason. Do not end your turn "
    "without calling the tool."
)

_TEMPLATE_FOR_STAGE = {
    "goal_health": "ra-goal-health",
    "progress": "ra-progress",
    "convener": "ra-convener",
    "story": "ra-story",
}

# Diff/plan-doc reads are bounded so one runaway plan doc can't blow the budget.
_MAX_WORKSPACE_FILE_CHARS = 24000


def _safe_instance_id(stage: str, run_id: str) -> str:
    base = f"ra-{stage}-{run_id[:8]}".lower()
    return "".join(c if (c.isalnum() or c == "-") else "-" for c in base)


def _as_obj(v):
    """Coerce a jsonb column value (asyncpg returns it as str) to a Python obj."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def _extract_file_content(resp) -> str:
    """agents.files.get response → text content.

    The gateway nests it under resp['file']['content']; a few other shapes are
    tried defensively.
    """
    if not isinstance(resp, dict):
        return ""
    file_obj = resp.get("file")
    if isinstance(file_obj, dict) and isinstance(file_obj.get("content"), str):
        return file_obj["content"]
    for key in ("content", "text", "data", "value"):
        v = resp.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


async def _fetch_template(template_id: str) -> dict:
    """Model + tools_allow + workspace files (AGENTS.md/SOUL.md) from the
    templates registry — same DB-backed pattern as GO/SHCA."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, model, tools_allow FROM templates WHERE id = $1", template_id,
    )
    if not row:
        raise RuntimeError(f"template {template_id!r} not found")
    file_rows = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id = $1", template_id,
    )
    files = {f["filename"]: f["content"] for f in file_rows}
    if "AGENTS.md" not in files:
        raise RuntimeError(f"template {template_id!r} has no AGENTS.md in template_files")
    return {
        "name": row["name"],
        "model": row["model"],
        "tools_allow": list(row["tools_allow"] or []),
        "files": files,
    }


async def _build_inputs(goal_id: str, stage: str, instance_id: str, review_id: str) -> dict[str, str]:
    pool = get_pool()
    docs: dict[str, str] = {}

    # Story reviews are story-scoped, not goal-scoped — build a dedicated bundle
    # (SCOPE + the pre-resolved cycle timeline + tasks + approvals) and return.
    if stage == "story":
        return await _build_story_inputs(goal_id, instance_id, review_id)

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

    stories = await pool.fetch(
        "SELECT id, title, state FROM stories WHERE goal_id = $1 ORDER BY created_at", goal_id,
    )
    docs["docs/STORIES.md"] = (
        "\n".join(f"- {s['id']} | {s['title']} [{s['state']}]" for s in stories)
        if stories else "(no stories created yet)"
    )

    docs["docs/REVIEW_CONTEXT.json"] = json.dumps({
        "goal_id": goal_id,
        "agent_id": instance_id,
        "agent_type": stage,
        "review_id": review_id,
    }, indent=2)

    if stage == "goal_health":
        docs["docs/RECENT_MESSAGES.md"] = await _recent_messages(goal["go_thread_id"], checkpoint)
        mem, logs = await _go_memory(goal["go_agent_id"])
        docs["docs/GO_MEMORY.md"] = mem or "(no GO MEMORY.md available.)"
        docs["docs/GO_MEMORY_LOGS.md"] = logs or "(no GO memory/ daily logs available.)"

        # Enriched story list: each story WITH its description (what it proposes
        # to build) so goal_health can judge whether the set covers the scope —
        # overrides the plain one-liner STORIES.md the other stages get.
        srows = await pool.fetch(
            "SELECT id, title, state, description FROM stories WHERE goal_id = $1 ORDER BY created_at",
            goal_id,
        )
        if srows:
            docs["docs/STORIES.md"] = "\n\n".join(
                f"### {s['id']} — {s['title']}  [{s['state']}]\n"
                f"{(s['description'] or '').strip() or '(no description)'}"
                for s in srows
            )

        # Resources attached to the goal — name + type only (NOT the workspace
        # RESOURCES.md with clone/credential instructions). What's available to
        # build with; a needed type that's absent is a question, not a fact.
        rrows = await pool.fetch(
            "SELECT name, kind, uri FROM goal_resources "
            "WHERE goal_id = $1 AND status = 'active' ORDER BY kind, name",
            goal_id,
        )
        docs["docs/GOAL_RESOURCES.md"] = (
            "\n".join(
                f"- {r['name']} — {r['kind']}"
                + (f" ({r['uri']})" if (r["uri"] or "").strip() else "")
                for r in rrows
            )
            if rrows else "(no resources attached to this goal)"
        )

    if stage == "progress":
        # One stories fetch (with the resolved assignee NAME) shared by all three
        # progress docs, so current state agrees across them.
        story_rows = await pool.fetch(
            "SELECT s.id, s.title, s.state, s.description, s.assigned_to, s.depends_on, "
            "s.created_at, s.started_at, s.completed_at, s.updated_at, s.blocked_reason, "
            "COALESCE(NULLIF(trim(u.display_name), ''), NULLIF(s.assigned_to, ''), 'Unassigned') AS assignee "
            "FROM stories s LEFT JOIN users u ON u.uid = s.assigned_to "
            "WHERE s.goal_id = $1 ORDER BY s.created_at",
            goal_id,
        )
        if story_rows:
            docs["docs/STORIES.md"] = "\n\n".join(
                f"### {s['id']} — {s['title']}  [{s['state']}]  (assignee: {s['assignee']})\n"
                f"depends on: {', '.join(s['depends_on']) if s['depends_on'] else 'none'}\n"
                f"{(s['description'] or '').strip() or '(no description)'}"
                for s in story_rows
            )
        docs["docs/PROGRESS_DELTA.md"] = await _build_progress_delta(goal_id, story_rows)
        docs["docs/STORY_REVIEWS.md"] = await _progress_story_reviews(goal_id, story_rows)

    if stage == "convener":
        gh = await pool.fetchrow(
            "SELECT status, output, error FROM council_review_stages "
            "WHERE goal_id = $1 AND stage = 'goal_health' "
            "AND run_id = (SELECT run_id FROM council_review_stages WHERE id = $2)",
            goal_id, review_id,
        )
        gh_out = _as_obj(gh["output"]) if gh and gh["output"] is not None else None
        if gh and gh["status"] == "done" and isinstance(gh_out, str) and gh_out.strip():
            docs["docs/FINDINGS_GOAL_HEALTH.md"] = gh_out
        else:
            reason = (gh["error"] if gh and gh["error"] else "")
            docs["docs/FINDINGS_GOAL_HEALTH.md"] = (
                f"(Goal-Health review unavailable — status: "
                f"{gh['status'] if gh else 'missing'}. {reason})\n"
            )

        pr = await pool.fetchrow(
            "SELECT status, output, error FROM council_review_stages "
            "WHERE goal_id = $1 AND stage = 'progress' "
            "AND run_id = (SELECT run_id FROM council_review_stages WHERE id = $2)",
            goal_id, review_id,
        )
        pr_out = _as_obj(pr["output"]) if pr and pr["output"] is not None else None
        if pr and pr["status"] == "done" and isinstance(pr_out, str) and pr_out.strip():
            docs["docs/FINDINGS_PROGRESS.md"] = pr_out
        else:
            pr_reason = (pr["error"] if pr and pr["error"] else "")
            docs["docs/FINDINGS_PROGRESS.md"] = (
                f"(Progress review unavailable — status: "
                f"{pr['status'] if pr else 'missing'}. {pr_reason})\n"
            )

        findings_stories = await _story_reviews_for_convener(goal_id)
        # Story stages that FAILED in THIS run otherwise vanish (no row in
        # story_reviews). Surface them so the convener reports the coverage gap
        # instead of silently treating those stories as having no findings.
        failed_story = await pool.fetch(
            "SELECT story_id, reviewed_state, error FROM council_review_stages "
            "WHERE stage = 'story' AND status = 'failed' "
            "AND run_id = (SELECT run_id FROM council_review_stages WHERE id = $1)",
            review_id,
        )
        if failed_story:
            lines = ["", "", "## ⚠ Story reviews that FAILED this run (coverage gaps)", ""]
            for fs in failed_story:
                lines.append(
                    f"- story `{fs['story_id']}` [{fs['reviewed_state'] or '?'}] — "
                    f"{(fs['error'] or 'no reason recorded').strip()}"
                )
            lines.append("")
            lines.append(
                "These stories could NOT be reviewed this run. Call each out as a coverage gap "
                "(cite the story id); do NOT treat a failed review as 'no issues'. Review failures "
                "are expected to happen occasionally and must be reported, not hidden."
            )
            findings_stories = findings_stories + "\n" + "\n".join(lines)
        docs["docs/FINDINGS_STORIES.md"] = findings_stories

    return docs


async def _story_reviews_for_convener(goal_id: str) -> str:
    """The latest FULL review for EVERY story that has been reviewed, so the
    convener synthesizes story-level findings into the goal packet. One block
    per story (latest review only), newest first. Each review is bounded so a
    goal with many stories can't overflow even the convener's window."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT t.story_id, t.reviewed_state, t.reviewed_at, t.review_text, s.title "
        "FROM ("
        "  SELECT DISTINCT ON (story_id) story_id, reviewed_state, reviewed_at, review_text "
        "  FROM story_reviews WHERE goal_id = $1 "
        "  ORDER BY story_id, reviewed_at DESC"
        ") t LEFT JOIN stories s ON s.id = t.story_id "
        "ORDER BY t.reviewed_at DESC",
        goal_id,
    )
    if not rows:
        return ("(No stories have been reviewed yet — no story-level findings to fold in. "
                "Note this as a coverage gap if stories exist but none are reviewed.)")

    parts = [
        f"# Story reviews — latest review per story ({len(rows)} reviewed)",
        "",
        "Each block is the most recent review for that story. Fold their verdicts and "
        "notable issues into your synthesis and Top Issues (attribute by story id).",
        "",
    ]
    for r in rows:
        body = (r["review_text"] or "").strip() or "(empty review)"
        if len(body) > _MAX_WORKSPACE_FILE_CHARS:
            body = body[:_MAX_WORKSPACE_FILE_CHARS] + "\n\n…[truncated]…\n"
        when = r["reviewed_at"].strftime("%Y-%m-%d %H:%M") if r["reviewed_at"] else "?"
        parts.append(
            f"## Story `{r['story_id']}` — {r['title'] or '(untitled)'} "
            f"[reviewed as: {r['reviewed_state']}] @ {when}\n\n{body}\n\n---"
        )
    return "\n".join(parts)


async def _read_workspace_file(instance_id: str, story_id: str, path: str) -> str:
    """Read one file from the story's live SHCA workspace via the gateway.

    `instance_id` is the SHCA instance that owns the workspace
    (stories.shca_instance_id); `path` is story-workspace-root-relative (incl.
    any `repo/` prefix, exactly as stored in plan_doc_path). Returns "" on any
    failure (missing instance, file not found, gateway error) so the bundle
    degrades gracefully — the DB-side plan structure still carries the plan.
    """
    if not instance_id or not path:
        return ""
    try:
        resp = await gateway_admin.call(
            "shca.workspace.readFile",
            {"agentId": instance_id, "storyId": story_id, "path": path},
            timeout=30,
        )
    except Exception as exc:
        log.info("council story: readFile failed (instance=%s path=%s): %s", instance_id, path, exc)
        return ""
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ""
    content = resp.get("content") or ""
    if len(content) > _MAX_WORKSPACE_FILE_CHARS:
        content = content[:_MAX_WORKSPACE_FILE_CHARS] + "\n\n…[truncated]…\n"
    return content


def _scope_path_for(plan_doc_paths: list[str], story_id: str) -> str:
    """Best-effort SCOPE.md path, derived from a cycle plan doc's directory.

    Old convention: repo/docs/plans/<sid>/cycle-NN-*.md  → repo/docs/plans/<sid>/SCOPE.md
    New (059+):     repo/stories/<sid>/plans/plan_*.md    → repo/stories/<sid>/scope.md
    Falls back to the new-convention path when no plan doc is known yet.
    """
    for p in plan_doc_paths:
        if not p:
            continue
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if "/docs/plans/" in p:
            return f"{d}/SCOPE.md"
        if "/plans" in d:                       # repo/stories/<sid>/plans → repo/stories/<sid>/scope.md
            return f"{d.rsplit('/', 1)[0]}/scope.md"
        return f"{d}/SCOPE.md" if d else "SCOPE.md"
    return f"repo/stories/{story_id}/scope.md"


def _cycle_status_label(status: str, superseded_by: str | None, root_state: str | None) -> str:
    """A single human-readable status for a cycle, fusing the plan status, the
    supersession link, and the cycle-root task's terminal state."""
    if superseded_by:
        return f"SUPERSEDED→{superseded_by}"
    s = (status or "").lower()
    rs = (root_state or "").lower()
    if s in ("rejected", "superseded"):
        return s.upper()
    if rs in ("cancelled", "rejected", "failed"):
        return rs.upper()
    if rs == "done":
        return "DONE"
    if s == "approved" and rs in ("in_progress", "blocked", ""):
        return f"APPROVED · {rs or 'no root task'}".rstrip(" ·")
    return (rs or s or "unknown").upper()


# Every docs/ file pushed to a reviewer is passed through _bound_docs(), which
# splits anything over _MAX_DOC_CHARS into continuation files — so no input can
# exceed the RA's `read` limit and abort the review. CYCLES.md additionally
# splits its bulky per-cycle plan prose into a separate docs/cycle_v<n>_plan.md
# (a semantic split that keeps the structured index small and readable).
_MAX_DOC_CHARS = 40000           # per-file read budget; larger files are split
_MAX_RESULT_SUMMARY_CHARS = 600  # per phase subtask, inline in the CYCLES.md index


def _cycle_plan_filename(version) -> str:
    return f"docs/cycle_v{version}_plan.md"


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


def _bound_docs(docs: dict[str, str], max_chars: int = _MAX_DOC_CHARS) -> dict[str, str]:
    """Pass every doc through _split_doc so no pushed file exceeds the read limit.

    Applied to the docs/ inputs only — bootstrap files (AGENTS.md/SOUL.md/…) are
    injected into context, not read with the `read` tool, so they are not split.
    """
    out: dict[str, str] = {}
    for path, content in docs.items():
        out.update(_split_doc(path, content or "", max_chars))
    return out


async def _build_cycles_md(goal_id: str, story_id: str, instance_id: str) -> dict[str, str]:
    """The backend-resolved, chronological, fully-annotated cycle timeline.

    Returns a dict of workspace files: the `docs/CYCLES.md` structured index PLUS
    one `docs/cycle_v<n>_plan.md` per cycle holding that cycle's full plan prose.
    The index references each plan file rather than inlining it, so no single file
    exceeds the RA's read limit and no plan content is lost.

    All correlation happens HERE (deterministic), so the RA only judges:
      - order plan_versions by version (each = one cycle/DAG)
      - resolve parent_plan_id into supersedes / superseded-by, and pull the
        SUPERSEDING cycle's `reason` to explain why a cycle was abandoned
      - map each cycle-root task (+ its phase subtasks) by shared plan_doc_path
      - attach diff/plan approvals to the cycle whose root execution window
        brackets the approval
      - each cycle's plan doc prose is written to its own docs/cycle_v<n>_plan.md
    """
    pool = get_pool()
    plans = await pool.fetch(
        "SELECT id, version, status, parent_plan_id, plan_doc_path, task_dag, "
        "risk_level, reason, proposed_by, approved_by, approved_at, created_at "
        "FROM plan_versions WHERE story_id = $1 ORDER BY version",
        story_id,
    )
    tasks = await pool.fetch(
        "SELECT id, parent_task_id, title, description, state, executor, "
        "acceptance_criteria, result_summary, started_at, completed_at, created_at "
        "FROM tasks WHERE story_id = $1 AND status = 'active' ORDER BY created_at",
        story_id,
    )
    approvals = await pool.fetch(
        "SELECT kind, decision, feedback, proposed_at, decided_at, decided_by, escalation_count "
        "FROM approvals WHERE story_id = $1 ORDER BY proposed_at",
        story_id,
    )

    roots = [t for t in tasks if not (t["parent_task_id"] or "").strip()]
    subs_by_root: dict[str, list] = {}
    for t in tasks:
        parent = (t["parent_task_id"] or "").strip()
        if parent:
            subs_by_root.setdefault(parent, []).append(t)

    # supersession: plan.id -> superseding plan.version
    superseded_by: dict[str, str] = {}
    by_id = {p["id"]: p for p in plans}
    for p in plans:
        parent = p["parent_plan_id"]
        if parent and parent in by_id:
            superseded_by[parent] = f"v{p['version']}"
    # the superseding plan's reason explains why the parent was abandoned
    abandon_reason: dict[str, str] = {}
    for p in plans:
        parent = p["parent_plan_id"]
        if parent and parent in by_id and (p["reason"] or "").strip():
            abandon_reason[parent] = p["reason"].strip()

    def _root_for(plan) -> dict | None:
        pdp = plan["plan_doc_path"] or ""
        if pdp:
            for r in roots:
                blob = f"{r['title'] or ''}\n{r['description'] or ''}"
                if pdp in blob:
                    return r
        tag = f"· v{plan['version']}"          # fallback: match version tag in title
        for r in roots:
            if tag in (r["title"] or ""):
                return r
        return None

    def _approvals_for(root) -> list:
        if not root:
            return []
        start = root["started_at"] or root["created_at"]
        end = root["completed_at"]
        out = []
        for a in approvals:
            when = a["proposed_at"] or a["decided_at"]
            if when is None:
                continue
            if start and when < start:
                continue
            if end and when > end:
                continue
            out.append(a)
        return out

    # Execution timeline header
    timeline_bits = []
    for p in plans:
        lbl = _cycle_status_label(p["status"], superseded_by.get(p["id"]),
                                  (_root_for(p) or {}).get("state"))
        timeline_bits.append(f"v{p['version']} [{lbl}]")
    timeline = " → ".join(timeline_bits) if timeline_bits else "(no approved plans/DAGs on record)"

    out: list[str] = [
        "# Story Execution — Cycles (chronological, fully resolved)",
        "",
        "## Execution Timeline",
        f"{timeline}   ({len(plans)} cycle{'s' if len(plans) != 1 else ''})",
        "",
        "Each cycle below is one task DAG. Status, supersession, and reasons are "
        "pre-resolved — trust them; judge whether the decisions were sound.",
        "Each cycle's FULL plan prose is in its own file `docs/cycle_v<n>_plan.md` "
        "(referenced under that cycle). Open it with `read` when you need the plan "
        "detail to judge that cycle's decisions.",
        "",
    ]

    plan_files: dict[str, str] = {}

    if not plans:
        # No plan_versions rows — surface the raw root tasks so the reviewer
        # still sees what executed.
        out.append("_No plan_versions rows for this story. Raw cycle-root tasks:_")
        for r in roots:
            out.append(f"- {r['id']} — {r['title']} [{r['state']}]")
        return {"docs/CYCLES.md": "\n".join(out)}

    plan_doc_paths = [p["plan_doc_path"] for p in plans if p["plan_doc_path"]]
    for p in plans:
        root = _root_for(p)
        lbl = _cycle_status_label(p["status"], superseded_by.get(p["id"]),
                                  (root or {}).get("state"))
        out.append(f"## Cycle v{p['version']} · {p['plan_doc_path'] or '(no doc path)'} · {lbl}")
        out.append(f"- Plan status: {p['status'] or '(unset)'} · risk: {p['risk_level'] or '(unset)'}")
        if p["parent_plan_id"] and p["parent_plan_id"] in by_id:
            out.append(f"- Supersedes: v{by_id[p['parent_plan_id']]['version']}")
        if superseded_by.get(p["id"]):
            out.append(f"- Superseded by: {superseded_by[p['id']]}")
        out.append(f"- Created because: {(p['reason'] or '(initial plan / no reason recorded)').strip()}")
        if superseded_by.get(p["id"]):
            out.append(f"- Abandoned because: {abandon_reason.get(p['id'], '(the superseding cycle gave no reason)')}")
        appr = "never approved" if not p["approved_by"] else f"{p['approved_by']} @ {p['approved_at']}"
        out.append(f"- Plan approval: {appr}")
        diff_appr = _approvals_for(root)
        if diff_appr:
            for a in diff_appr:
                fb = f" — \"{a['feedback'][:200]}\"" if (a["feedback"] or "").strip() else ""
                out.append(f"- {a['kind']} approval: {a['decision'] or '(pending)'} "
                           f"by {a['decided_by'] or '(unknown)'} @ {a['decided_at'] or '(undecided)'}{fb}")
        else:
            out.append("- Diff approval: (none recorded for this cycle)")

        # Phase subtasks, labelled by their plan task_dag node.
        dag = _as_obj(p["task_dag"]) or {}
        dag_nodes = {n.get("id"): n for n in (dag.get("tasks") or []) if isinstance(n, dict)}
        if root:
            out.append(f"- Root task {root['id']}: [{root['state']}]")
            subs = sorted(subs_by_root.get(root["id"], []), key=lambda s: s["id"])
            if subs:
                out.append("- Phase outcomes:")
                for s in subs:
                    suffix = s["id"].rsplit("-", 1)[-1]        # …-T3 → T3
                    node = dag_nodes.get(suffix) or {}
                    phase = node.get("phase") or s["executor"] or "?"
                    summ = (s["result_summary"] or "").strip() or "(no result summary)"
                    if len(summ) > _MAX_RESULT_SUMMARY_CHARS:
                        summ = summ[:_MAX_RESULT_SUMMARY_CHARS] + " […]"
                    out.append(f"    {suffix} {phase} [{s['state']}] — {summ}")
            else:
                out.append("- Phase outcomes: (no subtasks recorded)")
        else:
            out.append("- Root task: (no matching cycle-root task found)")

        prose = (await _read_workspace_file(instance_id, story_id, p["plan_doc_path"] or "")).strip()
        if prose:
            # Full prose, untruncated — _bound_docs() splits it into continuation
            # files if a single plan doc is itself larger than the read budget.
            plan_path = _cycle_plan_filename(p["version"])
            plan_files[plan_path] = (
                f"# Cycle v{p['version']} — plan prose\n"
                f"# Source: {p['plan_doc_path'] or '(no doc path)'}\n\n"
                f"{prose}\n"
            )
            out.append(f"- Plan (prose): read `{plan_path}` for this cycle's full plan")
        else:
            out.append("- Plan (prose): (plan document unavailable from the workspace)")
        out.append("")

    # The structured index is small; if it ever isn't, _bound_docs() splits it too.
    return {"docs/CYCLES.md": "\n".join(out), **plan_files}


async def _build_story_inputs(goal_id: str, instance_id: str, review_id: str) -> dict[str, str]:
    """Assemble the story reviewer's docs/ bundle (see ra-story AGENTS.md)."""
    pool = get_pool()
    docs: dict[str, str] = {}

    stage = await pool.fetchrow(
        "SELECT story_id, reviewed_state FROM council_review_stages WHERE id = $1", review_id,
    )
    story_id = (stage and stage["story_id"]) or ""
    reviewed_state = (stage and stage["reviewed_state"]) or ""
    if not story_id:
        raise RuntimeError(f"story stage {review_id!r} has no story_id")

    story = await pool.fetchrow(
        "SELECT id, goal_id, title, description, acceptance_criteria, state, current_phase, "
        "priority, assigned_to, blocked_reason, shca_instance_id, current_plan_path, "
        "created_at, started_at, completed_at, updated_at "
        "FROM stories WHERE id = $1", story_id,
    )
    if not story:
        raise RuntimeError(f"story {story_id!r} not found")
    shca_instance_id = story["shca_instance_id"] or ""

    docs["docs/REVIEW_CONTEXT.json"] = json.dumps({
        "goal_id": goal_id,
        "agent_id": instance_id,
        "agent_type": "story",
        "review_id": review_id,
        "story_id": story_id,
        "reviewed_state": reviewed_state or story["state"],
    }, indent=2)

    docs["docs/STORY_SPEC.md"] = (
        f"# Story {story['id']} — {story['title']}\n\n"
        f"**State:** {story['state']}  ·  **Phase:** {story['current_phase'] or '(none)'}  ·  "
        f"**Priority:** {story['priority'] or '(none)'}  ·  **Assigned to:** {story['assigned_to'] or '(none)'}\n\n"
        f"**Reviewing state:** {reviewed_state or story['state']} "
        f"({'FINAL quality review' if (reviewed_state or story['state']) == 'done' else 'INTERIM check'})\n\n"
        f"## Description\n{story['description'] or '(none)'}\n\n"
        f"## Acceptance criteria\n{story['acceptance_criteria'] or '(none specified)'}\n\n"
        f"## Timeline\ncreated {story['created_at']} · started {story['started_at']} · "
        f"completed {story['completed_at']} · updated {story['updated_at']}\n"
        + (f"\n**Blocked reason:** {story['blocked_reason']}\n" if (story['blocked_reason'] or '').strip() else "")
    )

    summ = await pool.fetchrow(
        "SELECT summary FROM goal_summary WHERE goal_id = $1", goal_id,
    )
    docs["docs/GOAL_SUMMARY.md"] = (
        (summ["summary"] if summ and summ["summary"] else "")
        or "(no goal summary has been generated yet.)"
    )

    # CYCLES.md (the structured index) + one docs/cycle_v<n>_plan.md per cycle.
    docs.update(await _build_cycles_md(goal_id, story_id, shca_instance_id))

    # SCOPE.md — derived from a cycle plan doc's directory; read from the live ws.
    plan_doc_paths = [r["plan_doc_path"] for r in await pool.fetch(
        "SELECT plan_doc_path FROM plan_versions WHERE story_id = $1 ORDER BY version", story_id,
    )]
    if story["current_plan_path"]:
        plan_doc_paths.insert(0, story["current_plan_path"])
    scope_path = _scope_path_for(plan_doc_paths, story_id)
    scope = await _read_workspace_file(shca_instance_id, story_id, scope_path)
    docs["docs/SCOPE.md"] = scope.strip() if scope.strip() else (
        f"(SCOPE file unavailable from the workspace at {scope_path!r} — "
        "judge against docs/STORY_SPEC.md instead.)"
    )

    # PRIOR_REVIEWS.md — earlier reviews of THIS story, excluding the state we're
    # reviewing now. Blank is normal (first review) — the AGENTS.md says so.
    priors = await pool.fetch(
        "SELECT reviewed_state, reviewer_agent_id, reviewed_at, review_text "
        "FROM story_reviews WHERE story_id = $1 AND reviewed_state <> $2 "
        "ORDER BY reviewed_at DESC",
        story_id, reviewed_state or story["state"],
    )
    if priors:
        chunks = [
            f"## Prior review — state '{r['reviewed_state']}' @ {r['reviewed_at']} "
            f"(by {r['reviewer_agent_id'] or 'unknown'})\n\n{r['review_text'] or '(empty)'}"
            for r in priors
        ]
        docs["docs/PRIOR_REVIEWS.md"] = "\n\n---\n\n".join(chunks)
    else:
        docs["docs/PRIOR_REVIEWS.md"] = (
            "(No prior reviews of this story — this is the first review. This is normal; "
            "do not treat it as a finding.)"
        )

    return docs


_STATE_RANK = {"proposed": 0, "accepted": 1, "in_progress": 2, "done": 3, "reviewed": 4}
_STUCK_HOURS = 12
_STALLED_DAYS = 3
# A dependency is "complete" (no longer blocks a dependent from starting) in any
# of these states — matches the start-gate in story_transitions.
_DEP_SATISFIED = {"done", "reviewed", "cancelled", "rejected"}


def _aware(ts):
    """Normalize a possibly-naive timestamp to UTC-aware (asyncpg returns aware,
    but guard so an aware/naive subtraction can never TypeError)."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _hours_between(later, earlier) -> int:
    a, b = _aware(later), _aware(earlier)
    if a is None or b is None:
        return 0
    return int((a - b).total_seconds() // 3600)


def _delta_label(reviewed_state: str, current_state: str) -> str:
    """Pre-computed reconciliation: did the story move since its review was done.
    moved-forward / unchanged / regressed / sideways."""
    rs, cs = (reviewed_state or "").lower(), (current_state or "").lower()
    if cs in ("blocked", "rejected", "cancelled"):
        return "unchanged" if rs == cs else "regressed"
    if rs in ("blocked", "rejected", "cancelled"):
        return "moved-forward" if cs in _STATE_RANK else "sideways"
    r, c = _STATE_RANK.get(rs), _STATE_RANK.get(cs)
    if r is None or c is None:
        return "sideways"
    return "moved-forward" if c > r else ("unchanged" if c == r else "regressed")


async def _build_progress_delta(goal_id: str, story_rows) -> str:
    """Pre-computed movement since the last completed council run. ALL numbers,
    durations, >12h stuck flags, and assignee NAMES are computed HERE — the
    Progress RA interprets, never recounts. Sub-headers are bold LABELS, never
    '##' headings, because Flash is primed to copy '##' lines as its own output
    headings and would fail section validation. `story_rows` is fetched once by
    the caller (with the resolved `assignee` name) and shared with STORIES.md /
    STORY_REVIEWS.md so current state agrees across all three docs."""
    pool = get_pool()
    # Anchor the window to the last time the PROGRESS stage itself completed for
    # THIS goal — NOT the last completed run. Story-review runs (kind='story')
    # have no progress stage, and a goal run can fail at the convener after
    # progress succeeded; keying on the progress STAGE (status='done') avoids
    # both, so the window is exactly "since the last progress review of this goal".
    # The currently-running progress stage is 'running', so it is excluded.
    window_start = await pool.fetchval(
        "SELECT finished_at FROM council_review_stages "
        "WHERE goal_id = $1 AND stage = 'progress' AND status = 'done' AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1",
        goal_id,
    )
    first_review = window_start is None
    if first_review:
        window_start = await pool.fetchval("SELECT created_at FROM goals WHERE id = $1", goal_id)

    # When each story ENTERED its current state (the reliable 'stuck since' clock)
    # + which stories changed state in-window — from the ledger, one query.
    current_state_by_id = {s["id"]: s["state"] for s in story_rows}
    levents = await pool.fetch(
        "SELECT payload->>'story_id' AS sid, payload->>'to_state' AS to_state, timestamp AS ts "
        "FROM ledger_events WHERE event_type = 'story.state_changed' AND context_goal_id = $1 "
        "ORDER BY timestamp",
        goal_id,
    )
    entered_current: dict = {}
    moved_in_window: set = set()
    for e in levents:
        sid = e["sid"]
        if not sid:
            continue
        if current_state_by_id.get(sid) == e["to_state"]:
            entered_current[sid] = e["ts"]   # ascending → keeps the latest entry into current state
        if window_start and e["ts"] and _aware(e["ts"]) > _aware(window_start):
            moved_in_window.add(sid)

    now = datetime.now(timezone.utc)
    created, completed, stuck, stalled, ready = [], [], [], [], []
    for s in story_rows:
        # READY TO START: created/accepted, not yet started, every dependency complete.
        if s["state"] in ("proposed", "accepted"):
            deps = list(s["depends_on"] or [])
            if all(current_state_by_id.get(d) in _DEP_SATISFIED for d in deps):
                ready.append((s, deps))
        if window_start and s["created_at"] and _aware(s["created_at"]) > _aware(window_start):
            created.append(s)
        if s["state"] in ("done", "reviewed") and s["completed_at"] and (
            not window_start or _aware(s["completed_at"]) > _aware(window_start)
        ):
            completed.append(s)
        if s["state"] in ("in_progress", "blocked"):
            entry = entered_current.get(s["id"])
            approx = entry is None
            if entry is None:   # pre-ledger fallback: started_at (in_progress) / updated_at (blocked)
                entry = s["started_at"] if s["state"] == "in_progress" else s["updated_at"]
            hrs = _hours_between(now, entry) if entry else 0
            if hrs > _STUCK_HOURS:
                stuck.append((s, hrs, approx))
        if s["state"] == "in_progress" and s["updated_at"]:
            if _hours_between(now, s["updated_at"]) // 24 >= _STALLED_DAYS:
                stalled.append((s, _hours_between(now, s["updated_at"]) // 24))
    stuck.sort(key=lambda x: x[1], reverse=True)

    win = ("goal inception → now (FIRST review — Created/Completed are cumulative, not a delta)"
           if first_review else f"{window_start} → now")
    L = [
        "# Progress delta — PRE-COMPUTED. Trust these numbers; never recount or recompute a duration.",
        "# NOTE: the sub-headers below are LABELS, not '## ' headings — do not copy them as your output sections.",
        f"WINDOW: {win}   (UTC now: {now.strftime('%Y-%m-%d %H:%M')})",
        "",
        f"CREATED IN WINDOW ({len(created)}):",
        *([f"  - {s['id']} — {s['title']} (assignee: {s['assignee']}, created {s['created_at']})"
           for s in created] or ["  - none"]),
        "",
        f"COMPLETED IN WINDOW ({len(completed)}):",
        *([f"  - {s['id']} — {s['title']} (assignee: {s['assignee']}, completed {s['completed_at']})"
           for s in completed] or ["  - none"]),
        "",
        f"CURRENTLY STUCK (>{_STUCK_HOURS}h) ({len(stuck)}):  [in_progress or blocked > {_STUCK_HOURS}h]",
        *([f"  - {hrs}h{' (approx)' if approx else ''} — {s['id']} — {s['title']} — assignee: {s['assignee']} "
           f"— {s['state']} — {(s['blocked_reason'] or 'no reason given') if s['state'] == 'blocked' else 'in_progress, not completed'}"
           for (s, hrs, approx) in stuck] or ["  - none"]),
        "",
        f"STALLED IN_PROGRESS (no row update >= {_STALLED_DAYS} days) ({len(stalled)}):",
        *([f"  - {s['id']} — {s['title']} — assignee: {s['assignee']} — no update for {d} days"
           for (s, d) in stalled] or ["  - none"]),
        "",
        f"READY TO START (created/accepted, not started, all dependencies complete) ({len(ready)}):",
        *([f"  - {s['id']} — {s['title']} — assignee: {s['assignee']} — state {s['state']} — "
           f"deps complete: {', '.join(deps) if deps else 'none'}"
           for (s, deps) in ready] or ["  - none"]),
        "",
        "MOVED THIS WINDOW (story ids with a state change since window start): "
        + (", ".join(sorted(moved_in_window)) or "none"),
    ]
    return "\n".join(L)


async def _progress_story_reviews(goal_id: str, story_rows) -> str:
    """Latest review per reviewed story, shown next to the story's CURRENT state
    with a PRE-COMPUTED reconciliation DELTA label. Story RAs ran earlier in the
    run, so a review may pre-date the current state — reviewed_at + current state
    are shown side by side. Headers use '###' to avoid colliding with the
    Progress RA's '##' output headings."""
    pool = get_pool()
    meta = {s["id"]: (s["title"], s["state"]) for s in story_rows}
    rows = await pool.fetch(
        "SELECT DISTINCT ON (story_id) story_id, reviewed_state, reviewed_at, review_text "
        "FROM story_reviews WHERE goal_id = $1 ORDER BY story_id, reviewed_at DESC",
        goal_id,
    )
    if not rows:
        return "(no stories reviewed yet)"
    rows = sorted(rows, key=lambda r: r["reviewed_at"], reverse=True)
    parts = []
    for r in rows:
        title, current = meta.get(r["story_id"], ("(unknown)", "(unknown)"))
        when = r["reviewed_at"].strftime("%Y-%m-%d %H:%M") if r["reviewed_at"] else "?"
        body = (r["review_text"] or "").strip() or "(empty review)"
        if len(body) > _MAX_WORKSPACE_FILE_CHARS:
            body = body[:_MAX_WORKSPACE_FILE_CHARS] + "\n\n…[truncated]…\n"
        parts.append(
            f"### Story {r['story_id']} — {title} | reviewed as {r['reviewed_state']} @ {when} "
            f"| CURRENT state now: {current} | DELTA: {_delta_label(r['reviewed_state'], current)}\n\n"
            f"{body}\n\n---"
        )
    return "\n".join(parts)


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


async def _go_memory(go_agent_id) -> tuple[str, str]:
    """Best-effort read of GO's MEMORY.md + ALL its dated memory/ logs, in order.

    MEMORY.md comes via agents.files.get (allowlisted). The daily logs live in the
    GO workspace's memory/ folder, which agents.files.get rejects and
    agents.files.list ignores — so we traverse them with the agents.memoryLogs
    RPC, which returns memory/*.md in chronological order (newest kept if capped).
    """
    if not go_agent_id:
        return "", ""
    mem = ""
    try:
        mem = _extract_file_content(await gateway_admin.get_agent_file(go_agent_id, "MEMORY.md"))
    except Exception as exc:
        log.info("council: GO MEMORY.md unavailable (%s): %s", go_agent_id, exc)

    logs = ""
    try:
        resp = await gateway_admin.call("agents.memoryLogs", {"agentId": go_agent_id}, timeout=30)
        files = (resp or {}).get("files") or []
        chunks = [
            f"## {f.get('name')}\n{f.get('content') or ''}"
            for f in files if (f.get("content") or "").strip()
        ]
        logs = "\n\n".join(chunks)
        if logs and (resp or {}).get("truncated"):
            logs = "(older logs truncated to fit; newest shown)\n\n" + logs
    except Exception as exc:
        log.info("council: GO memory logs unavailable (%s): %s", go_agent_id, exc)
    return mem, logs


async def _fail_stage(stage_id: str, error: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE council_review_stages SET status='failed', error=$1, finished_at=NOW() "
        "WHERE id=$2 AND status <> 'done'",
        error[:500], stage_id,
    )
    log.warning("council: stage %s failed: %s", stage_id, error)


async def _wait_for_stage_terminal(stage_id: str, timeout_s: int) -> bool:
    """Poll until the stage reaches a terminal state (done OR failed), or timeout.

    Returns True if it became terminal within the window, else False (so the
    caller can nudge the agent). The endpoint sets the status when the RA calls
    submit_review (success -> done, failed -> failed).
    """
    pool = get_pool()
    waited = 0
    while waited < timeout_s:
        row = await pool.fetchrow(
            "SELECT status FROM council_review_stages WHERE id = $1", stage_id,
        )
        if row and row["status"] in ("done", "failed"):
            return True
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    return False


_INPUT_DESCRIPTIONS = {
    "docs/REVIEW_CONTEXT.json": "your run context (goal_id, agent_id, agent_type, review_id) — pass these to submit_review unchanged",
    "docs/OBJECTIVE.md": "the goal's name, objective, description, deadline, priority — the intent",
    "docs/GOAL_SUMMARY.md": "the current structured goal summary (scope / completed / remaining, with story ids) — your PRIMARY judgment target",
    "docs/RECENT_MESSAGES.md": "recent user<->GO messages AFTER the summary checkpoint — the summary may lag these, so use them for the LATEST scope direction",
    "docs/STORIES.md": "the story list; for goal_health each story includes its description (what it proposes to build)",
    "docs/GOAL_RESOURCES.md": "the goal's attached resources as name + type (repo/branch/doc/dataset/...) — what's available to build with",
    "docs/PROGRESS_DELTA.md": "PRE-COMPUTED movement since the last review: window, created/completed lists, the CURRENTLY STUCK (>12h) list (id + assignee NAME + state + hours-stuck + reason), stalled list, the READY TO START list (created/accepted + all deps complete + not started), and moved-this-window set. Authoritative numbers; sub-headers are bold labels, NOT '##' headings",
    "docs/STORY_REVIEWS.md": "latest review per reviewed story: reviewed_state + reviewed_at shown next to the CURRENT state with a pre-computed moved/unchanged/regressed DELTA label — reconcile by time, the review may pre-date the current state (blank line = none reviewed yet, not an error)",
    "docs/GO_MEMORY.md": "the orchestrator's durable memory (may be a placeholder)",
    "docs/GO_MEMORY_LOGS.md": "the orchestrator's recent dated logs (may be a placeholder)",
    "docs/FINDINGS_GOAL_HEALTH.md": "the Goal-Health reviewer's findings for you to synthesize",
    "docs/FINDINGS_PROGRESS.md": "the Progress reviewer's findings (may be absent in Phase 1)",
    "docs/FINDINGS_STORIES.md": "the latest FULL review for every reviewed story — story-level findings you MUST fold into the packet (empty only if no stories reviewed yet)",
    "docs/STORY_SPEC.md": "what the story asked for (title, description, acceptance criteria, state, timeline)",
    "docs/SCOPE.md": "the living human-authored story scope — the primary yardstick (may be unavailable)",
    "docs/CYCLES.md": "the PRE-RESOLVED execution timeline: every cycle (DAG) in order, with status, supersession, reasons, tasks+outcomes, approvals, and plan prose — trust its structure, judge the substance",
    "docs/PRIOR_REVIEWS.md": "earlier reviews of this story (BLANK is normal = first review; not an error)",
}


def _build_kickoff_message(stage: str, goal_id: str, instance_id: str, review_id: str,
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
        f"- Form your review, then FINISH by calling the submit_review tool:\n"
        f"    • success: status=\"success\" with a complete review_details object "
        f"(the exact schema is in your AGENTS.md).\n"
        f"    • if you CANNOT complete (missing critical inputs, unable to judge, or an error): "
        f"you MUST STILL call submit_review with status=\"failed\" and reason=\"<what went wrong>\".\n"
        f"- If submit_review returns ok:false with errors, fix exactly those and call it again.\n\n"
        f"You MUST end your run by calling submit_review (status \"success\" or \"failed\"). "
        f"Never end any other way and never leave the review unreported."
    )


async def run_stage(run_id: str, stage_id: str, stage: str, goal_id: str) -> bool:
    """Provision + run one reviewer stage. Returns True if it ended 'done'."""
    pool = get_pool()
    template_id = _TEMPLATE_FOR_STAGE.get(stage)
    if not template_id:
        await _fail_stage(stage_id, f"no template mapped for stage {stage!r}")
        return False

    try:
        tmpl = await _fetch_template(template_id)
    except Exception as exc:
        await _fail_stage(stage_id, f"template load failed: {exc}")
        return False

    instance_id = _safe_instance_id(stage, run_id)
    workspace = f"~/.openclaw/workspace-{instance_id}"  # match GO's workspace path form

    try:
        docs = await _build_inputs(goal_id, stage, instance_id, stage_id)
    except Exception as exc:
        await _fail_stage(stage_id, f"input build failed: {exc}")
        return False

    await pool.execute(
        "UPDATE council_review_stages SET status='running', instance_id=$1, "
        "started_at=NOW(), claimed_at=NOW() WHERE id=$2",
        instance_id, stage_id,
    )

    created = False
    try:
        from app.openclaw import GatewayError
        # Create the gateway agent with model + tools AT CREATION — same call
        # shape GO uses (app/services/go_agent.py). One round-trip, tools applied
        # before the run instead of via a follow-up update.
        try:
            await gateway_admin.call("agents.create", {
                "name": instance_id,
                "workspace": workspace,
                "model": tmpl["model"],
                "tools": {"allow": tmpl["tools_allow"]},
            })
            created = True
        except GatewayError as exc:
            if "already exists" in str(exc):
                created = True  # stale instance from a prior run — reuse + overwrite files
            else:
                raise

        # IDENTITY.md is just the reviewer's template name.
        identity_md = f"{tmpl['name']}\n"
        # Push bootstrap files (template AGENTS.md/SOUL.md + IDENTITY/USER) and the
        # docs/ inputs — the full workspace provisioning, like GO.
        bootstrap = {
            **tmpl["files"],           # AGENTS.md, SOUL.md (from template_files)
            "IDENTITY.md": identity_md,
            "USER.md": "",
        }
        # Bound every docs/ input so none exceeds the RA's read limit (oversized
        # files split into `…__2`, `…__3` continuation files). Bootstrap files are
        # injected into context — not read with the `read` tool — so they are NOT
        # split (splitting AGENTS.md/SOUL.md would corrupt the agent's prompt).
        for fn, content in {**bootstrap, **_bound_docs(docs)}.items():
            await gateway_admin.set_agent_file(instance_id, fn, content or "")

        session_key = f"agent:{instance_id}:{goal_id.lower()}-{stage_id}"
        kickoff = _build_kickoff_message(stage, goal_id, instance_id, stage_id, docs)
        # Kill switch — re-check just before the agent kickoff in case the goal
        # was paused between stage provisioning and actual dispatch (multi-stage
        # runs can span seconds to minutes).
        from app.services import execution_control as _ec  # noqa: PLC0415
        _blk = await _ec.dispatch_block(goal_id)
        if _blk:
            log.info(
                "council: stage kickoff blocked (scope=%s reason=%s) — stage=%s run=%s goal=%s",
                _blk.scope, _blk.reason or "<none>", stage, stage_id, goal_id,
            )
            await _fail_stage(stage_id, f"execution paused ({_blk.scope}) — stage skipped")
            return False
        # The "agent" RPC returns on ACK (runId), not when the turn finishes.
        # Ack the kickoff, then wait until the RA runs + calls submit_review
        # (success -> 'done', failed -> 'failed'; the endpoint sets the status).
        await gateway_admin.call(
            "agent",
            {
                "agentId": instance_id,
                "sessionKey": session_key,
                "message": kickoff,
                "idempotencyKey": f"council-{stage_id}",
            },
            timeout=_AGENT_ACK_TIMEOUT,
        )
        # Wait for the submit; if the agent stalls without calling the tool,
        # nudge it (and wait again) up to _MAX_NUDGES times.
        terminal = await _wait_for_stage_terminal(stage_id, _NUDGE_INTERVAL)
        nudges = 0
        while not terminal and nudges < _MAX_NUDGES:
            nudges += 1
            log.info("council: nudging %s — no submit yet (nudge %d/%d)",
                     instance_id, nudges, _MAX_NUDGES)
            try:
                await gateway_admin.call(
                    "agent",
                    {
                        "agentId": instance_id,
                        "sessionKey": session_key,
                        "message": _NUDGE_MESSAGE,
                        "idempotencyKey": f"council-{stage_id}-nudge{nudges}",
                    },
                    timeout=_AGENT_ACK_TIMEOUT,
                )
            except Exception as exc:
                log.warning("council: nudge dispatch failed for %s: %s", instance_id, exc)
            terminal = await _wait_for_stage_terminal(stage_id, _NUDGE_INTERVAL)
    except Exception as exc:
        log.warning("council: stage run errored goal=%s stage=%s: %s", goal_id, stage, exc)
    finally:
        row = await pool.fetchrow("SELECT status FROM council_review_stages WHERE id=$1", stage_id)
        status = row["status"] if row else None
        done = status == "done"
        if status not in ("done", "failed"):
            # Never reported (crash/timeout). An agent-reported 'failed' keeps its
            # own reason; only the truly-silent case gets this generic error.
            await _fail_stage(stage_id, "agent run ended without calling submit_review")
            status = "failed"
        # The reviewer's job is over the moment the stage is terminal. The submit
        # endpoint persists the review into council_review_stages.output BEFORE it
        # sets the terminal status we read above, so deleting the gateway agent
        # here never loses the review (delivery reads it from Postgres, not the
        # workspace). Delete the agent AND its workspace so reviewers don't
        # accumulate on the gateway. Best-effort: a failed delete only leaks a
        # workspace; it never affects the stored review or delivery.
        if created:
            from app.config import get_settings as _get_settings
            if getattr(_get_settings(), "council_keep_ra_workspaces", False):
                log.warning(
                    "council: KEEPING reviewer agent %s + workspace (stage %s) — "
                    "COUNCIL_KEEP_RA_WORKSPACES is set (debug). Reviewers will "
                    "accumulate on the gateway until cleared.",
                    instance_id, status,
                )
            else:
                try:
                    await gateway_admin.delete_agent(instance_id, delete_files=True)
                    log.info("council: deleted reviewer agent %s + workspace (stage %s)",
                             instance_id, status)
                except Exception as exc:
                    log.info("council: cleanup delete failed for %s: %s", instance_id, exc)

    return done
