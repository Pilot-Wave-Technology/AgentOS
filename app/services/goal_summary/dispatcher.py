"""Goal Summary dispatcher — gathers context for a goal and generates two
documents by calling the LLM directly:

  1. Feature Overview (doc_type='progress') — plain-language product
     description of what this goal delivers. Written for a non-technical
     person who wants to understand the product, not the project.

  2. Work Documentation (doc_type='code') — technical record of what was
     built, how, and why. Implementation detail, design decisions, code
     review findings. Written for a developer reading this months later.

Both documents are written directly to goal_documents by this module —
no tool calls, no GO agent involvement. The LLM is called twice in
parallel (once per doc_type) via app.services.utility_llm, using
whichever provider/model the `goal_summary_doc` purpose is configured for.

External commit detection: a commit is flagged as external when its actor_id
does not belong to any goal_member AND the commit author_email is not in the
member email set.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.database import get_pool

log = logging.getLogger(__name__)

# Characters of plan content to include per task.
_MAX_PLAN_CHARS = 5000
# Maximum commits to surface.
_MAX_COMMITS = 50
# Characters of council convener output to include.
_MAX_COUNCIL_CHARS = 6000
# Task reviews to pull.
_MAX_TASK_REVIEWS = 30
# Characters per .plan workspace file.
_MAX_SCOPE_FILE_CHARS = 10_000
# Characters of each existing document to include for update context.
_MAX_EXISTING_DOC_CHARS = 8_000


async def dispatch_summary_for_goal(goal_id: str) -> bool:
    """Gather context, call LLM twice in parallel, write both documents to DB.

    Returns True if at least one document was written, False if the goal
    doesn't exist or both LLM calls failed.
    """
    log.info("goal-summary: dispatch started for goal %s", goal_id)
    pool = get_pool()

    goal = await pool.fetchrow(
        "SELECT id, name, objective, status, completion_pct FROM goals WHERE id = $1",
        goal_id,
    )
    last_generated_at = await pool.fetchval(
        "SELECT generated_at FROM goal_documents WHERE goal_id = $1 AND doc_type = 'code'",
        goal_id,
    )

    if not goal:
        log.warning("goal-summary: goal %s not found — skipping", goal_id)
        return False

    log.info(
        "goal-summary: goal %s (%s) — status=%s progress=%s%%",
        goal_id, goal["name"], goal["status"], goal["completion_pct"] or 0,
    )

    # Write provisional requested_at for both doc types so the scheduler
    # won't re-dispatch this goal until the interval elapses.
    await pool.execute(
        """
        INSERT INTO goal_documents (goal_id, doc_type, requested_at)
        VALUES ($1, 'code', now()), ($1, 'progress', now())
        ON CONFLICT (goal_id, doc_type) DO UPDATE
            SET requested_at = now(), updated_at = now()
        """,
        goal_id,
    )

    ctx = await _gather_context(pool, goal_id, goal, last_generated_at)

    log.info(
        "goal-summary: context gathered for goal %s — "
        "done_stories=%d active_stories=%d decisions=%d "
        "council_review=%s security_findings=%d task_reviews=%d "
        "commits=%d (external=%d, since=%s) resources=%d members=%d "
        "scope_plan_docs=%d existing_docs=%s",
        goal_id,
        len(ctx["done_stories"]),
        len(ctx["active_stories"]),
        len(ctx["decisions"]),
        "yes" if ctx["council_convener"] else "no",
        len(ctx["security_findings"]),
        len(ctx["task_reviews"]),
        len(ctx["commit_rows"]),
        ctx["external_commit_count"],
        ctx["commit_since_label"],
        len(ctx["resources"]),
        len(ctx["members"]),
        len(ctx["scope_plan_docs"]),
        list(ctx["existing_docs"].keys()),
    )

    context_section = _build_context_section(goal, ctx)

    # Generate both documents in parallel.
    progress_content, code_content = await asyncio.gather(
        _generate_document("progress", goal, ctx, context_section),
        _generate_document("code", goal, ctx, context_section),
    )

    written = 0
    for doc_type, content in [("progress", progress_content), ("code", code_content)]:
        if not content:
            log.warning(
                "goal-summary: %s document is empty for goal %s — skipping write",
                doc_type, goal_id,
            )
            continue
        await pool.execute(
            """
            INSERT INTO goal_documents
                (goal_id, doc_type, content, generated_at, generated_by, updated_at)
            VALUES ($1, $2, $3, now(), 'goal-summary-dispatcher', now())
            ON CONFLICT (goal_id, doc_type) DO UPDATE
                SET content      = EXCLUDED.content,
                    generated_at = now(),
                    generated_by = EXCLUDED.generated_by,
                    updated_at   = now()
            """,
            goal_id, doc_type, content,
        )
        log.info(
            "goal-summary: %s document updated for goal %s (%d chars)",
            doc_type, goal_id, len(content),
        )
        written += 1

    log.info(
        "goal-summary: dispatch complete for goal %s — %d/2 documents written",
        goal_id, written,
    )
    return written > 0


async def _generate_document(
    doc_type: str, goal, ctx: dict, context_section: str
) -> str:
    """Call the LLM to generate one goal document. Returns empty string on failure."""
    if doc_type == "progress":
        system_prompt, user_prompt = _build_progress_prompt(goal, ctx, context_section)
    else:
        system_prompt, user_prompt = _build_code_prompt(goal, ctx, context_section)

    log.debug(
        "goal-summary: calling LLM for %s document, goal %s (%d prompt chars)",
        doc_type, goal["id"], len(user_prompt),
    )

    try:
        from app.services.utility_llm import generate_utility_text

        content = await generate_utility_text(
            "goal_summary_doc", user_prompt, system_instruction=system_prompt,
        )
        if not content:
            log.warning(
                "goal-summary: LLM returned empty response for %s document, goal %s",
                doc_type, goal["id"],
            )
        return content
    except Exception as exc:
        log.warning(
            "goal-summary: LLM call failed for %s document, goal %s: %s",
            doc_type, goal["id"], exc,
        )
        return ""


async def _gather_context(pool, goal_id: str, goal, last_generated_at=None) -> dict:
    log.debug("goal-summary: gathering context for goal %s", goal_id)

    done_stories = await pool.fetch(
        """
        SELECT id, title, state, assigned_to, result_summary, completed_at
          FROM stories
         WHERE goal_id = $1
           AND state IN ('done', 'rejected', 'cancelled')
         ORDER BY completed_at DESC NULLS LAST
        """,
        goal_id,
    )
    log.debug("goal-summary: fetched %d done stories for goal %s", len(done_stories), goal_id)

    active_stories = await pool.fetch(
        """
        SELECT id, title, state, assigned_to, progress_summary, blocked_reason, estimated_effort
          FROM stories
         WHERE goal_id = $1
           AND state NOT IN ('done', 'rejected', 'cancelled')
         ORDER BY
           CASE state
             WHEN 'in_progress' THEN 1
             WHEN 'blocked'     THEN 2
             WHEN 'accepted'    THEN 3
             ELSE 4
           END,
           created_at
        """,
        goal_id,
    )
    log.debug("goal-summary: fetched %d active stories for goal %s", len(active_stories), goal_id)

    decisions = await pool.fetch(
        """
        SELECT gd.id, gd.decision, gd.rationale, gd.decided_by, gd.story_id, gd.created_at,
               COALESCE(u.display_name, a.name, gd.decided_by) AS decided_by_name
          FROM goal_decisions gd
          LEFT JOIN users u ON u.uid = gd.decided_by
          LEFT JOIN agents a ON a.id = gd.decided_by
         WHERE gd.goal_id = $1
         ORDER BY gd.created_at DESC
         LIMIT 50
        """,
        goal_id,
    )
    log.debug("goal-summary: fetched %d decisions for goal %s", len(decisions), goal_id)

    council_convener = await pool.fetchrow(
        """
        SELECT crs.output, crr.finished_at, crr.trigger
          FROM council_review_stages crs
          JOIN council_review_runs crr ON crr.id = crs.run_id
         WHERE crr.goal_id = $1
           AND crs.stage = 'convener'
           AND crs.status = 'done'
           AND crr.status = 'done'
         ORDER BY crr.finished_at DESC NULLS LAST
         LIMIT 1
        """,
        goal_id,
    )

    security_findings = await pool.fetch(
        """
        SELECT sf.severity, sf.title, sf.description, sf.file, sf.rule, sf.aspect, sf.status
          FROM security_findings sf
          JOIN security_review_runs srr ON srr.id = sf.run_id
          JOIN goal_resources gr
            ON gr.branch_name = srr.branch_name
           AND gr.goal_id = $1
           AND gr.kind = 'branch'
           AND gr.status = 'active'
         WHERE srr.status = 'done'
           AND sf.status = 'open'
         ORDER BY
           CASE sf.severity
             WHEN 'critical' THEN 1 WHEN 'high' THEN 2
             WHEN 'medium'   THEN 3 WHEN 'low'  THEN 4 ELSE 5
           END,
           sf.created_at DESC
         LIMIT 20
        """,
        goal_id,
    )
    if security_findings:
        log.info("goal-summary: %d open security finding(s) for goal %s", len(security_findings), goal_id)

    plan_rows = await pool.fetch(
        """
        SELECT t.id, t.title, t.plan_content, t.state, t.story_id, s.title AS story_title
          FROM tasks t
          LEFT JOIN stories s ON s.id = t.story_id
         WHERE t.goal_id = $1
           AND t.plan_content IS NOT NULL AND t.plan_content <> ''
           AND t.state IN ('in_progress', 'done')
           AND t.status = 'active'
           AND t.parent_task_id IS NULL
         ORDER BY t.updated_at DESC
         LIMIT 20
        """,
        goal_id,
    )

    task_reviews = await pool.fetch(
        """
        SELECT tr.verdict, tr.quality_score, tr.findings, tr.recommendations,
               tr.summary, tr.reviewed_at,
               t.title AS task_title, t.story_id
          FROM task_reviews tr
          JOIN tasks t ON t.id = tr.task_id
         WHERE t.goal_id = $1
           AND tr.verdict IS NOT NULL
         ORDER BY tr.reviewed_at DESC
         LIMIT $2
        """,
        goal_id, _MAX_TASK_REVIEWS,
    )
    log.debug("goal-summary: fetched %d task review(s) for goal %s", len(task_reviews), goal_id)

    members = await pool.fetch(
        """
        SELECT gm.uid, u.display_name, u.email, gm.role
          FROM goal_members gm
          JOIN users u ON u.uid = gm.uid
         WHERE gm.goal_id = $1
        """,
        goal_id,
    )
    member_uids = {m["uid"] for m in members}
    member_emails = {(m["email"] or "").lower() for m in members if m["email"]}

    resources = await pool.fetch(
        """
        SELECT name, kind, uri, branch_name
          FROM goal_resources
         WHERE goal_id = $1 AND status = 'active'
         ORDER BY created_at
         LIMIT 20
        """,
        goal_id,
    )

    if last_generated_at:
        commit_rows = await pool.fetch(
            """
            SELECT le.payload, le.actor_id, le.timestamp
              FROM ledger_events le
             WHERE le.context_goal_id = $1
               AND le.event_type = 'ws.commit_created'
               AND le.timestamp > $2
             ORDER BY le.timestamp DESC
             LIMIT $3
            """,
            goal_id, last_generated_at, _MAX_COMMITS,
        )
    else:
        commit_rows = await pool.fetch(
            """
            SELECT le.payload, le.actor_id, le.timestamp
              FROM ledger_events le
             WHERE le.context_goal_id = $1
               AND le.event_type = 'ws.commit_created'
             ORDER BY le.timestamp DESC
             LIMIT $2
            """,
            goal_id, _MAX_COMMITS,
        )
    log.debug("goal-summary: fetched %d commits for goal %s", len(commit_rows), goal_id)

    external_count = 0
    internal_count = 0
    for row in commit_rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        payload = payload or {}
        author_email = (payload.get("author_email") or "").lower()
        if row["actor_id"] not in member_uids and author_email not in member_emails:
            external_count += 1
        else:
            internal_count += 1

    if external_count:
        log.info("goal-summary: %d external commit(s) for goal %s", external_count, goal_id)

    commit_since_label = (
        last_generated_at.strftime("%Y-%m-%d %H:%M UTC") if last_generated_at else "goal start"
    )

    # Read .plan/ files from SHCA workspaces.
    shca_agents = await pool.fetch(
        """
        SELECT DISTINCT a.id AS gateway_agent_id
          FROM agents a
          JOIN threads t ON t.agent_id = a.id
         WHERE t.goal_id = $1
           AND a.type = 'SHCA'
           AND a.status = 'active'
        """,
        goal_id,
    )
    log.debug(
        "goal-summary: found %d active SHCA(s) for goal %s — scanning .plan/ folders",
        len(shca_agents), goal_id,
    )

    from app.storage import read_workspace_folder

    scope_plan_docs: list[dict] = []
    seen_filenames: set[str] = set()
    for shca in shca_agents:
        gid = shca["gateway_agent_id"]
        try:
            files = await read_workspace_folder(goal_id, gid, ".plan")
            for f in files:
                if f["name"] in seen_filenames:
                    continue
                seen_filenames.add(f["name"])
                content = f["content"].strip()
                scope_plan_docs.append({
                    "name": f["name"],
                    "content": content[:_MAX_SCOPE_FILE_CHARS],
                    "truncated": len(content) > _MAX_SCOPE_FILE_CHARS,
                })
                log.info(
                    "goal-summary: loaded .plan/%s (%d chars) from SHCA %s for goal %s",
                    f["name"], len(content), gid, goal_id,
                )
        except Exception as exc:
            log.warning(
                "goal-summary: failed to read .plan/ from SHCA %s for goal %s: %s",
                gid, goal_id, exc,
            )

    if not scope_plan_docs:
        log.info(
            "goal-summary: no .plan files found across %d SHCA(s) for goal %s",
            len(shca_agents), goal_id,
        )

    existing_doc_rows = await pool.fetch(
        """
        SELECT doc_type, content, generated_at
          FROM goal_documents
         WHERE goal_id = $1
           AND content IS NOT NULL AND content <> ''
        """,
        goal_id,
    )
    existing_docs: dict[str, dict] = {}
    for row in existing_doc_rows:
        existing_docs[row["doc_type"]] = {
            "content": row["content"],
            "generated_at": (
                row["generated_at"].strftime("%Y-%m-%d %H:%M UTC")
                if row["generated_at"] else "unknown"
            ),
        }
    if existing_docs:
        log.debug(
            "goal-summary: found existing docs %s for goal %s",
            list(existing_docs.keys()), goal_id,
        )

    return {
        "done_stories": done_stories,
        "active_stories": active_stories,
        "decisions": decisions,
        "council_convener": council_convener,
        "security_findings": security_findings,
        "plan_rows": plan_rows,
        "task_reviews": task_reviews,
        "commit_rows": commit_rows,
        "commit_since_label": commit_since_label,
        "members": members,
        "member_uids": member_uids,
        "member_emails": member_emails,
        "resources": resources,
        "external_commit_count": external_count,
        "internal_commit_count": internal_count,
        "scope_plan_docs": scope_plan_docs,
        "existing_docs": existing_docs,
    }


def _build_context_section(goal, ctx: dict) -> str:
    """Build the shared context block included in both document prompts."""
    decisions_by_story: dict[str, list] = {}
    goal_level_decisions = []
    for d in ctx["decisions"]:
        sid = d["story_id"] or ""
        if sid:
            decisions_by_story.setdefault(sid, []).append(d)
        else:
            goal_level_decisions.append(d)

    plan_by_story: dict[str, list] = {}
    for p in ctx["plan_rows"]:
        plan_by_story.setdefault(p["story_id"] or "", []).append(p)

    reviews_by_story: dict[str, list] = {}
    for r in ctx["task_reviews"]:
        reviews_by_story.setdefault(r["story_id"] or "", []).append(r)

    in_progress = [s for s in ctx["active_stories"] if s["state"] == "in_progress"]
    blocked = [s for s in ctx["active_stories"] if s["state"] == "blocked"]
    upcoming = [s for s in ctx["active_stories"] if s["state"] not in ("in_progress", "blocked")]

    lines: list[str] = [
        f"GOAL: {goal['name']}",
        f"Status: {goal['status']}  |  Progress: {goal['completion_pct'] or 0}%",
    ]
    if goal["objective"]:
        lines.append(f"Objective: {goal['objective']}")

    if ctx["scope_plan_docs"]:
        lines += ["", "PLAN & SCOPE FILES (primary source for Feature Overview):"]
        for doc in ctx["scope_plan_docs"]:
            lines.append(f"\n### {doc['name']}")
            lines.append(doc["content"])
            if doc["truncated"]:
                lines.append(f"[... truncated at {_MAX_SCOPE_FILE_CHARS} chars]")
    else:
        lines += [
            "",
            "PLAN & SCOPE FILES: none found — derive scope from goal objective and stories.",
        ]

    lines += ["", "Team:"]
    for m in ctx["members"]:
        lines.append(f"  - {m['display_name']} <{m['email']}> [{m['role']}]")

    if ctx["resources"]:
        lines += ["", "Technical Resources:"]
        for r in ctx["resources"]:
            ref = r.get("branch_name") or r.get("uri") or ""
            lines.append(f"  - [{r['kind']}] {r['name']}" + (f" — {ref}" if ref else ""))

    lines += ["", "COMPLETED STORIES:"]
    if ctx["done_stories"]:
        for s in ctx["done_stories"]:
            completed = s["completed_at"].strftime("%Y-%m-%d") if s["completed_at"] else "unknown"
            lines.append(f"\n### [{s['state'].upper()}] {s['title']}")
            lines.append(f"Assigned: {s['assigned_to'] or 'unassigned'}  |  Completed: {completed}")
            if s.get("result_summary"):
                lines.append(f"Result: {s['result_summary']}")
            story_plans = plan_by_story.get(s["id"], [])
            if story_plans:
                lines.append("Implementation plan:")
                for p in story_plans:
                    content = (p["plan_content"] or "").strip()
                    lines.append(f"  Task [{p['state']}] — {p['title']}:")
                    lines.append(f"  {content[:_MAX_PLAN_CHARS]}")
                    if len(content) > _MAX_PLAN_CHARS:
                        lines.append(f"  ... [{len(content) - _MAX_PLAN_CHARS} chars truncated]")
            story_reviews = reviews_by_story.get(s["id"], [])
            if story_reviews:
                lines.append("Code review results:")
                for rev in story_reviews:
                    score = f"  quality={rev['quality_score']}/100" if rev["quality_score"] is not None else ""
                    lines.append(f"  Review: verdict={rev['verdict']}{score} ({rev['task_title']})")
                    if rev.get("summary"):
                        lines.append(f"  Summary: {rev['summary']}")
                    for f in (rev.get("findings") or []):
                        lines.append(f"    Finding: {f}")
                    for r in (rev.get("recommendations") or []):
                        lines.append(f"    Recommendation: {r}")
            story_decisions = decisions_by_story.get(s["id"], [])
            if story_decisions:
                lines.append("Decisions:")
                for d in story_decisions:
                    lines.append(f"  - {d['decision']}")
                    if d.get("rationale"):
                        lines.append(f"    Rationale: {d['rationale']}")
                    lines.append(f"    Decided by: {d['decided_by_name']}")
    else:
        lines.append("No completed stories yet.")

    lines += ["", "IN PROGRESS:"]
    if in_progress:
        for s in in_progress:
            lines.append(f"\n### [IN PROGRESS] {s['title']}")
            lines.append(f"Assigned: {s['assigned_to'] or 'unassigned'}")
            if s.get("progress_summary"):
                lines.append(f"Current state: {s['progress_summary']}")
            for p in plan_by_story.get(s["id"], []):
                lines.append(f"Plan [{p['state']}] {p['title']}: {(p['plan_content'] or '').strip()[:_MAX_PLAN_CHARS]}")
            for d in decisions_by_story.get(s["id"], []):
                lines.append(f"Decision: {d['decision']} — {d['decided_by_name']}")
    else:
        lines.append("No stories in progress.")

    if blocked:
        lines += ["", "BLOCKED:"]
        for s in blocked:
            lines.append(f"\n### [BLOCKED] {s['title']}")
            lines.append(f"Assigned: {s['assigned_to'] or 'unassigned'}")
            if s.get("blocked_reason"):
                lines.append(f"Reason: {s['blocked_reason']}")

    if upcoming:
        lines += ["", "UPCOMING:"]
        for s in upcoming:
            lines.append(f"  [{s['state']}] {s['title']}")

    if goal_level_decisions:
        lines += ["", "ARCHITECTURAL & DESIGN DECISIONS (goal-level):"]
        for d in goal_level_decisions:
            lines.append(f"\n- **{d['decision']}**")
            if d.get("rationale"):
                lines.append(f"  Rationale: {d['rationale']}")
            lines.append(
                f"  Decided by: {d['decided_by_name']}  |  "
                f"{d['created_at'].strftime('%Y-%m-%d') if d['created_at'] else ''}"
            )

    if ctx["council_convener"]:
        c = ctx["council_convener"]
        finished = c["finished_at"].strftime("%Y-%m-%d %H:%M") if c["finished_at"] else "unknown"
        lines += ["", f"GOAL REVIEW COUNCIL (run: {finished}, trigger: {c['trigger']}):"]
        output = c["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:
                pass
        if isinstance(output, dict):
            for key in ("summary", "findings", "recommendations", "verdict", "content"):
                val = output.get(key)
                if val:
                    lines.append(f"**{key.capitalize()}:** {str(val)[:_MAX_COUNCIL_CHARS]}")
        elif output:
            lines.append(str(output)[:_MAX_COUNCIL_CHARS])

    if ctx["security_findings"]:
        lines += ["", "OPEN SECURITY FINDINGS:"]
        for f in ctx["security_findings"]:
            loc = f" ({f['file']})" if f.get("file") else ""
            lines.append(f"  [{f['severity'].upper()}] {f['title']}{loc}")
            if f.get("description"):
                lines.append(f"    {f['description'][:300]}")

    since_label = ctx["commit_since_label"]
    lines += ["", f"COMMITS SINCE {since_label}:"]
    if ctx["commit_rows"]:
        for row in ctx["commit_rows"]:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            payload = payload or {}
            sha = (payload.get("commit_sha") or payload.get("sha") or "")[:8]
            msg = (payload.get("message") or payload.get("commit_message") or "").split("\n")[0][:120]
            author = payload.get("author") or payload.get("author_name") or row["actor_id"] or "unknown"
            ts = row["timestamp"].strftime("%Y-%m-%d %H:%M") if row.get("timestamp") else ""
            author_email = (payload.get("author_email") or "").lower()
            is_external = (
                row["actor_id"] not in ctx["member_uids"]
                and author_email not in ctx["member_emails"]
            )
            prefix = "⚠️ [external]" if is_external else "[internal]"
            lines.append(f"  {prefix} {sha} {ts} — {msg!r} by {author}")
    else:
        lines.append(f"  (no commits since {since_label})")

    return "\n".join(lines)


def _build_progress_prompt(goal, ctx: dict, context_section: str) -> tuple[str, str]:
    """Build system + user prompt for the Feature Overview (doc_type='progress')."""
    system = (
        "You are writing plain-language feature documentation for a software project. "
        "Output ONLY the markdown document — no preamble, no 'here is the document', "
        "no commentary. Start directly with the # heading."
    )

    existing = ctx["existing_docs"].get("progress", {})
    lines: list[str] = []

    if existing.get("content"):
        old = existing["content"]
        stale_markers = (
            "Goal Progress", "Completed Stories", "Council Findings", "Active Work",
            "story-state", "## Available Now", "## In Development", "## Coming Up",
            "## Features", "## What It Does", "## What's Working",
        )
        needs_full_rewrite = any(m in old for m in stale_markers)
        if needs_full_rewrite:
            lines += [
                "A previous Feature Overview exists but it is in an outdated format.",
                "REWRITE it from scratch using the instructions below.",
                "You may draw on the information in it, but do NOT copy its structure.",
                "",
                "PREVIOUS FEATURE OVERVIEW (for reference only):",
                old[:_MAX_EXISTING_DOC_CHARS],
                "",
            ]
        else:
            lines += [
                f"A previous Feature Overview exists (generated {existing['generated_at']}).",
                "Update it using the instructions below. Preserve what is still accurate.",
                "Do not shrink or condense anything already there.",
                "",
                "PREVIOUS FEATURE OVERVIEW:",
                old[:_MAX_EXISTING_DOC_CHARS],
                "",
            ]

    lines += [
        "Write a Feature Overview for the goal described in CONTEXT below.",
        "",
        "Model your output on a well-written open source project README — think LangGraph,",
        "n8n, or Dify. Those documents are scannable in under a minute. Every feature is",
        "one line: a bold name and a single sentence. No paragraphs. No restating the same",
        "point in different words. No filler phrases like 'seamlessly', 'ensures that',",
        "'significantly reduces'. Just what it does, stated once, clearly.",
        "",
        "RULES:",
        "  - No code, technical terms, product names, or function names",
        "  - No story states, percentages, or sprint jargon",
        "  - Translate to user value: 'added an API endpoint' → 'users can now do X'",
        "  - Internal refactors with no user-visible effect → omit",
        "  - Do not restate the same point twice in different words",
        "",
        "STRUCTURE:",
        "Start with the goal name as a # heading and a 2-sentence overview.",
        "Then organise the rest however best fits the content — section names, groupings,",
        "and order are your call. Use judgment: a single focused goal might need no subgroups;",
        "a broad goal might need several named sections. If the pieces connect in a non-obvious",
        "way, add a short paragraph explaining the end-to-end flow. If nothing is in progress,",
        "don't add a 'coming soon' section.",
        "",
        "The one consistent rule across all sections:",
        "  **Feature name** — one sentence saying what it does.",
        "No sub-bullets, no paragraphs, no elaboration beyond that one sentence.",
        "",
        "---",
        "CONTEXT:",
        "",
        context_section,
    ]

    return system, "\n".join(lines)


def _build_code_prompt(goal, ctx: dict, context_section: str) -> tuple[str, str]:
    """Build system + user prompt for the Work Documentation (doc_type='code')."""
    system = (
        "You are writing technical documentation for a software development team. "
        "Output ONLY the markdown document — no preamble, no 'here is the document', "
        "no commentary. Start directly with the # heading."
    )

    existing = ctx["existing_docs"].get("code", {})
    lines: list[str] = []

    if existing.get("content"):
        lines += [
            f"A previous Work Documentation exists (generated {existing['generated_at']}).",
            "Update and extend it — do not discard sections that have not changed.",
            "Preserve sections unchanged. Add or revise only what the new context requires.",
            "",
            "PREVIOUS WORK DOCUMENTATION:",
            existing["content"][:_MAX_EXISTING_DOC_CHARS],
            "",
        ]

    lines += [
        "Write Work Documentation for the goal described in CONTEXT below.",
        "",
        "This is TECHNICAL DOCUMENTATION of what was built in the codebase.",
        "Audience: a developer reading this months from now asking 'what was built here",
        "and why is the code structured this way?'",
        "",
        "Plan files are in CONTEXT — do NOT rehash them. Capture what plans do NOT contain:",
        "implementation choices, design reasoning, deviations, problems hit and how solved,",
        "code review findings, architectural decisions. Be very detailed.",
        "",
        "FORMAT:",
        "# {goal name} — Work Documentation",
        "## Overview — what is being built, the technical problem, team, repos",
        "## Completed Work — one ### per story:",
        "    What was implemented (technical detail)",
        "    How (approach, patterns, libraries, code structure from plan_content)",
        "    Why (reasoning, trade-offs, alternatives rejected)",
        "    Problems encountered and how resolved",
        "    Code review findings (RA verdict, quality score, findings, recs)",
        "    Decisions made (with attribution)",
        "## In Progress — current approach in enough detail to pick it up",
        "## Blocked — specific technical reason, what would unblock it",
        "## Upcoming Work — brief technical description per story",
        "## Architectural & Design Decisions — why the code does X this way",
        "## Code Review & Quality — council findings, recommendations",
        "## Security — open findings (only if present)",
        f"## Recent Commits — since {ctx['commit_since_label']}, flag externals with ⚠️",
        "## Resources & Scope — repos, branches",
        "",
        "Write at length. Be technically precise.",
        "",
        "---",
        "CONTEXT:",
        "",
        context_section,
    ]

    return system, "\n".join(lines)
