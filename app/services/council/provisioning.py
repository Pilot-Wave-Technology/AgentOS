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

from app import local_dev
from app.database import get_pool
from app.openclaw import gateway_admin
from app.services.council.inputs import compose

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

def _safe_instance_id(stage: str, run_id: str) -> str:
    base = f"ra-{stage}-{run_id[:8]}".lower()
    return "".join(c if (c.isalnum() or c == "-") else "-" for c in base)


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
    workspace = local_dev.agent_workspace_path(instance_id)  # match GO's workspace path form

    try:
        docs = await compose.build_inputs(goal_id, stage, instance_id, stage_id)
    except Exception as exc:
        await _fail_stage(stage_id, f"input build failed: {exc}")
        return False

    await pool.execute(
        "UPDATE council_review_stages SET status='running', instance_id=$1, "
        "started_at=NOW(), claimed_at=NOW() WHERE id=$2",
        instance_id, stage_id,
    )

    created = False
    # Counted here (not inside the try) because `finally` reports it and the try can
    # exit early. See the recovery log in `finally`: a stage that only succeeds after
    # nudges still reports 'done', which hides real model failures behind a green run.
    nudges = 0
    # Bound before the try for the same reason — `finally` reads it to fetch the
    # transcript when explaining a failure, and the try can exit before its own
    # assignment below is reached. Deterministic from values already known here.
    session_key = f"agent:{instance_id}:{goal_id.lower()}-{stage_id}"
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
        for fn, content in {**bootstrap, **compose.bound_docs(docs)}.items():
            await gateway_admin.set_agent_file(instance_id, fn, content or "")

        # (session_key is bound above the try — see the note there.)
        kickoff = compose.build_kickoff_message(stage, goal_id, instance_id, stage_id, docs)
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
            #
            # That sentence alone can't distinguish "the model's turn errored" from
            # "the model finished normally but skipped the tool" — the ambiguity that
            # hid a ~1-in-3 provider failure rate. Read the transcript tail and say
            # which it was. Best-effort: never let this mask the original failure.
            reason = "agent run ended without calling submit_review"
            try:
                from app.services.agent_failure_reason import describe_last_agent_failure
                detail = await describe_last_agent_failure(session_key)
                if detail:
                    reason = f"{reason} — {detail}"
            except Exception as exc:  # noqa: BLE001
                log.info("council: could not describe failure for %s: %s", stage_id, exc)
            await _fail_stage(stage_id, reason)
            status = "failed"
        # A stage that needed nudges still ends 'done', so run-level health looks
        # perfect while model turns were actually burned. That is exactly how the
        # Gemini MALFORMED_FUNCTION_CALL problem stayed invisible until the gateway
        # was instrumented (research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md):
        # ~1 stage in 3 failed its tool call, and every run still reported green.
        # Emit a distinct, greppable token so the retry cost is measurable without
        # a schema change. Silent on the happy path (nudges == 0).
        if nudges:
            log.warning(
                "council_stage_recovered_after_nudges=%d goal=%s stage=%s status=%s "
                "instance=%s — the reviewer did not submit on its first attempt; this "
                "run consumed %d extra model turn(s) despite its final status",
                nudges, goal_id, stage, status, instance_id, nudges,
            )
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
                    # NOT FOUND MEANS ALREADY GONE, WHICH IS THE OUTCOME WE
                    # WANTED. Logging it as a failure sends someone looking
                    # for a leak that is not there -- the agent was reaped by
                    # a retry, a restart, or the gateway itself.
                    if "not found" in str(exc).lower():
                        log.info("council: reviewer agent %s was already gone",
                                 instance_id)
                    else:
                        log.warning("council: cleanup delete failed for %s: %s "
                                    "(the workspace will leak)", instance_id, exc)

    return done
