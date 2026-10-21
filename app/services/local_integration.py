"""Trunk moved under a local story waiting to land. Offer to catch it up.

THE CLOUD SWEEP CANNOT SEE THESE STORIES AND THAT IS NOT AN OVERSIGHT.
`trunk_integration.BEHIND_SQL` joins `kernel.executions` and parks the
run on a `plan_approval` question -- a local story has no execution, no
run and no park, so it matches nothing there and nothing there needs
changing. This is the same decision taken again in the only terms the
local lane has: a plan card, a person, and an editor.

ONE OFFER PER STORY, NOT PER LANE. A story writing two repos can be
behind on both, and they land as a group; two offers would be two plan
cards for one piece of work, with whichever was approved second
overwriting the first's pending plan.

AND IT UPDATES IN PLACE WHEN TRUNK MOVES AGAIN. The offer names the
shas it is catching up to, so an offer made against a sha nobody will
merge to any more is worse than no offer: the person approves bringing
in a position trunk has already left. Keyed on the shas, so a new
position rewrites the card rather than stacking a second one behind it.

ONLY A STORY WHOSE WORK IS PUSHED. `awaiting_merge` is the state, and
the reason is the same one the cloud gives: approving a plan REPLACES
the story's contract, so handing an integration plan to a story still
being written re-scopes it to the integration checks with no way back.
A branch still being written is not going anywhere anyway.
"""

from __future__ import annotations

from app.services.lane_sync_state import BEHIND_TRUNK_SQL

import logging

_log = logging.getLogger("app.local_integration")

# WHY `trunk_sha_seen_at > gn.created_at`: `trunk_sha` is a DETECTION
# CACHE refreshed when something happens to notice, not the live tip. A
# lane nobody has merged to for a week carries a week-old sha, and a
# branch cut yesterday reads as behind against it. Trunk can only have
# moved SINCE the node was cut.
BEHIND_SQL = f"""
    SELECT s.id AS story_id,
           s.goal_id AS goal_id,
           array_agg(DISTINCT gn.trunk_branch) AS lanes,
           array_agg(DISTINCT t.trunk_sha)     AS shas,
           array_agg(DISTINCT gr.owner || '/' || gr.name || chr(9)
                              || gn.trunk_branch || chr(9) || t.trunk_sha)
                                               AS targets
      FROM git_nodes gn
      JOIN git_repos gr ON gr.id = gn.git_repo_id
      JOIN git_node_stories gns ON gns.git_node_id = gn.id
      JOIN stories s ON s.id = gns.story_id
      JOIN git_repo_trunks t ON t.git_repo_id = gn.git_repo_id
                            AND t.trunk_branch = gn.trunk_branch
     WHERE COALESCE(s.execution_mode, '') = 'local'
       AND gn.state NOT IN ('MERGED', 'ABANDONED')
       AND t.trunk_sha IS NOT NULL
       AND {BEHIND_TRUNK_SQL}
       AND t.trunk_sha_seen_at > gn.created_at
       AND s.created_by <> 'engine'
       AND COALESCE(s.story_kind, 'code') = 'code'
       AND COALESCE(s.work_kind, 'story') <> 'errand'
       AND s.state = 'awaiting_merge'
     GROUP BY s.id, s.goal_id
     LIMIT 25
"""


async def sweep(pool) -> int:
    """Offer catch-up to every local story trunk has moved under."""
    offered = 0
    for row in await pool.fetch(BEHIND_SQL):
        try:
            if await offer_for(pool, dict(row)):
                offered += 1
        except Exception:                                   # noqa: BLE001
            _log.exception("could not offer the catch-up plan for story %s",
                           row["story_id"])
    return offered


def offer_hash(shas) -> str:
    """What makes two offers the same offer: the position being taken in."""
    return "trunk-integration-" + "+".join(sorted(shas or []))


async def offer_for(pool, row: dict) -> bool:
    """Write (or refresh) the catch-up plan card. True if the card changed."""
    from app.services.local_integration_targets import (  # noqa: PLC0415
        attach_mounts, carried_and_suite_checks)
    from app.services.trunk_integration_targets import parse_targets  # noqa: PLC0415
    from kernel.harness.integration_plan import canned_plan  # noqa: PLC0415

    story_id = row["story_id"]
    shas = sorted(row["shas"] or [])
    lanes = sorted(row["lanes"] or [])
    digest = offer_hash(shas)

    if await _already_offered(pool, story_id, digest):
        return False

    targets = await attach_mounts(pool, story_id,
                                  parse_targets(row.get("targets")))
    checks = await carried_and_suite_checks(pool, story_id, targets)
    plan = canned_plan(shas, lanes, targets=targets, checks=checks)
    await _park_the_offer(pool, story_id, plan, digest)
    _log.info("local story %s: catch-up plan awaiting approval (lanes: %s)",
              story_id, ", ".join(lanes))
    return True


