"""The grind loop (spec §5/§6) — drive each ACTIVE lease holder toward a clean,
mergeable state by spawning engine-authored diff/fix stories, without ever merging
(a human merges on the PR; detection finalizes).

For each active lease holder (its (repo,trunk) slot), live-read the trunk and
`compare`:
  * behind_by > 0  → ensure a DIFF story exists (a normal `code` story, created_by
    ='engine', in the node's merge group): "sync trunk into the branch + resolve".
    NOT when the node's own story is still alive and holding its pod for the merge
    -- that story integrates trunk itself (app/services/trunk_integration.py), and
    cutting a diff story as well gives one behind branch two integrations.
  * behind_by == 0 → the branch is current; it's ready for the merge story to open
    the PR (build §7 opens it; this step just reports "ready").

KEY (spec §0): the diff/fix → merge edge only SEQUENCES dispatch; the merge is
authorized by LIVE git truth (behind_by==0 [+ checks green]), never by the edge
state. So a cancelled/abandoned diff cannot prematurely let the merge proceed —
the next grind pass re-`compare`s and re-spawns if still behind.

diff/fix stories are `code` (same Claude phases, different brief) marked
created_by='engine' + merge_group_id so plan derivation / GC / council EXCLUDE them
(they're not goal work). Idempotent: a diff id is keyed on (node, trunk_sha) so a
same-trunk re-detect is a no-op; an external push (new trunk_sha) supersedes the
prior in-flight diff and spawns a fresh one (only one diff in flight per node).

The CONSUMER that dispatches a spawned diff story to an assistant + opens the PR on
behind_by==0 is build §7; the CI-red → fix-story path needs the PR's checks (§7).
Step 6 builds the behind→diff drive + the engine-authored wiring; until §7
dispatches it, a spawned diff sits 'proposed'.
"""
from __future__ import annotations

import hashlib
import logging

from app.database import get_pool
from app.ledger.journal import append_event
from app.services.operator_provisioning import repo_clone_subdir
from app.services.trunk_integration import (
    node_is_errand,
    node_self_integrates,
    record_caught_up,
)

from . import github_ops as gh
from .dispatch import notify_story_branch_ready
from .merge_lane import _finalize_merged
from .node_errors import clear_node_error, record_node_github_failure
from .reconcile import _SYSTEM_ACTOR_ID

_log = logging.getLogger(__name__)

# Read-only for the sync-check (compare + trunk read), the CI gate, and the PR
# review-state read. The installation token only carries permissions requested at
# mint time (intersected with what the App is granted), so each endpoint's perm MUST
# be listed or it 403s even when the App has it:
#   checks:read        → GET /commits/{ref}/check-runs (the CI gate)
#   pull_requests:read → get_merge_state_status (the approved-PR / in-merge-queue
#                        restack guard); without it that read silently degrades to
#                        "not approved" and could restack+dismiss a human approval.
_GRIND_PERMS = {"contents": "read", "metadata": "read", "checks": "read",
                "pull_requests": "read"}
# Opening the PR needs pull_requests:write (the App re-consent prerequisite).
_PR_OPEN_PERMS = {"contents": "read", "metadata": "read", "pull_requests": "write"}
# Distinct fix attempts (per head sha) before a perpetually-red PR is escalated.
_MAX_FIX_ATTEMPTS = 5

# The `mergeStateStatus` values that mean THIS BRANCH'S STALENESS is what
# stops the merge, and therefore the only ones worth an integrate story once
# a PR is open. Everything else -- CLEAN, UNSTABLE, BLOCKED, DRAFT,
# HAS_HOOKS, UNKNOWN -- is waiting on review, on checks, or on nothing.
# See the long note at the call site in `_grind_one`.
_STALENESS_BLOCKS_MERGE = ("DIRTY", "BEHIND")


def _diff_story_id(node_id: str, trunk_sha: str) -> str:
    """Deterministic, idempotent id keyed on (node, trunk_sha) — a same-trunk
    re-detect maps to the same story (no duplicate); a new trunk_sha is a new diff."""
    return "ds_" + hashlib.sha256(f"{node_id}:{trunk_sha}:diff".encode()).hexdigest()[:14]


def _fix_story_id(node_id: str, head_sha: str) -> str:
    """Idempotent id keyed on (node, head_sha) — a flaky check on the SAME head maps
    to the same fix story (no churn); a new push (head_sha changes) is a new fix."""
    return "fs_" + hashlib.sha256(f"{node_id}:{head_sha}:fix".encode()).hexdigest()[:14]


async def run_grind_pass() -> dict:
    """For each active lease holder, sync-check its branch vs live trunk and spawn a
    diff story if behind. Leader-only, short-lived pool calls (no conn held across
    GitHub I/O)."""
    out = {"synced": 0, "diffs_spawned": 0, "fixes_spawned": 0, "prs_opened": 0,
           "noop_merged": 0, "escalated": 0, "ready": 0}
    pool = get_pool()
    holders = await pool.fetch(
        "SELECT q.node_id, n.git_repo_id, n.goal_id, n.branch_name, n.trunk_branch, "
        "       n.pr_number, r.owner, r.name "
        "FROM git_merge_queue q "
        "JOIN git_nodes n ON n.id = q.node_id "
        "JOIN git_repos r ON r.id = n.git_repo_id "
        "WHERE q.status = 'active' "
        "  AND n.state NOT IN ('MERGED', 'ABANDONED', 'ESCALATED')"  # don't grind a settled holder
    )
    for h in holders:
        try:
            await _grind_one(dict(h), out)
        except Exception as exc:
            # Surface EVERY grind GitHub failure on the node (UI error column) instead
            # of only logging: an auth/permission failure (401/403/404) escalates +
            # holds the lane; a transient one is recorded + retried. Covers the
            # trunk/compare/server-merge/PR-open/checks/ref reads in _grind_one.
            res = await record_node_github_failure(h["node_id"], exc, context="grind")
            if res == "escalated":
                out["escalated"] += 1
            _log.exception("git-engine: grind failed for node %s (%s)", h["node_id"], res)
    return out


