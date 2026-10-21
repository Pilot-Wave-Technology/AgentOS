"""Trunk moved under a story that is waiting to land. Catch it up.

WHAT USED TO HAPPEN. The engine cut a separate diff story to merge the
lane in. That made sense when the feature story was already `done` and
its workspace collected — there was nothing left to do the work in. It
is no longer true: a coding story holds its pod until its work lands,
so the story that owns the branch is the one that catches it up.

A PERSON STILL APPROVES IT, and that is a correction to how this was
first built. The plan was registered AND answered by the platform, on
the reasoning that catching up is the same three steps every time and
asking somebody to re-read them teaches approving without reading.

That traded a concrete risk for a speculative one. The canned plan runs
THE STORY'S EXISTING CHECKS — the ones written before trunk moved — and
integration is exactly the case where those can be insufficient:
somebody changed a contract, renamed a function, moved a file. The
person often knows "if X lands, also run Y", and under the old shape
they were never asked. Rubber-stamping is a slow erosion; shipping an
unverified integration is an immediate correctness risk on trunk.

So the platform still COMPOSES the plan — nobody has to write it — and
a person approves it, or revises it to add the checks they want. It now
carries the story's own checks AND each merged repository's full suite,
and one approval covers resolving a conflict too
(`kernel/harness/integration_plan.py`).

ONLY A STORY THAT HAS FINISHED AND IS WAITING TO LAND. Never one still
being written: `plan_scope` reads the newest approved plan, so handing
an integration plan to an executor mid-work re-scopes it to the
integration tools and replaces its contract with the integration
checks, for the rest of the run, with no way back. And there is no hurry — a branch still being
written is not going anywhere until it is pushed.
"""

from __future__ import annotations

from app.services.lane_sync_state import BEHIND_TRUNK_SQL

import logging

_log = logging.getLogger("app.trunk_integration")

# ONE ROW PER STORY, NOT PER NODE, and that is the multi-repo fix. A
# story writing two repos can be behind on both; per node it produced
# two rows, and the second would be refused by `questions_one_open` --
# leaving that lane silently un-integrated. One plan names every lane
# it is catching up.
#
# `trunk_sha_seen_at > gn.created_at` because `trunk_sha` is a DETECTION
# CACHE, refreshed when something happens to notice, not the live tip. A
# lane nobody has merged to for a week carries a week-old sha, and a
# branch cut yesterday reads as behind against it. Trunk can only have
# moved SINCE the node was cut.
BEHIND_SQL = f"""
    SELECT e.id         AS execution_id,
           e.generation AS generation,
           s.id         AS story_id,
           array_agg(DISTINCT gn.trunk_branch) AS lanes,
           array_agg(DISTINCT t.trunk_sha)     AS shas,
           -- PAIRED, so a plan can say which sha is which repo's lane.
           -- See trunk_integration_targets.parse_targets.
           array_agg(DISTINCT gr.owner || '/' || gr.name || chr(9)
                              || gn.trunk_branch || chr(9) || t.trunk_sha)
                                               AS targets
      FROM git_nodes gn
      JOIN git_repos gr ON gr.id = gn.git_repo_id
      JOIN git_node_stories gns ON gns.git_node_id = gn.id
      JOIN stories s ON s.id = gns.story_id
      JOIN kernel.executions e ON e.story_id = s.id
      JOIN git_repo_trunks t ON t.git_repo_id = gn.git_repo_id
                            AND t.trunk_branch = gn.trunk_branch
     WHERE gn.state NOT IN ('MERGED', 'ABANDONED')
       AND t.trunk_sha IS NOT NULL
       AND {BEHIND_TRUNK_SQL}
       AND t.trunk_sha_seen_at > gn.created_at
       AND s.created_by <> 'engine'
       AND COALESCE(s.story_kind, 'code') = 'code'
       AND COALESCE(s.work_kind, 'story') <> 'errand'
       AND s.state = 'awaiting_merge'
       AND e.state IN ('PARKED_HOT', 'PARKED_COLD')
     GROUP BY e.id, e.generation, s.id
     LIMIT 25
"""