async def _already_offered(pool, story_id: str, digest: str) -> bool:
    """Is this offer already in front of somebody, or already taken up?

    TWO QUESTIONS, AND THE SECOND ONE WAS MISSING -- which made this
    loop. Approval CLEARS `pending_plan_md`, so on the next tick the
    sweep saw nothing parked and offered again; the lane still read
    behind, because `last_synced_trunk_sha` is written by the ENGINE
    from GitHub's own compare and had not caught up yet. On ec6d5d1f
    that produced two approved catch-ups for one trunk move and a
    third parked behind them, each re-asking somebody who had already
    said yes.

    AN APPROVED, UNSPENT CATCH-UP IS AN OFFER ALREADY TAKEN UP. The
    work it describes has not been delivered -- `spend_cycle` marks it
    `executed` at the submit -- so there is nothing a second one could
    add. The cloud never had this: its dedupe is the question's
    `content_hash`, which survives being answered.
    """
    if await pool.fetchval(
            "SELECT 1 FROM plan_versions WHERE story_id = $1 "
            "   AND COALESCE(kind, 'work') = 'integration' "
            "   AND status = 'approved' LIMIT 1", story_id):
        return True

    from app.services.local_report import latest_card  # noqa: PLC0415

    pending = await pool.fetchval(
        "SELECT pending_plan_md FROM stories WHERE id = $1", story_id)
    if not pending:
        return False
    card = await latest_card(pool, story_id, "plan_review")
    if isinstance(card, str):
        import json  # noqa: PLC0415
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    return bool(card) and (card or {}).get("plan_hash") == digest


async def _park_the_offer(pool, story_id: str, plan: dict, digest: str) -> None:
    """One card, one pending plan, one phase move -- the submit path's shape.

    NOT THROUGH `submit_local_plan`, deliberately. That function is the
    AGENT's door: it validates prose a model wrote and refuses it in
    prose a person reads. This plan is written by the platform from a
    template, there is nobody to refuse it to, and routing it through
    the agent's validator would let a change to those rules silently
    stop the platform catching stories up.
    """
    from app.services import local_plan  # noqa: PLC0415
    from app.services.local_report import report_message  # noqa: PLC0415

    steps = [str(x) for x in plan.get("steps") or []]
    checks = local_plan.normalise_checks(plan.get("checks") or [])
    plan_md = local_plan.as_markdown(steps, plan.get("rationale") or "", checks)
    path = await integration_plan_path(pool, story_id)

    await pool.execute(
        "UPDATE stories SET pending_plan_md = $1, pending_plan_path = $2, "
        "       current_phase = 'awaiting_plan_approval', updated_at = NOW() "
        " WHERE id = $3", plan_md, path, story_id)
    await report_message(
        story_id, "agent", plan.get("question") or "Trunk moved under this branch.",
        card_data={"kind": "plan_review", "plan_path": path,
                   # THE CARD SAYS WHAT KIND OF PLAN IT IS, and approval
                   # reads it: an integration plan does not consume a work
                   # cycle and lands the story in `integrating`, not
                   # `executing`. Without this the approval path cannot
                   # tell the platform's plan from the agent's.
                   "plan_kind": "integration",
                   "steps": steps,
                   "checks": [{"run": c.get("run"), "label": c.get("label")}
                              for c in checks],
                   "plan_hash": digest})


async def integration_plan_path(pool, story_id: str) -> str:
    """`agentos_plans/<story>/plan-NN-trunk.md`, beside the cycle it serves.

    NOT `plan-NN.md`, which is the WORK plan of this cycle: that file is
    on the branch and in the pull request, and overwriting it would
    rewrite the record of what was agreed with a machine-written catch-up.

    NOT `plan-NN+1.md` either. Catching up is not a new intent -- it
    takes trunk in and re-runs the checks -- and consuming a cycle number
    would renumber the plan files underneath a person mid-story.
    """
    from app.services.local_cycles import PLAN_FOLDER, current_cycle  # noqa: PLC0415

    return "{}/{}/plan-{:02d}-trunk.md".format(
        PLAN_FOLDER, story_id, await current_cycle(pool, story_id))