async def _escalate_node(node_id: str, reason: str) -> None:
    """Escalate a node OUT of the grind + free its lease slot (one txn, so a crash
    can't leave an ESCALATED node holding the active slot — the zombie sweep self-heals
    regardless). promote_pending then advances the line."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE git_nodes SET state = 'ESCALATED', last_error = $2, updated_at = now() "
                "WHERE id = $1 AND state NOT IN ('MERGED', 'ABANDONED')",
                node_id, reason[:500])
            await conn.execute(
                "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1", node_id)


async def _grind_one(h: dict, out: dict) -> None:
    owner, name = h["owner"], h["name"]
    trunk_branch = h["trunk_branch"] or "main"
    branch = h["branch_name"]
    # GitHub I/O first (no DB conn held).
    token = await gh.mint_repo_token(name, _GRIND_PERMS)

    # §3 pre-flight protection gate (read once; reused for the required-checks scope).
    try:
        prot = await gh.get_effective_protection(token, owner, name, trunk_branch)
    except Exception:
        prot = {}
    if prot.get("required_signatures"):
        # Signing is DEFERRED in v1 (§0/§3.2): the assistant's pushes are UNSIGNED and a
        # required-signing trunk would reject them forever → escalate, don't grind.
        await _escalate_node(
            h["node_id"],
            f"trunk {trunk_branch} requires SIGNED commits (deferred in v1) — the engine "
            f"cannot land it; reconfigure the trunk or enable gateway commit signing")
        out["escalated"] += 1
        return
    if prot and not prot.get("manageable", False):
        # No require-up-to-date + required check = the no-stale-merge backbone is missing
        # (§3.1). We warn rather than refuse (many dev trunks intentionally lack strict
        # checks); a stale merge is still bounded by GitHub's own behind/required-review.
        _log.warning("git-engine: trunk %s/%s %s lacks require-up-to-date + a required "
                     "check (no-stale-merge backbone, §3.1) — managing anyway",
                     owner, name, trunk_branch)

    trunk_sha = await gh.get_trunk_sha(token, owner, name, trunk_branch)
    cmp = await gh.compare(token, owner, name, trunk_sha, branch)

    # TOCTOU guard (web vs engine): the active-holder snapshot was read at the top of
    # run_grind_pass, but a web-tier "Cancel lane" runs concurrently and can ABANDON this
    # node + park its lease in between. Re-assert it still holds the active lease before
    # any GitHub WRITE (open PR) or assistant story spawn, so we never act on a node a
    # human just cancelled. (Reads above are harmless; the writes below are what matter.)
    if not await get_pool().fetchval(
        "SELECT 1 FROM git_merge_queue q JOIN git_nodes n ON n.id = q.node_id "
        "WHERE q.node_id = $1 AND q.status = 'active' "
        "  AND n.state NOT IN ('ABANDONED', 'MERGED', 'ESCALATED')",
        h["node_id"],
    ):
        return

    # LEVEL WITH TRUNK, SO SAY SO. GitHub's own compare is the evidence,
    # and it is the only evidence anybody should accept: the pod that
    # ran the merge used to write this column itself and was refused by
    # Postgres on every attempt (`agentos_leg_dev` has no write on the
    # git graph) -- correctly, because an executor able to declare
    # itself caught up could land work that never took trunk in.
    #
    # Written before the branches below, so it covers the branch that
    # is ready for its PR AND the one already contained in trunk.
    if cmp["behind_by"] == 0:
        await record_caught_up(get_pool(), h["node_id"], trunk_branch, trunk_sha)

    if cmp["behind_by"] > 0 and cmp["ahead_by"] > 0:
        # Trunk has MOVED under this branch AND the branch still has its own un-merged
        # work → hand integration to the assistant via a diff story — never an engine-
        # side server merge. A clean *textual* merge is NOT proof the code is still
        # correct: trunk may have changed an API, a signature, a shared file, or a
        # contract this branch depends on, which git merges without conflict but leaves
        # semantically broken. The assistant must merge trunk in, RECONCILE its work
        # with the new trunk, and run the tests — work the engine can't do. (Idempotent:
        # one diff story per trunk_sha; a fresh trunk movement supersedes it.)
        #
        # The `ahead_by > 0` gate is essential: the instant a human MERGES this branch's
        # PR, trunk advances to the merge commit and the branch reads "behind_by >= 1"
        # (its own merge commit) with ahead_by == 0 — it carries nothing trunk lacks.
        # Without this gate, grind racing detection (grind ticks before detection
        # finalizes the node) spawns a SPURIOUS integrate story for work that JUST
        # landed. ahead_by == 0 means "fully contained in trunk" → fall through to the
        # no-op/merged finalize below, never a diff story.
        if h.get("pr_number"):
            # FIRST: if the PR already MERGED/closed, the branch has LANDED (or been
            # cancelled) — do NOT spawn a diff story; let detection finalize the node.
            # Covers the SQUASH/REBASE merge race the ahead_by gate misses: those leave
            # the branch's commits as non-ancestors of the new trunk, so ahead_by stays
            # > 0 even though the branch just merged. `merged` is completion truth
            # (verify-by-read), not SHA reachability.
            try:
                pr = await gh.get_pr(token, owner, name, h["pr_number"])
            except Exception:
                pr = {}
            if pr.get("merged") or pr.get("state") == "closed":
                out["ready"] += 1
                return
            try:
                ms = await gh.get_merge_state_status(token, owner, name, h["pr_number"])
            except Exception:
                ms = {}
            # Don't disrupt a PR a human has already APPROVED or that's IN THE MERGE
            # QUEUE (a new push dismisses the approval / bumps it out). Leave it for the
            # human to "Update branch" + re-review; GitHub surfaces the out-of-date banner.
            if ms.get("review_decision") == "APPROVED" or ms.get("in_merge_queue"):
                out["ready"] += 1
                return
            # AN OPEN PR IS INTEGRATED ONLY WHEN STALENESS IS WHAT BLOCKS IT.
            #
            # `behind_by > 0 and ahead_by > 0` is true of EVERY open PR the
            # moment anyone else lands on trunk, so on an actively-developed
            # trunk this spawned one integrate story per upstream commit, for
            # as long as the PR stayed open. Measured on GOAL1/next: PR #82
            # against `develop` produced runs 416, 418, 419, 421
            # and 424 over thirteen hours, each one landing within the hour of
            # a push to dev, each one completing, each one obsoleted by the
            # next push. The cost is linear in how long a human takes to
            # review.
            #
            # `mergeStateStatus` is GitHub's own answer to "can this merge",
            # and it already arrives in `ms` -- no extra call. Only two of its
            # values mean the branch's own staleness is the obstacle:
            #
            #   DIRTY   a real conflict with trunk -- a person must resolve it
            #   BEHIND  the base requires up-to-date and this is not
            #
            # CLEAN, UNSTABLE, BLOCKED and DRAFT all mean the merge is not
            # waiting on staleness: it is waiting on review, on checks, or on
            # nothing. Integrating those produces a merge commit nobody needed
            # and a story nobody asked for.
            #
            # UNKNOWN is GitHub still computing, and it skips too: acting on it
            # is how the churn gets reintroduced by a transient read. The next
            # tick asks again.
            #
            # AN EMPTY `ms` FALLS THROUGH DELIBERATELY. That is the shape of a
            # FAILED `get_merge_state_status` (it is wrapped in try/except
            # above), and a GitHub outage must not silently stop integration
            # for every story -- it falls back to the behind/ahead judgement
            # that was the whole rule until now.
            state = (ms.get("merge_state_status") or "").upper()
            if state and state not in _STALENESS_BLOCKS_MERGE:
                out["ready"] += 1
                return
        # THE STORY DOES ITS OWN INTEGRATION NOW, when it is still alive to do it.
        # A coding story holds its pod from push until its PR merges, so the branch
        # that is behind belongs to a run that is parked on the merge card right
        # now -- `trunk_integration.sweep` offers that run an integration plan, a
        # person approves it, and the same executor that wrote the branch brings
        # trunk in. Cutting a diff story as well gives one behind branch two
        # integrations and a stray assistant thread nobody asked for.
        if await node_self_integrates(get_pool(), h["node_id"]):
            _log.info("git-engine: node %s is behind but its own story integrates "
                      "it -- no diff story", h["node_id"])
            return
        # AND NEVER FOR AN ERRAND: see `trunk_integration.node_is_errand`.
        if await node_is_errand(get_pool(), h["node_id"]):
            _log.info("git-engine: node %s is behind but belongs to an errand "
                      "-- no diff story", h["node_id"])
            return
        if await _ensure_diff_story(h, trunk_sha):   # assistant integrates + reconciles + tests
            out["diffs_spawned"] += 1
        return
    # Branch carries nothing trunk lacks (ahead_by == 0) — either an empty branch, or
    # one that's already FULLY MERGED into trunk (behind only by its own merge commit
    # after a human merged the PR). THOSE ARE NOT THE SAME THING, and treating them
    # the same is how the first kernel story "landed" without a byte reaching GitHub:
    # its seven commits sat unpushed in the sandbox, the remote branch still equalled
    # the engine's cut point, ahead_by read 0, and this path marked the node MERGED
    # and told the GO the work was on trunk.
    #
    # The discriminator is the branch tip itself: a branch that was merged MOVED
    # (its tip is not the cut point); a branch that never received a push did not.
    # Never-moved + a rider claiming completed work = the work is stranded somewhere
    # else — ESCALATE with the reason instead of certifying a landing.
    if cmp["ahead_by"] == 0:
        never_moved = False
        try:
            tip = await gh.get_ref_sha(token, owner, name, branch)
            base = await get_pool().fetchval(
                "SELECT base_sha FROM git_nodes WHERE id = $1", h["node_id"])
            never_moved = bool(tip and base and tip == base)
        except Exception:
            never_moved = False  # can't prove it; the old behaviour stands
        if never_moved:
            # A BRANCH THAT NEVER MOVED IS NOT A LANDING, and what it IS
            # depends entirely on whether its work is finished.
            #
            # STILL IN FLIGHT: the executor simply has not pushed yet --
            # the ordinary state of every story between the cut and the
            # first push. Finalising here marks the node MERGED while
            # the work is being written, so the eventual push has no
            # live node, opens no PR, and lands nowhere. Seen live on a
            # two-lane story: both branches "merged" minutes after being
            # cut, with the run still RUNNING.
            live_rider = await get_pool().fetchval(
                "SELECT s.id FROM git_node_stories gns "
                "JOIN stories s ON s.id = gns.story_id "
                "WHERE gns.git_node_id = $1 "
                "AND s.state IN ('proposed', 'accepted', 'in_progress', "
                "                'blocked') LIMIT 1",
                h["node_id"])
            if live_rider:
                out["awaiting_push"] = out.get("awaiting_push", 0) + 1
                return
            done_rider = await get_pool().fetchval(
                "SELECT s.id FROM git_node_stories gns "
                "JOIN stories s ON s.id = gns.story_id "
                "WHERE gns.git_node_id = $1 "
                "AND s.state IN ('done', 'reviewed', 'awaiting_merge') LIMIT 1",
                h["node_id"])
            if done_rider:
                pool = get_pool()
                await pool.execute(
                    "UPDATE git_nodes SET state = 'ESCALATED', "
                    "last_error = $2, updated_at = now() WHERE id = $1 "
                    "AND state NOT IN ('MERGED', 'ABANDONED')",
                    h["node_id"],
                    f"story {done_rider} is done but branch {branch} has "
                    f"no commits — the work was never pushed. Nothing was "
                    f"merged; nothing will be reported as landed.")
                out["escalated"] = out.get("escalated", 0) + 1
                return
        await _finalize_merged(h["git_repo_id"], h["node_id"], trunk_sha,
                               token, owner, name, trunk_branch)
        out["noop_merged"] += 1
        return
    # Ahead + up-to-date → ensure a PR is open. The HUMAN reviews + merges it;
    # detection finalizes. PR-open is idempotent (open_or_get_pr).
    if not h.get("pr_number"):
        if await _ensure_pr_open(h, trunk_branch):
            out["prs_opened"] += 1
            await clear_node_error(get_pool(), h["node_id"])  # recovered → clear stale error
        return
    # PR already open → check CI. A failing REQUIRED check spawns a fix story
    # (dispatched to the assignee's SHCA); idempotent by head sha so a flaky check
    # doesn't churn. Advisory (non-required) failures don't block merge → ignored.
    #
    # The CI gate must NOT be skipped on an auth/permission failure: an unreadable
    # checks API means we can't prove CI is green. get_check_runs raises on non-2xx
    # and the run_grind_pass classifier turns a 401/403/404 into ESCALATED+last_error
    # (held, visible) rather than degrading to "no failures" (which could land
    # CI-unverified code); a transient error is recorded + retried. So a plain call
    # here is correct — do NOT swallow it.
    checks = await gh.get_check_runs(token, owner, name, branch)
    failed = checks["failed"]
    if failed:
        # Scope to REQUIRED checks (the only ones that block the human merge) using the
        # protection we already read above. No required checks → don't auto-disrupt.
        required = set(prot.get("required_checks") or [])
        failed = [f for f in failed if f in required]
    if failed:
        pool = get_pool()
        attempts = await pool.fetchval(
            "SELECT repark_count FROM git_nodes WHERE id = $1", h["node_id"]) or 0
        if attempts >= _MAX_FIX_ATTEMPTS:
            # Perpetually-red CI — stop spawning fix stories; escalate + free the slot.
            await _escalate_node(
                h["node_id"], f"CI still failing after {attempts} fix attempts: "
                              f"{', '.join(str(f) for f in failed[:4])}")
            out["escalated"] += 1
            return
        head_sha = await gh.get_ref_sha(token, owner, name, branch) or ""
        if head_sha and await _ensure_fix_story(h, head_sha, failed):
            await pool.execute(
                "UPDATE git_nodes SET repark_count = repark_count + 1 WHERE id = $1", h["node_id"])
            out["fixes_spawned"] += 1
    else:
        out["ready"] += 1   # PR open + no required failure → awaiting the human merge
        # Checks are green for this head → clear the stale error AND reset the fix-attempt
        # counter, so _MAX_FIX_ATTEMPTS counts CONSECUTIVE unresolved failures, not lifetime
        # pushes (a branch that legitimately needed >5 separate fixes over its life would
        # otherwise be falsely escalated into the no-exit ESCALATED state).
        await get_pool().execute(
            "UPDATE git_nodes SET last_error = NULL, repark_count = 0, updated_at = now() "
            "WHERE id = $1 AND (last_error IS NOT NULL OR repark_count <> 0)",
            h["node_id"])


async def _spawn_engine_code_story(
    h: dict, sid: str, title: str, brief: str, thread_label: str, kind_tag: str,
) -> bool:
    """Spawn (idempotently) an engine-authored `code` story (a diff or fix) for the
    active holder, wire it to the merge story, and NOTIFY the web tier to dispatch it
    to the assignee's SHCA. Returns True iff a NEW story was created.

    `kind_tag` ∈ {'diff','fix'} (ledger only). Only ONE engine story is in flight per
    node at a time — spawning a new one supersedes any prior non-terminal engine code
    story on the group (a fresh trunk_sha / head_sha means the old one is stale)."""
    node_id = h["node_id"]
    goal_id = h["goal_id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM stories WHERE id = $1", sid):
                return False  # this exact (node, sha, kind) story already exists
            # Deterministic group choice. Reconcile prunes superseded memberships, so a
            # node should be in exactly one group — but a diff/fix story must not bind
            # to a stale group in the window before that prune runs. Prefer a group
            # whose merge story is still live, then order by id so the pick can never
            # vary between two otherwise-identical passes.
            grp = await conn.fetchrow(
                "SELECT gmn.merge_group_id FROM git_merge_group_nodes gmn "
                "  LEFT JOIN stories s ON s.merge_group_id = gmn.merge_group_id "
                "                     AND s.story_kind = 'merge' "
                " WHERE gmn.git_node_id = $1 "
                " ORDER BY (s.id IS NOT NULL "
                "           AND s.state NOT IN ('cancelled', 'rejected', 'done')) DESC, "
                "          gmn.merge_group_id ASC "
                " LIMIT 1",
                node_id,
            )
            if grp is None:
                return False
            merge_group_id = grp["merge_group_id"]
            merge = await conn.fetchrow(
                "SELECT id, assigned_to FROM stories "
                "WHERE merge_group_id = $1 AND story_kind = 'merge' LIMIT 1",
                merge_group_id,
            )
            last = await conn.fetchrow(
                "SELECT story_id FROM git_node_stories WHERE git_node_id = $1 "
                "ORDER BY seq DESC LIMIT 1",
                node_id,
            )
            assignee = (merge["assigned_to"] if merge else "") or ""
            depends = [last["story_id"]] if last else []
            thread_id = "th" + sid

            await conn.execute(
                # execution_mode='operator' is REQUIRED: a diff/fix story must provision
                # an OPERATOR workspace (single-branch clone of the engine-cut branch +
                # git creds) so the assistant can pull trunk / commit / push. The column
                # defaults to 'ia_dispatch' (no git workspace), which would make the story
                # undispatchable as branch work AND invisible to the listener's
                # operator-only poll backstop.
                "INSERT INTO stories (id, goal_id, title, description, assigned_to, "
                "  state, depends_on, thread_id, created_by, story_kind, merge_group_id, "
                "  execution_mode, git_node_id) "
                "VALUES ($1, $2, $3, $4, $5, 'proposed', $6, $7, 'engine', 'code', $8, "
                "  'operator', $9) "
                "ON CONFLICT (id) DO NOTHING",
                sid, goal_id, title, brief, assignee, depends, thread_id, merge_group_id,
                # git_node_id (R3): the group is no longer a proxy for the node once a
                # group can hold one node per repo — record the node itself.
                node_id,
            )
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, title) "
                "VALUES ($1, $2, 'engine', $3) ON CONFLICT (id) DO NOTHING",
                thread_id, goal_id, thread_label,
            )
            # One engine story in flight per NODE: supersede prior non-terminal engine
            # code stories for THIS node. Runs AFTER the INSERT so `id <> $2` excludes
            # the story we just created.
            #
            # Scoped by node, not by group (R3/2.4): with a multi-write story the group
            # holds one node per repo, so the old group-wide cancel would kill repo B's
            # in-flight diff story every time repo A spawned one. The merge_group_id
            # clause is retained ONLY for legacy rows written before git_node_id
            # existed — those have a NULL node and would otherwise never be superseded.
            from app.services.story_state_ledger import record_states_where
            _superseded_where = (
                "created_by = 'engine' AND story_kind = 'code' "
                "AND id <> $3 AND state NOT IN "
                "('done', 'reviewed', 'cancelled', 'rejected') "
                "AND (git_node_id = $2 OR (git_node_id IS NULL "
                "AND merge_group_id = $1))")
            # THE IDS, READ BEFORE THE CANCEL, because the predicate stops
            # matching the moment they move to `cancelled` and the run still
            # has to be stopped. `record_states_where` returns a count.
            _superseded = [r["id"] for r in await conn.fetch(
                f"SELECT id FROM stories WHERE {_superseded_where}",
                merge_group_id, node_id, sid)]
            await record_states_where(
                conn, "cancelled",
                where=_superseded_where,
                params=(merge_group_id, node_id, sid),
                action="superseded_by_newer_diff")
            # AND STOP THE RUN, which cancelling the story never did.
            #
            # A superseded story left its execution exactly where it was.
            # Caught on GOAL1/next: run 419 sat `PARKED_COLD` with one
            # unanswered question for nine hours while runs 421 and 424
            # superseded it — the story was cancelled, the run was not, and a
            # parked run holds its question, its workspace and its admission
            # slot until something sweeps it. The open question is the worst
            # part: it is addressed to a person, about work that five commits
            # have since made irrelevant.
            #
            # `stop_the_run` goes through the mailbox on its own kernel
            # connection and never raises, so a run that cannot be reached
            # does not undo the supersede.
            from app.services.story_cancellation import stop_the_run
            for _sid in _superseded:
                await stop_the_run(conn, _sid, actor_uid="system")
            # The merge story waits on this story (edge SEQUENCES dispatch; the live
            # behind_by==0 + checks-green gate is the real merge authorization — §0).
            if merge:
                await conn.execute(
                    "UPDATE stories SET depends_on = array_append(depends_on, $2) "
                    "WHERE id = $1 AND NOT ($2 = ANY(depends_on))",
                    merge["id"], sid,
                )
            # Link to the engine-cut branch resource so dispatch provisions ONTO the branch.
            br = await conn.fetchrow(
                "SELECT id FROM goal_resources WHERE goal_id = $1 AND kind = 'branch' "
                "  AND repo_name = $2 AND branch_name = $3 AND status = 'active' LIMIT 1",
                goal_id, h["name"], h["branch_name"],
            )
            if br:
                await conn.execute(
                    "INSERT INTO story_resources (story_id, resource_id, relation) "
                    "VALUES ($1, $2, 'produces') ON CONFLICT DO NOTHING",
                    sid, br["id"],
                )

            # Ripple-check (research/MULTI_REPO_STORIES_PLAN.md §15.3): grant this SAME
            # story write access to every SIBLING repo's branch in the multi-write story's
            # merge group too, not just its own. This reuses provision_story_workspace's
            # EXISTING multi-repo clone path (resolve_repos_for_story -> _clone_descriptors)
            # with zero new provisioning code — it already clones every write repo a story
            # is granted, multi-write or not. Safe from a two-writer race because every
            # story in this merge group shares the SAME assignee (below), so the
            # assistant-busy/single-active-thread rule already guarantees only one of
            # them is ever actively dispatched at a time — nothing else can be
            # concurrently touching a sibling's branch while this session is.
            # 2026-08-20: exclude a sibling whose OWN node already reached a terminal
            # state (MERGED/ABANDONED) — confirmed live (goal VKSDN, story b20bb3f9):
            # a sync story granted write access to a sibling that had ALREADY merged
            # by the time the grant was read. The engine's own detection (detect.py's
            # _POLL_SKIP_STATES) stops watching a MERGED/ABANDONED node entirely, so a
            # new commit pushed to that branch is invisible to it — no PR-open
            # detection, no finalize, and the branch itself may already be deleted
            # (many repos auto-delete on merge). Granting write access to a dead
            # workstream just hands the assistant a blind spot; there is nothing left
            # there for it to safely touch.
            #
            # ESCALATED excluded too: it means a human closed the sibling's PR
            # unmerged (detect.py's _cancel_node), a persistent GitHub 401/403/404
            # (node_errors.py — revoked App permission, deleted ref/PR), or another
            # grind-loop condition that can't self-heal (grind._escalate_node). Its
            # queue slot is already released and — if it was the group's last live
            # node — the merge story itself is rejected. Unlike MERGED/ABANDONED an
            # ESCALATED node COULD still recover (the poller keeps checking it), but
            # right now it needs a human "Retry", not a new commit from this session.
            siblings = await conn.fetch(
                "SELECT r.owner, r.name, n.branch_name FROM git_merge_group_nodes gmn "
                "JOIN git_nodes n ON n.id = gmn.git_node_id "
                "JOIN git_repos r ON r.id = n.git_repo_id "
                "WHERE gmn.merge_group_id = $1 AND gmn.git_node_id <> $2 "
                "  AND n.state NOT IN ('MERGED', 'ABANDONED', 'ESCALATED')",
                merge_group_id, node_id,
            )
            for sib in siblings:
                sib_br = await conn.fetchrow(
                    "SELECT id FROM goal_resources WHERE goal_id = $1 AND kind = 'branch' "
                    "  AND repo_name = $2 AND branch_name = $3 AND status = 'active' LIMIT 1",
                    goal_id, sib["name"], sib["branch_name"],
                )
                if sib_br:
                    # relation='modifies', NOT 'produces': this grant and the own-repo
                    # grant above are inserted in the SAME transaction, so they tie
                    # exactly on created_at (Postgres now() resolves to transaction
                    # start for every statement in one transaction — the same class of
                    # bug fixed for story_workspace_nodes.is_primary, see
                    # story_workspace_layout.py). 'modifies' is still a full write
                    # relation (repo_identity.WRITE_RELATIONS) so this repo is still
                    # cloned writable — it just no longer ties with the story's own
                    # repo for "which one is primary" in _resolve_repo_for_story /
                    # resolve_repos_for_story, which now rank 'produces' strictly above
                    # 'modifies'.
                    await conn.execute(
                        "INSERT INTO story_resources (story_id, resource_id, relation) "
                        "VALUES ($1, $2, 'modifies') ON CONFLICT DO NOTHING",
                        sid, sib_br["id"],
                    )
            await append_event(
                conn,
                actor_id=_SYSTEM_ACTOR_ID, actor_type="system",
                event_type="story.created",
                payload={"story_id": sid, "title": title, "story_kind": "code",
                         "engine_authored": kind_tag, "merge_group_id": merge_group_id,
                         "assigned_user_id": assignee or ""},
                context_goal_id=goal_id,
            )
    # Wake the web-tier branch-ready listener to bind this story to its assignee's SHCA
    # + dispatch it (the engine worker can't provision/dispatch). Best-effort + the
    # listener's poll backstop covers a missed NOTIFY.
    try:
        await notify_story_branch_ready(goal_id, sid)
    except Exception:
        _log.warning("git-engine: notify for engine %s story %s failed", kind_tag, sid)
    _log.info("git-engine: spawned %s story %s for node %s", kind_tag, sid, node_id)
    return True


async def _ensure_diff_story(h: dict, trunk_sha: str) -> bool:
    """Spawn (idempotently) a diff story for an active holder whose branch is BEHIND
    trunk. Spawned for ANY behind state (not only conflicts): the assistant must bring
    trunk in AND reconcile + test, because a clean text merge can still be semantically
    broken. The brief IS the dispatch message the SHCA receives."""
    branch, trunk = h["branch_name"], h["trunk_branch"]
    sid = _diff_story_id(h["node_id"], trunk_sha)
    # Name the workstream's OWN stories (the work we're trying to land) so the assistant
    # knows what its branch contains and what it must keep working after integrating.
    pool = get_pool()
    members = await pool.fetch(
        "SELECT s.title FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
        "WHERE gns.git_node_id = $1 AND s.created_by <> 'engine' AND s.story_kind = 'code' "
        "ORDER BY gns.seq",
        h["node_id"],
    )
    work = "; ".join(m["title"] for m in members if m["title"]) or "this branch's work"

    # Ripple-check (research/MULTI_REPO_STORIES_PLAN.md §15.3): when this branch is
    # part of a multi-write story, this same clone ALSO carries a write-enabled copy
    # of every sibling repo in the change (granted alongside this story's own branch —
    # see _spawn_engine_code_story). Tell the assistant so it actually looks, rather
    # than assuming its own repo is the whole change.
    # Same terminal-sibling exclusion as _spawn_engine_code_story's grant query
    # (2026-08-20) — telling the assistant to go check a sibling that already
    # merged would send it looking at a workstream the engine no longer watches.
    siblings = await pool.fetch(
        "SELECT r.owner, r.name FROM git_merge_group_nodes gmn "
        "JOIN git_nodes n ON n.id = gmn.git_node_id "
        "JOIN git_repos r ON r.id = n.git_repo_id "
        "WHERE gmn.merge_group_id = ( "
        "  SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1 LIMIT 1) "
        "  AND gmn.git_node_id <> $1 "
        "  AND n.state NOT IN ('MERGED', 'ABANDONED', 'ESCALATED')",
        h["node_id"],
    )
    ripple_note = ""
    if siblings:
        sib_dirs = ", ".join(f"`{repo_clone_subdir(s['owner'], s['name'])}`" for s in siblings)
        ripple_note = (
            f"\nThis is part of a MULTI-REPO change: {sib_dirs} are the sibling repo(s) in "
            f"the same change, cloned writable alongside yours in this same workspace. "
            f"Before you finish: check whether integrating `{trunk}` here requires a "
            f"matching change in any of them (a renamed function, a changed contract, a "
            f"shared type) — if so, make that change directly in that sibling's clone and "
            f"commit + push it too, in this SAME session. Do not open a separate story or "
            f"assume someone else will notice.\n"
        )
    title = f"Integrate {trunk} into {branch}"
    brief = (
        f"Your branch `{branch}` holds the work for: {work}. That work is finished and "
        f"ready to merge into trunk `{trunk}` — but trunk has ADVANCED since your branch "
        f"was cut: OTHER stories and/or humans have landed their own changes on `{trunk}` "
        f"in the meantime. So `{branch}` is now BEHIND `{trunk}` and must be brought up to "
        f"date before it can merge cleanly.\n"
        f"Your job: bring those newer trunk changes into your branch and make sure your "
        f"work still integrates with them. Stay ON `{branch}` — merging trunk INTO your "
        f"branch does NOT count as switching branches and is exactly what you should do "
        f"here (do not create or check out a different branch).\n"
        f"1. Bring trunk in with the **`sync_trunk` tool**. It takes no arguments: "
        f"it resolves the lane from the record, fetches `{trunk}` by name and "
        f"merges it into `{branch}`, worker-side. Call it once and read what it "
        f"reports.\n"
        f"   Do NOT use `git pull` / `git fetch` / `git merge` by hand. This brief "
        f"used to spell out `git pull origin {trunk}`, and the result was that every "
        f"diff story did the merge with raw git while `sync_trunk` went uncalled -- "
        f"the outcome looked right and nothing recorded that the platform own tool "
        f"had not done it.\n"
        f"   A CONFLICT from `sync_trunk` is not a failure: the merge ran and found "
        f"two real answers, the markers are in your files, and resolving them is "
        f"step 2. An ERROR is a platform fault -- say so and stop rather than "
        f"working around it with git.\n"
        f"2. Resolve any merge conflicts.\n"
        f"3. RECONCILE your work with what others changed on `{trunk}` — EVEN IF the merge "
        f"was clean. Their changes may have moved/renamed files, changed an API/signature/"
        f"contract your code calls, added a required step, or updated shared types. Read "
        f"the incoming `{trunk}` changes and adapt your work so it still builds and behaves "
        f"correctly against them.\n"
        f"4. Run the build/tests and repair anything the integration broke.\n"
        f"5. Commit with **`story_commit`**, then **`propose_completion`**. Do NOT "
        f"run `git push`: publishing is the platform, exactly as for an ordinary "
        f"story, and it happens once your claim is settled. A diff story finishes "
        f"the same way every other story does.\n"
        f"Do NOT open a pull request — the engine opens/updates the merge PR once your "
        f"branch is current with trunk and green."
        f"{ripple_note}"
    )
    return await _spawn_engine_code_story(
        h, sid, title, brief, f"Sync: {branch[:60]}", "diff")


async def _ensure_fix_story(h: dict, head_sha: str, failed: list) -> bool:
    """Spawn (idempotently) a fix story for an active holder whose OPEN PR has failing
    checks. Idempotent by head_sha so a flaky check on the same head doesn't churn."""
    sid = _fix_story_id(h["node_id"], head_sha)
    names = ", ".join(str(f) for f in failed[:6]) or "required checks"
    title = f"Fix failing checks on {h['branch_name']}"
    brief = (
        f"The open PR for `{h['branch_name']}` has FAILING checks: {names}. Reproduce and "
        f"fix the failures so the checks pass, then commit + push. Do NOT open a pull "
        f"request — it is already open; pushing updates it."
    )
    return await _spawn_engine_code_story(
        h, sid, title, brief, f"Fix: {h['branch_name'][:60]}", "fix")