def _enabled() -> bool:
    from kernel.worker.landing import holds_for_merge_enabled
    return holds_for_merge_enabled()


async def sweep(pool) -> int:
    """Offer an integration plan to every story trunk has moved under."""
    if not _enabled():
        return 0
    asked = 0
    for row in await pool.fetch(BEHIND_SQL):
        try:
            if await register_for(pool, dict(row)):
                asked += 1
        except Exception:                                   # noqa: BLE001
            _log.exception("could not offer the integration plan for "
                           "execution %s", row["execution_id"])
    return asked


async def register_for(pool, row: dict) -> bool:
    """Park the run on an integration plan. True if newly asked."""
    from app.services.story_landing import _kernel_conn
    from kernel.harness.integration_plan import canned_plan
    from kernel.harness.park_open import open_request

    lanes = sorted(row["lanes"] or [])
    shas = sorted(row["shas"] or [])
    # KEYED ON THE TRUNK STATE: one plan per distinct position of the
    # lanes this story lands on. Without it the sweep re-offers every
    # poll -- `ask_question` would dedupe the question, but the landing
    # park would be retired again each time, and on a4098b67 one of
    # those retirements raced the person's merge. That merge then found
    # no park to answer, opened no closeout, and the story finished
    # with nobody asked and no review.
    content_hash = (f"trunk-integration-{row['execution_id']}-"
                    + "+".join(shas))

    conn = await _kernel_conn()
    try:
        if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM kernel.questions "
                " WHERE execution_id = $1 AND kind = 'authorization' "
                "   AND content_hash = $2)",
                row["execution_id"], content_hash):
            return False

        # WHAT THE PLAN NAMES AND RUNS, resolved before anything is
        # written: each lane paired with its repo, sha and mount, the
        # story's command checks carried forward, each mount's suite.
        from app.services.trunk_integration_targets import (
            attach_mounts, carried_and_suite_checks, parse_targets)
        targets = await attach_mounts(
            conn, row["execution_id"], parse_targets(row.get("targets")))
        checks = await carried_and_suite_checks(
            conn, row["execution_id"], targets)

        async with conn.transaction():
            # THE LANDING PARK GOES FIRST, and it has to:
            # `questions_one_open` is UNIQUE per execution on
            # status='OPEN', so the plan cannot be inserted while the
            # merge question is still open. It is also honest -- what
            # the person was asked to merge is about to change.
            await conn.execute(
                "UPDATE kernel.questions "
                "   SET status = 'ANSWERED', verdict = 'revise', "
                "       answer_text = $2, answered_at = now(), "
                "       answered_by = 'platform:trunk-integration' "
                " WHERE execution_id = $1 AND kind = 'landing' "
                "   AND status = 'OPEN'",
                row["execution_id"],
                "Trunk moved under this branch. You are being asked to "
                "approve bringing it up to date, and then to merge again.")

            # OPEN, NOT ANSWERED, which is the whole correction.
            # `open_request` parks the run on it and the card poller
            # puts it in front of a person: the platform writes the
            # plan, a person approves it or adds the checks they want.
            # Approving is what un-parks the run, through the ordinary
            # `park_resolve` path -- nothing here wakes it by hand.
            # `plan_approval`, NOT `authorization`. `_KIND` maps the
            # CALLER's word to the stored kind, and "authorization" is
            # the stored side -- passing it falls through to the
            # "clarification" default, which then hits the evidence
            # floor and comes back E_ASK_PREMATURE.
            got = await open_request(
                conn, row["execution_id"], row["generation"],
                "plan_approval",
                canned_plan(shas, lanes, targets=targets, checks=checks),
                content_hash=content_hash)
            if not isinstance(got, int):
                _log.warning("execution %s: integration plan refused (%s)",
                             row["execution_id"], getattr(got, "code", got))
                return False
    finally:
        await conn.close()

    _log.info("execution %s: integration plan awaiting approval (lanes: %s)",
              row["execution_id"], ", ".join(lanes))
    return True


