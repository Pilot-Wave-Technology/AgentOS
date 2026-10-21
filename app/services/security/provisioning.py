"""Provision + run a single Security Council stage as a transient gateway agent.

Aspect stages (secrets/sast/…) are SHELL-CAPABLE: the platform clones the branch
read-only into the agent's workspace (agents.workspace.prepareRepo — keeps .git,
strips the remote/creds), then the agent runs offline scanners on `./repo`,
triages, and finishes by calling submit_security_review (→ /internal/security/
review, which validates, stores findings, and marks the stage terminal).

The convener stage is READ-ONLY: it gets the aspect reports + structured findings
as docs/ files and synthesizes — no checkout, no shell.

Mirrors app/services/council/provisioning.py (ack → poll → nudge → cleanup), with
clone-prep + a github read-only token that the agent never sees (server-side only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app import local_dev
from app.database import get_pool
from app.openclaw import gateway_admin

log = logging.getLogger(__name__)

# The "agent" RPC returns on ACK (runId), not when the turn finishes — so we ack
# the kickoff, then POLL the stage until submit_security_review marks it
# terminal.
#
# Two failure modes pull in opposite directions:
#   (a) the agent is genuinely BUSY (a long scan running in the background) — we
#       must NOT nudge aggressively or kill it on a short clock; and
#   (b) the agent FINISHED but just didn't call the finish tool — here waiting a
#       long fixed window before the first nudge wastes minutes.
# The gateway exposes no reliable "is this run still in flight?" signal across the
# admin-dispatch path (sessions.status only reflects the persisted store), so we
# can't tell (a) from (b) directly. Instead we use a FRONT-LOADED nudge schedule:
# a short first window (so case (b) is prodded within ~90s), then escalating
# back-off windows (so case (a) isn't spammed), and we only give up after the
# whole schedule elapses (a long ceiling, so a real scan is never killed early).
# A nudge is sent after every window except the last; the last window is the
# final grace before the stage is failed. Total budget = sum(schedule).
_AGENT_ACK_TIMEOUT = 60
_POLL_INTERVAL = 5
# aspect (scanning): first nudge @90s, back off, ~75 min total ceiling.
_SCAN_NUDGE_SCHEDULE = [90, 180, 300, 600, 900, 1200, 1200]
# convener (synthesis, no scan): first nudge @90s, ~13 min total ceiling.
_CONVENER_NUDGE_SCHEDULE = [90, 150, 240, 300]
# The nudge is SAFE for a still-working agent: it must not provoke a premature
# partial submit. It explicitly tells a busy agent to ignore it and keep going.
_NUDGE_MESSAGE = (
    "Status check — your security review stage is not yet recorded. "
    "If your scanners are STILL running or you are STILL triaging findings, IGNORE this and keep "
    "going; do NOT submit a partial review. "
    "If you have FINISHED your analysis, call submit_security_review NOW — status=\"success\" "
    "with your full review_details (every required section) + the findings array, or status=\"failed\" "
    "with a reason. Only that tool call ends your run."
)

_TEMPLATE_FOR_ASPECT = {
    "secrets": "sec-secrets",
    "sast": "sec-sast",
    "sca": "sec-sca",
    "access": "sec-access",
    "infra": "sec-infra",
    "convener": "sec-convener",
}

_ASPECT_LABEL = {
    "secrets": "Secrets & Exposure",
    "sast": "Code Vulnerabilities (SAST)",
    "sca": "Dependencies & Supply Chain (SCA)",
    "access": "Access Control & Sensitive Data",
    "infra": "Infrastructure / IaC / Config",
}

_MAX_DOC_CHARS = 40000  # per-file read budget (matches the council); larger files are split


def _safe_instance_id(aspect: str, run_id: str) -> str:
    base = f"sec-{aspect}-{run_id[:8]}".lower()
    return "".join(c if (c.isalnum() or c == "-") else "-" for c in base)


def _repo_name_from(clone_url: str, fallback: str) -> str:
    if fallback:
        return fallback
    m = re.search(r"/([^/]+?)(?:\.git)?$", clone_url or "")
    return m.group(1) if m else ""


async def _fetch_template(template_id: str) -> dict:
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


def _split_doc(path: str, content: str, max_chars: int) -> dict[str, str]:
    """Split one oversized doc into continuation files at line boundaries (lossless)."""
    if len(content) <= max_chars:
        return {path: content}
    if "." in path.rsplit("/", 1)[-1]:
        stem, _, ext = path.rpartition(".")
        ext = "." + ext
    else:
        stem, ext = path, ""
    budget = max(1000, max_chars - 300)
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
            chunk += f"\n\n[CONTINUES IN `{nxt}` — read it next (part {i + 1}/{total}).]"
        else:
            chunk += f"\n\n[END — part {i + 1}/{total} of `{path}`.]"
        out[fn] = chunk
    return out


def _bound_docs(docs: dict[str, str], max_chars: int = _MAX_DOC_CHARS) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, content in docs.items():
        out.update(_split_doc(path, content or "", max_chars))
    return out


async def _referencing_goals(clone_url: str, branch_name: str) -> list[dict]:
    """Active goals that have this (clone_url, branch_name) as a resource."""
    pool = get_pool()
    rows = await pool.fetch(
        # NON-DELETED goals (goals.status='active'), any lifecycle state — a
        # planning goal still references the branch. See delivery.deliver_report.
        "SELECT DISTINCT g.id, g.name FROM goals g "
        "JOIN goal_resources r ON r.goal_id = g.id "
        "WHERE g.status = 'active' AND r.status = 'active' "
        "AND r.kind = 'branch' AND lower(r.branch_name) = lower($2) "
        "AND lower(r.clone_url) = lower($1) "
        "ORDER BY g.name",
        clone_url, branch_name,
    )
    return [{"id": r["id"], "name": r["name"]} for r in rows]


# ── input builders ──────────────────────────────────────────────────────────

def _context_doc(run: dict, aspect: str, instance_id: str, review_id: str) -> str:
    return json.dumps({
        "run_id": run["id"],
        "review_id": review_id,
        "agent_id": instance_id,
        "aspect": aspect,
        "repo_owner": run["repo_owner"],
        "repo_name": run["repo_name"],
        "branch_name": run["branch_name"],
        "head_sha": run["head_sha"] or "(resolved at clone time)",
    }, indent=2)


def _repo_info_doc(run: dict) -> str:
    return (
        f"# Repository under review\n\n"
        f"- **Repo:** {run['repo_owner']}/{run['repo_name']}\n"
        f"- **Branch:** {run['branch_name']}\n"
        f"- **Head SHA:** {run['head_sha'] or '(see SECURITY_CONTEXT.json after clone)'}\n\n"
        f"The branch is checked out **read-only** at `./repo` (its `.git` history is intact; "
        f"there is no remote and you have no network). Run your scanners against `./repo`.\n\n"
        f"Exclude noise: `node_modules`, `vendor`, `dist`, `build`, `.git` internals, and any "
        f"minified/generated/lockfile blobs (sample them at most). `wc -c` a file before reading it.\n"
    )


async def _build_aspect_inputs(run: dict, aspect: str, instance_id: str, review_id: str) -> dict[str, str]:
    return {
        "docs/SECURITY_CONTEXT.json": _context_doc(run, aspect, instance_id, review_id),
        "docs/REPO_INFO.md": _repo_info_doc(run),
    }


async def _build_convener_inputs(run: dict, instance_id: str, review_id: str) -> dict[str, str]:
    pool = get_pool()
    docs: dict[str, str] = {
        "docs/SECURITY_CONTEXT.json": _context_doc(run, "convener", instance_id, review_id),
    }

    goals = await _referencing_goals(run["clone_url"], run["branch_name"])
    goal_lines = "\n".join(f"- {g['name']} (`{g['id']}`)" for g in goals) or "- (none currently active)"
    docs["docs/BRANCH_INFO.md"] = (
        f"# Branch under review\n\n"
        f"- **Repo:** {run['repo_owner']}/{run['repo_name']}\n"
        f"- **Branch:** {run['branch_name']}\n"
        f"- **Head SHA:** {run['head_sha'] or '(unknown)'}\n\n"
        f"## Active goals that reference this branch (delivery targets)\n{goal_lines}\n"
    )

    # Each aspect's report (output) — or a placeholder if it failed/was absent.
    stages = await pool.fetch(
        "SELECT aspect, status, output, error FROM security_review_stages "
        "WHERE run_id = $1 AND aspect <> 'convener'",
        run["id"],
    )
    for st in stages:
        aspect = st["aspect"]
        fn = f"docs/FINDINGS_{aspect.upper()}.md"
        out = st["output"]
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except Exception:
                pass
        if st["status"] == "done" and isinstance(out, str) and out.strip():
            docs[fn] = out
        else:
            reason = (st["error"] or "").strip()
            docs[fn] = (
                f"({_ASPECT_LABEL.get(aspect, aspect)} review unavailable — status: "
                f"{st['status']}. {reason})\n"
            )

    # Flat, pre-counted findings table — the convener's authoritative tally.
    findings = await pool.fetch(
        "SELECT aspect, severity, file, line, rule, tool, title FROM security_findings "
        "WHERE run_id = $1 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, aspect",
        run["id"],
    )
    if findings:
        by_sev: dict[str, int] = {}
        for f in findings:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        tally = ", ".join(f"{by_sev[s]} {s}" for s in ("critical", "high", "medium", "low", "info") if by_sev.get(s))
        lines = [
            f"# Structured findings ({len(findings)} total — {tally})",
            "",
            "| severity | aspect | file:line | tool | rule | title |",
            "|---|---|---|---|---|---|",
        ]
        for f in findings:
            loc = f["file"] + (f":{f['line']}" if f["line"] else "")
            lines.append(
                f"| {f['severity']} | {f['aspect']} | {loc} | {f['tool']} | "
                f"{(f['rule'] or '')[:60]} | {(f['title'] or '')[:80]} |"
            )
        docs["docs/STRUCTURED_FINDINGS.md"] = "\n".join(lines)
    else:
        docs["docs/STRUCTURED_FINDINGS.md"] = (
            "(No structured findings were recorded by any aspect this run. If aspect reports "
            "above also failed, treat this as a coverage gap — not a clean bill of health.)"
        )
    return docs


def _build_kickoff(aspect: str, run: dict, instance_id: str, review_id: str,
                   docs: dict[str, str]) -> str:
    file_list = "\n".join(f"  - {k}" for k in sorted(docs.keys()))
    role = "Security Council Convener" if aspect == "convener" else f"'{aspect}' security reviewer"
    scan_line = (
        "- You synthesize only — do NOT scan, fetch, browse, or execute.\n"
        if aspect == "convener" else
        "- The branch is cloned read-only at `./repo`. Run your offline scanners there per your AGENTS.md "
        "(write scanner output to a file, then read counts/top items with jq/head — never dump raw JSON).\n"
    )
    return (
        f"You are the {role} for branch '{run['branch_name']}' of "
        f"{run['repo_owner']}/{run['repo_name']}. This is your single review run.\n\n"
        f"Run context (also in docs/SECURITY_CONTEXT.json) — pass ALL FOUR to "
        f"submit_security_review UNCHANGED:\n"
        f"  run_id={run['id']}\n  review_id={review_id}\n  agent_id={instance_id}\n  aspect={aspect}\n\n"
        f"Your inputs are already in your workspace as files. Read each with the `read` tool:\n"
        f"{file_list}\n\n"
        f"What to do:\n"
        f"- Read every file above (start with docs/SECURITY_CONTEXT.json), then follow the numbered "
        f"steps in your AGENTS.md.\n"
        f"{scan_line}"
        # Mirrors the council kickoff — demand the tool call rather than "form your
        # review, then call". NOTE it does NOT fix Gemini's MALFORMED_FUNCTION_CALL;
        # that theory was measured and falsified (see the matching note in
        # app/services/council/provisioning.py and
        # research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md). Clearer wording only.
        f"- Do NOT write the review out in your reply. Put the whole review INSIDE the "
        f"submit_security_review call — your turn should be the tool call itself:\n"
        f"    • success: status=\"success\" with a complete review_details (every required section "
        f"from your AGENTS.md) and a findings array (empty for the convener).\n"
        f"    • if you CANNOT complete: status=\"failed\" with reason=\"<what went wrong>\".\n"
        f"- If it returns ok:false with errors, fix exactly those and call it again.\n\n"
        f"You MUST end your run by calling submit_security_review (status \"success\" or \"failed\"). "
        f"Never end any other way."
    )


# ── stage execution ───────────────────────────────────────────────────────────

async def _fail_stage(stage_id: str, error: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE security_review_stages SET status='failed', error=$1, finished_at=NOW() "
        "WHERE id=$2 AND status <> 'done'",
        error[:500], stage_id,
    )
    log.warning("security: stage %s failed: %s", stage_id, error)


async def _wait_for_stage_terminal(stage_id: str, timeout_s: int) -> bool:
    pool = get_pool()
    waited = 0
    while waited < timeout_s:
        row = await pool.fetchrow("SELECT status FROM security_review_stages WHERE id = $1", stage_id)
        if row and row["status"] in ("done", "failed"):
            return True
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    return False


async def _prepare_repo(instance_id: str, run: dict) -> str:
    """Clone the branch read-only into the agent workspace (server-side).

    Mints a short-lived contents:read github token (never written to the
    workspace), hands it to the gateway RPC, which clones depth-1 into `repo/`,
    keeps `.git`, and strips the remote/creds. Returns the resolved head SHA.
    Raises on failure (the caller fails the stage).
    """
    repo_name = _repo_name_from(run["clone_url"], run["repo_name"])
    token = ""
    try:
        from app.github_tokens import generate_installation_token
        tok = await generate_installation_token(
            [repo_name] if repo_name else [], permissions={"contents": "read"},
        )
        token = (tok or {}).get("token") or ""
    except Exception as exc:
        # Public repos may clone tokenless; log and try without a token.
        log.info("security: github token mint failed for %s (trying tokenless): %s", repo_name, exc)

    resp = await gateway_admin.call(
        "agents.workspace.prepareRepo",
        {
            "agentId": instance_id,
            "cloneUrl": run["clone_url"],
            "branch": run["branch_name"],
            "token": token,
            "dest": "repo",
        },
        timeout=300,
    )
    if not isinstance(resp, dict) or not resp.get("ok"):
        raise RuntimeError(f"prepareRepo failed: {resp}")
    return str(resp.get("headSha") or resp.get("head_sha") or "")


async def run_stage(run: dict, stage_id: str, aspect: str) -> bool:
    """Provision + run one security stage. Returns True if it ended 'done'.

    `run` is the security_review_runs row (with head_sha possibly updated in
    place by clone-prep so the convener inputs reflect it).
    """
    pool = get_pool()
    template_id = _TEMPLATE_FOR_ASPECT.get(aspect)
    if not template_id:
        await _fail_stage(stage_id, f"no template mapped for aspect {aspect!r}")
        return False
    try:
        tmpl = await _fetch_template(template_id)
    except Exception as exc:
        await _fail_stage(stage_id, f"template load failed: {exc}")
        return False

    instance_id = _safe_instance_id(aspect, run["id"])
    workspace = local_dev.agent_workspace_path(instance_id)
    is_scan = aspect != "convener"
    schedule = _SCAN_NUDGE_SCHEDULE if is_scan else _CONVENER_NUDGE_SCHEDULE
    total_budget = sum(schedule)

    log.info("security: provisioning aspect=%s run=%s instance=%s (template=%s, "
             "first check @%ds, %d nudge(s), ceiling≈%dm)",
             aspect, run["id"], instance_id, template_id,
             schedule[0], len(schedule) - 1, round(total_budget / 60))

    await pool.execute(
        "UPDATE security_review_stages SET status='running', instance_id=$1, "
        "started_at=NOW(), claimed_at=NOW() WHERE id=$2",
        instance_id, stage_id,
    )

    created = False
    # Initialised outside the try because `finally` reports it — see the recovery
    # log there. Mirrors the council path.
    nudges_sent = 0
    # Bound here too: `finally` reads it to fetch the transcript when explaining a
    # failure, and the try can exit before its own assignment below is reached.
    session_key = f"agent:{instance_id}:{run['id'].lower()}-{stage_id}"
    try:
        from app.openclaw import GatewayError
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

        # Clone-prep (aspect stages only) BEFORE building inputs, so head_sha is known.
        if is_scan:
            try:
                head_sha = await _prepare_repo(instance_id, run)
                if head_sha:
                    run["head_sha"] = head_sha
                    await pool.execute(
                        "UPDATE security_review_runs SET head_sha = COALESCE(NULLIF(head_sha,''), $1) "
                        "WHERE id = $2",
                        head_sha, run["id"],
                    )
                log.info("security: cloned %s@%s for run=%s aspect=%s (instance=%s)",
                         run["clone_url"], (head_sha or "?")[:12], run["id"], aspect, instance_id)
            except Exception as exc:
                # Tagged prefix so the dispatcher can tell "couldn't clone the
                # branch" (skip the run quietly) apart from a scanner failure.
                await _fail_stage(stage_id, f"CLONE_UNAVAILABLE: clone-prep failed: {exc}")
                return False
            docs = await _build_aspect_inputs(run, aspect, instance_id, stage_id)
        else:
            docs = await _build_convener_inputs(run, instance_id, stage_id)

        identity_md = f"{tmpl['name']}\n"
        bootstrap = {**tmpl["files"], "IDENTITY.md": identity_md, "USER.md": ""}
        for fn, content in {**bootstrap, **_bound_docs(docs)}.items():
            await gateway_admin.set_agent_file(instance_id, fn, content or "")

        # (session_key is bound above the try — see the note there.)
        kickoff = _build_kickoff(aspect, run, instance_id, stage_id, docs)
        # Kill switch — system-pause guard just before agent kickoff so a pause
        # that lands mid-run doesn't start additional scanner agents.
        from app.services import execution_control as _ec  # noqa: PLC0415
        _blk = await _ec.is_system_paused()
        if _blk:
            log.info(
                "security: stage kickoff blocked by system pause (reason=%s) — aspect=%s run=%s",
                _blk.reason or "<none>", aspect, run["id"],
            )
            await _fail_stage(stage_id, f"system paused ({_blk.reason or ''}) — stage skipped")
            return False
        await gateway_admin.call(
            "agent",
            {"agentId": instance_id, "sessionKey": session_key, "message": kickoff,
             "idempotencyKey": f"security-{stage_id}"},
            timeout=_AGENT_ACK_TIMEOUT,
        )
        log.info("security: kickoff sent to %s (run=%s aspect=%s) — awaiting submit (first check @%ds)",
                 instance_id, run["id"], aspect, schedule[0])
        # Front-loaded nudge schedule: wait window[i], then (unless it's the last
        # window) send ONE safe nudge and wait the next window. Stage flips to
        # terminal within _POLL_INTERVAL of the agent actually submitting.
        terminal = False
        elapsed = 0
        last_i = len(schedule) - 1
        for i, wait_s in enumerate(schedule):
            terminal = await _wait_for_stage_terminal(stage_id, wait_s)
            elapsed += wait_s
            if terminal:
                log.info("security: %s submitted after ~%ds (run=%s aspect=%s)",
                         instance_id, elapsed, run["id"], aspect)
                break
            if i == last_i:
                break  # final grace window elapsed with no submit → give up (failed in finally)
            log.info("security: no submit after ~%ds (run=%s aspect=%s) — nudge %d/%d",
                     elapsed, run["id"], aspect, i + 1, last_i)
            nudges_sent += 1
            try:
                await gateway_admin.call(
                    "agent",
                    {"agentId": instance_id, "sessionKey": session_key, "message": _NUDGE_MESSAGE,
                     "idempotencyKey": f"security-{stage_id}-nudge{i + 1}"},
                    timeout=_AGENT_ACK_TIMEOUT,
                )
            except Exception as exc:
                log.warning("security: nudge dispatch failed for %s: %s", instance_id, exc)
    except Exception as exc:
        log.warning("security: stage run errored run=%s aspect=%s: %s", run["id"], aspect, exc)
    finally:
        row = await pool.fetchrow("SELECT status FROM security_review_stages WHERE id=$1", stage_id)
        status = row["status"] if row else None
        if status not in ("done", "failed"):
            # See the matching note in app/services/council/provisioning.py: the bare
            # sentence can't distinguish "the model's turn errored" from "it finished
            # normally but skipped the tool". Read the transcript tail and say which.
            reason = "agent run ended without calling submit_security_review"
            try:
                from app.services.agent_failure_reason import describe_last_agent_failure
                detail = await describe_last_agent_failure(session_key)
                if detail:
                    reason = f"{reason} — {detail}"
            except Exception as exc:  # noqa: BLE001
                log.info("security: could not describe failure for %s: %s", stage_id, exc)
            await _fail_stage(stage_id, reason)
            status = "failed"
        log.info("security: stage terminal run=%s aspect=%s instance=%s status=%s",
                 run["id"], aspect, instance_id, status)
        # See the matching note in app/services/council/provisioning.py: a stage that
        # needed nudges still reports 'done', so retry cost is invisible at run level.
        # Distinct greppable token, no schema change, silent on the happy path.
        if nudges_sent:
            log.warning(
                "security_stage_recovered_after_nudges=%d run=%s aspect=%s status=%s "
                "instance=%s — the reviewer did not submit on its first attempt; this "
                "run consumed %d extra model turn(s) despite its final status",
                nudges_sent, run["id"], aspect, status, instance_id, nudges_sent,
            )
        if created:
            from app.config import get_settings as _get_settings
            if getattr(_get_settings(), "security_keep_agent_workspaces", False):
                log.warning("security: KEEPING agent %s + workspace (stage %s) — debug flag set", instance_id, status)
            else:
                try:
                    await gateway_admin.delete_agent(instance_id, delete_files=True)
                    log.info("security: deleted agent %s + workspace (stage %s)", instance_id, status)
                except Exception as exc:
                    log.info("security: cleanup delete failed for %s: %s", instance_id, exc)

    return status == "done"