async def _ensure_pr_open(h: dict, trunk_branch: str) -> bool:
    """Open the merge PR for a clean (up-to-date, ahead) active holder and pin its
    pr_number on the node. The engine opens it via `open_or_get_pr` (idempotent —
    422 → finds the existing open PR); the HUMAN reviews + merges on GitHub, and
    detection finalizes. Returns True iff a PR is now pinned.

    PR ATTRIBUTION (spec §0): the title/body name the workstream owner + goal + story.
    (The commit AUTHOR = the assistant identity is set gateway-side when the assistant
    commits — a deferred §7b gateway concern.)"""
    pool = get_pool()
    owner, name = h["owner"], h["name"]
    branch, node_id, goal_id = h["branch_name"], h["node_id"], h["goal_id"]

    merge = await pool.fetchrow(
        "SELECT s.id, s.assigned_to FROM git_merge_group_nodes gmn "
        "JOIN stories s ON s.merge_group_id = gmn.merge_group_id AND s.story_kind = 'merge' "
        "WHERE gmn.git_node_id = $1 LIMIT 1",
        node_id,
    )
    assignee = (merge["assigned_to"] if merge else "") or ""
    who = assignee or "the workstream owner"
    if assignee:
        # Best-effort display name (users PK is `uid`; columns display_name/email).
        try:
            disp = await pool.fetchval(
                "SELECT COALESCE(NULLIF(display_name, ''), NULLIF(email, '')) "
                "FROM users WHERE uid = $1",
                assignee,
            )
            if disp:
                who = disp
        except Exception:
            pass

    token = await gh.mint_repo_token(name, _PR_OPEN_PERMS)

    # Merge-queue split (§8): on a trunk that REQUIRES a merge queue, the human
    # ENQUEUES ("Merge when ready") and GitHub's queue lands it; the engine never
    # enqueues/merges, only detects (merged / dequeued). Read it fresh from the trunk's
    # effective protection (the `merge_queue` ruleset rule, readable with metadata) to
    # word the PR body. NOT cached on git_repos — that flag is repo-scoped while the
    # queue requirement is per-(repo,trunk); §10 can persist it per-trunk if needed.
    queue_required = False
    try:
        prot = await gh.get_effective_protection(token, owner, name, trunk_branch)
        queue_required = bool(prot.get("merge_queue_required"))
    except Exception:
        pass
    merge_instr = (
        "add this PR to the **merge queue** (\"Merge when ready\")" if queue_required
        else "review and **merge this PR**"
    )

    title = f"Merge {branch} → {trunk_branch}"
    body = (
        f"Automated merge PR opened by the **Git Engine** for goal `{goal_id}`, "
        f"workstream `{branch}`.\n\n"
        f"Worked by **{who}**. To land the workstream, {merge_instr} — the engine "
        f"detects the merge and unblocks dependents. Do not merge from the engine; "
        f"the merge is yours."
    )
    pr = await gh.open_or_get_pr(token, owner, name, branch, trunk_branch, title, body)
    status = await pool.execute(
        "UPDATE git_nodes SET pr_number = $2, updated_at = now() "
        "WHERE id = $1 AND pr_number IS NULL",
        node_id, pr["number"],
    )
    _log.info("git-engine: opened merge PR #%s for node %s (%s → %s)",
              pr["number"], node_id, branch, trunk_branch)
    # Fire the one-time "PR opened" notice ONLY when THIS call flipped pr_number
    # NULL->set ("UPDATE 1"); a re-grind or concurrent pass that finds it already
    # pinned ("UPDATE 0") doesn't re-notify. The web-tier listener is also flag-gated
    # (pr_opened_notified_at), so this is belt-and-suspenders. DB-only here — the
    # engine worker has no session access; the web tier posts the chat card.
    if isinstance(status, str) and status.strip().endswith(" 1"):
        try:
            from .dispatch import notify_git_pr_opened
            await notify_git_pr_opened(goal_id, node_id)
        except Exception:
            _log.warning("git-engine: pr-opened notify failed for node %s", node_id)
    return True