# THE OTHER HALF OF THE SAME DECISION, and it lives here so the two
# cannot drift. When a story holds its own pod until the work lands,
# the engine must stop cutting `ds_*` integration stories against the
# same node: on bc9679f4 both fired, so one behind branch produced an
# integration plan on the story AND two separate engine stories with
# their own assistant threads -- invisible on the board (filtered as
# engine git stories) and visible in the thread list, which is exactly
# how a person finds out.
#
# NOT A BLANKET SWITCH. A node whose story is already `done` -- cut
# before the flag, or an errand, or a story somebody cancelled -- has
# no pod left to integrate with, and for those the engine's diff story
# is still the only thing that can do it. So the question is per node:
# is there a live story here that will do it itself?
_SELF_INTEGRATES_SQL = """
    SELECT 1
      FROM git_node_stories gns
      JOIN stories s ON s.id = gns.story_id
     WHERE gns.git_node_id = $1
       AND s.created_by <> 'engine'
       AND COALESCE(s.story_kind, 'code') = 'code'
       AND COALESCE(s.work_kind, 'story') <> 'errand'
       AND s.state IN ('in_progress', 'blocked', 'awaiting_merge')
     LIMIT 1
"""


async def node_self_integrates(pool, node_id: str) -> bool:
    """True when this node's own story will catch trunk up, so the
    engine should not cut a diff story for it."""
    if not _enabled():
        return False
    return bool(await pool.fetchval(_SELF_INTEGRATES_SQL, node_id))


# AN ERRAND'S BRANCH IS NEVER CAUGHT UP BY ANYBODY ELSE. `_SELF_INTEGRATES_
# SQL` excludes errands -- an errand is never offered the integration plan
# -- and that exclusion is exactly what used to make the engine cut a
# separate diff story for an errand branch every time its trunk moved.
# There should be no sync story for an errand at all, so the engine asks
# this before it would cut one. Not gated on the hold-for-merge flag: this
# is not about who integrates, it is that nothing does.
_ERRAND_NODE_SQL = """
    SELECT 1
      FROM git_node_stories gns
      JOIN stories s ON s.id = gns.story_id
     WHERE gns.git_node_id = $1
       AND (COALESCE(s.work_kind, 'story') = 'errand'
            -- the run decides too: see merge_lane's errand gate
            OR (SELECT e.kind FROM kernel.executions e
                 WHERE e.story_id = s.id
                 ORDER BY e.id DESC LIMIT 1) = 'errand')
     LIMIT 1
"""


async def node_is_errand(pool, node_id: str) -> bool:
    """True when this node belongs to an errand: no diff story for it."""
    return bool(await pool.fetchval(_ERRAND_NODE_SQL, node_id))


async def record_caught_up(pool, node_id: str, trunk_branch: str,
                           trunk_sha: str) -> None:
    """The branch is level with trunk. Write that down.

    WHY THE ENGINE AND NOT THE POD. `sync_trunk` used to record this
    itself, straight after merging, and it never once succeeded: a leg
    runs as `agentos_leg_dev`, which has no write on the git graph, so
    every sync logged `permission denied for table git_nodes` into a
    best-effort try/except and carried on.

    Granting it would have been worse than the bug. The column decides
    whether a story is OFFERED integration, so an executor able to
    write it could mark itself caught up without merging and land work
    that never took trunk in. Here it is derived from GitHub's own
    `compare` -- `behind_by == 0` -- which no executor can forge.

    Best-effort: this is bookkeeping that makes the sweep quiet, and
    `BEHIND_SQL` deduplicates on the trunk shas anyway, so failing to
    write it costs a redundant check, never a wrong landing.
    """
    try:
        await pool.execute(
            "UPDATE git_nodes SET last_synced_trunk_sha = $2, updated_at = now() "
            " WHERE id = $1 AND trunk_branch = $3 "
            "   AND state NOT IN ('MERGED', 'ABANDONED') "
            "   AND last_synced_trunk_sha IS DISTINCT FROM $2",
            node_id, trunk_sha, trunk_branch)
    except Exception:                                       # noqa: BLE001
        _log.exception("could not record node %s as caught up to %s",
                       node_id, trunk_sha[:12])
