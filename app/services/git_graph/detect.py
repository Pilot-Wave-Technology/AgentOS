"""Detection + finalize (spec §4) — the engine learns of merges/cancels from
GitHub and reconciles. This REPLACES the retired self-merge land (`merge_lane.
run_merge_pass`/`_drain_repo`/`_merge_one`): the engine NEVER merges anymore — a
human merges on the PR, and the engine DETECTS it.

Two sources, webhooks-as-hints + API-as-truth:
  1. undelivered `git_webhook_events` rows (the event-aware intake, §4),
  2. a poll backstop over non-terminal nodes that already have an open PR
     (catches a dropped/duplicate webhook).

For a detected PR close we VERIFY-BY-READ (`get_pr`):
  - `merged == true` → reuse `merge_lane._finalize_merged` (advance the per-repo
    observed trunk_sha, node → MERGED, merge story → done when its whole group
    lands, queue row → 'merged'),
  - `closed && !merged` → human-cancel: node → ESCALATED, queue slot released,
    and crucially the merge story is NOT marked done, so dependents STAY BLOCKED
    (spec §0/§6). The story-level rejection + replan notification + slot promote
    are later steps (§9/§12 + the queue/lease §5).

Any merge to a managed trunk — ours, another goal's, or a human PR — advances the
observed `trunk_sha` (the Case-6 "advance on ANY merge"; branch-match decides
finalize-our-node vs just-advance-trunk).

Leader-only (called from engine `_tick`), short-lived pool calls, idempotent
(`_finalize_merged` guards `WHERE state <> 'MERGED'`; trunk advance is
IS-DISTINCT-FROM; cancel guards out of terminal states).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_pool

from . import github_ops as gh
from .merge_lane import _finalize_merged, cancel_engine_stories_for_group
from .node_errors import clear_node_error, record_node_github_failure
from .repo_identity import set_observed_trunk

_log = logging.getLogger(__name__)

# Read-only perms — detection never writes to GitHub (the human merges).
_DETECT_PERMS = {"contents": "read", "metadata": "read", "pull_requests": "read"}
_EVENT_BATCH = 100
_POLL_LIMIT = 100
# Poll-side cancel backstop: the close→cancel signal is normally the webhook (immediate).
# If that delivery is MISSED, a closed-unmerged PR would strand its node + dependents
# forever, because the poll is otherwise finalize-only (it won't cancel on a transient
# `closed` read that might be a close→reopen mid-window). So the poll cancels ONLY when
# the PR has been closed-unmerged for longer than this debounce — well past any reopen
# race — making cancel detection self-healing without a false-cancel.
_POLL_CANCEL_DEBOUNCE = timedelta(minutes=10)


def _closed_long_enough(closed_at: Optional[str]) -> bool:
    """True if the PR's GitHub `closed_at` is older than the cancel debounce."""
    if not closed_at:
        return False
    try:
        ts = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return (datetime.now(timezone.utc) - ts) >= _POLL_CANCEL_DEBOUNCE
# Nodes in these states are settled / not-yet-open — skip the PR poll.
# NOTE: ESCALATED is deliberately NOT skipped. A node can be escalated for a TRANSIENT
# reason (a 403 burst, a flaky required check) while its PR is still open + mergeable; if
# a human then merges that PR and the webhook is missed, only the poll can finalize it.
# The poll is finalize-ONLY (allow_cancel=False), so re-reading an escalated node's PR is
# safe — it either finalizes a real merge or no-ops. (MERGED/ABANDONED are truly terminal.)
_POLL_SKIP_STATES = (
    "MERGED", "ABANDONED", "PLANNED", "BRANCHING", "BRANCH_CREATED",
)


async def run_detection_pass() -> dict:
    """Consume webhook events + poll open PRs; finalize merges, handle cancels,
    advance trunk. Idempotent; safe to run every tick."""
    out = {"events": 0, "finalized": 0, "cancelled": 0, "trunk_advanced": 0}
    tokens: dict[str, str] = {}   # per-pass repo→token cache (least-privilege read)
    try:
        await _process_events(out, tokens)
    except Exception:
        _log.exception("git-engine: detection event pass failed")
    try:
        await _poll_open_prs(out, tokens)
    except Exception:
        _log.exception("git-engine: detection poll pass failed")
    return out


async def _token(repo_name: str, repo_id: str, tokens: dict) -> str:
    tok = tokens.get(repo_id)
    if tok is None:
        tok = await gh.mint_repo_token(repo_name, _DETECT_PERMS)
        tokens[repo_id] = tok
    return tok


async def _resolve_repo(ev) -> Optional[dict]:
    """Map a webhook delivery to a managed repo: prefer the path-param git_repo_id,
    fall back to repository.full_name (owner/name). None if we don't manage it."""
    pool = get_pool()
    if ev["git_repo_id"]:
        r = await pool.fetchrow(
            "SELECT id, owner, name, trunk_branch FROM git_repos WHERE id = $1",
            ev["git_repo_id"],
        )
        if r:
            return dict(r)
    full = ev["repo_full_name"]
    if full and "/" in full:
        owner, name = full.split("/", 1)
        r = await pool.fetchrow(
            "SELECT id, owner, name, trunk_branch FROM git_repos WHERE owner = $1 AND name = $2",
            owner, name,
        )
        if r:
            return dict(r)
    return None


async def _node_for_pr(repo_id: str, pr_number: int) -> Optional[dict]:
    """Find OUR node for a PR by the pr_number the merge story pinned on it
    (build §7). No match → an external/human PR → caller just advances trunk."""
    pool = get_pool()
    r = await pool.fetchrow(
        "SELECT id, state, trunk_branch FROM git_nodes WHERE git_repo_id = $1 AND pr_number = $2",
        repo_id, pr_number,
    )
    return dict(r) if r else None


async def _advance_trunk(repo: dict, trunk_branch: str, new_trunk: str) -> bool:
    """Advance the observed trunk_sha for (repo, trunk_branch) — detection cache only,
    never a mutation base. Writes the per-(repo,trunk) git_repo_trunks cache (§0) and
    the legacy git_repos.trunk_sha. Idempotent — no-op when unchanged. (Only ever
    called with the repo's default trunk — the external/human-PR + push paths — so
    the legacy git_repos.trunk_sha is not clobbered by a non-default trunk.)"""
    if not new_trunk:
        return False
    pool = get_pool()
    await set_observed_trunk(pool, repo["id"], trunk_branch, new_trunk)
    res = await pool.execute(
        "UPDATE git_repos SET trunk_sha = $2, trunk_sha_seen_at = now() "
        "WHERE id = $1 AND trunk_sha IS DISTINCT FROM $2",
        repo["id"], new_trunk,
    )
    return res.endswith(" 1")


async def _cancel_node(node_id: str, reason: str) -> None:
    """Human-cancel (closed&!merged): node → ESCALATED, release the queue slot, and
    mark the merge story 'rejected' (spec §12) — an explicit human merge-cancel.
    'rejected' keeps dependents BLOCKED (the merge-dep rule treats 'rejected' as
    unmet — story_transitions._DEP_SATISFIED_SQL / branches._is_cut_ready), so the
    consumer chain stays blocked for re-plan (§0 exit-2)."""
    pool = get_pool()
    # One transaction so a crash can't leave a torn ESCALATED/parked/rejected state
    # (mirrors grind._escalate_node).
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE git_nodes SET state = 'ESCALATED', last_error = $2, updated_at = now() "
            "WHERE id = $1 AND state NOT IN ('MERGED', 'ABANDONED')",
            node_id, reason[:500],
        )
        await conn.execute(
            "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1", node_id
        )
        # Mark the node's merge story rejected (dependents stay blocked via the merge-dep rule).
        await conn.execute(
            "UPDATE stories SET state = 'rejected', updated_at = now() "
            "WHERE story_kind = 'merge' AND state NOT IN ('done', 'rejected', 'cancelled') "
            "  AND merge_group_id IN (SELECT merge_group_id FROM git_merge_group_nodes "
            "                         WHERE git_node_id = $1)",
            node_id,
        )
        # Cascade-cancel the group's engine diff/fix stories — the branch is being torn
        # down, so its integrate/fix assistant stories must not keep running (orphans).
        for g in await conn.fetch(
            "SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1", node_id
        ):
            await cancel_engine_stories_for_group(conn, g["merge_group_id"])


# Write-to-PRs perms so reaping can CLOSE a dead goal's open PR.
_REAP_PERMS = {"contents": "read", "metadata": "read", "pull_requests": "write"}


async def reap_deleted_goal_nodes() -> int:
    """Terminalize git nodes whose goal is no longer live (deleted/cancelled).

    Goal deletion is a SOFT delete (goals.status='deleted') that does NOT touch
    git_nodes / merge groups / leases / the queue — and every engine pass selects
    nodes by their OWN state with no goal-status filter. So a dead goal's in-flight
    nodes keep getting cut, advanced, enqueued, ground, and (on a human PR merge)
    finalized to trunk — an outward git action on a goal that no longer exists.

    Run FIRST in the tick (zombie-sweep philosophy): close any open PR (best-effort,
    so a human can't later merge a dead goal's PR and have detection finalize it —
    _finalize_merged also refuses to revive an ABANDONED node), then node → ABANDONED,
    park the lease slot, cancel the merge story, and cascade-cancel the group's engine
    diff/fix stories. Once ABANDONED every downstream pass already skips it (WHERE
    state NOT IN (...,'ABANDONED')) and the cross-goal UI drops it — so this is the
    single defense + cleanup. The cut ref is left on GitHub (deletion is destructive +
    out-of-band; the ABANDONED node is inert). Idempotent: terminal nodes are excluded,
    so a reaped node is never revisited.

    "Dead" = the soft-delete `goals.status='deleted'` (the live delete_goal path; the
    status CHECK only allows active/deleted) OR the lifecycle terminal `goals.state=
    'cancelled'` (separate column, migration 014) — so a future lifecycle-cancel is
    reaped too, not just hard deletes."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT n.id AS node_id, n.pr_number, r.owner, r.name "
        "FROM git_nodes n "
        "JOIN goals g ON g.id = n.goal_id "
        "JOIN git_repos r ON r.id = n.git_repo_id "
        "WHERE (g.status = 'deleted' OR g.state = 'cancelled') "
        "  AND n.state NOT IN ('MERGED', 'ABANDONED')"
    )
    reaped = 0
    for row in rows:
        if row["pr_number"]:
            try:
                token = await gh.mint_repo_token(row["name"], _REAP_PERMS)
                await gh.close_pr(token, row["owner"], row["name"], row["pr_number"])
            except Exception as exc:
                # Non-fatal: the DB terminalization below is what makes the node inert.
                _log.warning("reap: close_pr failed for node %s (goal dead): %s",
                             row["node_id"], exc)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE git_nodes SET state = 'ABANDONED', "
                "  last_error = 'goal deleted/cancelled — node reaped by engine', "
                "  updated_at = now() "
                "WHERE id = $1 AND state NOT IN ('MERGED', 'ABANDONED')",
                row["node_id"],
            )
            await conn.execute(
                "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1",
                row["node_id"],
            )
            await conn.execute(
                "UPDATE stories SET state = 'cancelled', updated_at = now() "
                "WHERE story_kind = 'merge' "
                "  AND state NOT IN ('done', 'cancelled', 'rejected') "
                "  AND merge_group_id IN (SELECT merge_group_id FROM git_merge_group_nodes "
                "                         WHERE git_node_id = $1)",
                row["node_id"],
            )
            for g in await conn.fetch(
                "SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1",
                row["node_id"],
            ):
                await cancel_engine_stories_for_group(conn, g["merge_group_id"])
        reaped += 1
    if reaped:
        _log.info("git-engine: reaped %d node(s) of deleted/cancelled goal(s)", reaped)
    return reaped


async def _finalize_or_cancel(repo: dict, node: Optional[dict], pr: dict,
                              token: str, out: dict, *, allow_cancel: bool = True) -> None:
    """Given verify-by-read PR state, finalize a merge / advance trunk / cancel.

    `allow_cancel` gates the closed&!merged → cancel path. The discrete `closed`
    webhook EVENT sets it True (a real close action); the POLL backstop sets it
    False — a poll read of `closed` could be a close-then-REOPEN observed mid-window
    (§6 P1), and a false-cancel (ESCALATE) is worse than a dropped close (which just
    leaves the PR awaiting; the §12 cancel-authority action is the explicit path).
    The poll therefore only FINALIZES merges, never cancels."""
    owner, name = repo["owner"], repo["name"]
    # Per-goal trunk (§0): finalize against the NODE's trunk_branch; for an external/
    # human PR (no node) fall back to the repo default.
    node_trunk = node.get("trunk_branch") if node else None
    trunk_branch = node_trunk or repo["trunk_branch"] or "main"
    if pr["merged"]:
        new_trunk = pr.get("merge_commit_sha") or ""
        if not new_trunk:
            try:
                new_trunk = await gh.get_trunk_sha(token, owner, name, trunk_branch)
            except Exception:
                new_trunk = ""
        if node:
            await _finalize_merged(repo["id"], node["id"], new_trunk,
                                   token, owner, name, trunk_branch)
            out["finalized"] += 1
        elif await _advance_trunk(repo, trunk_branch, new_trunk):   # external / human PR
            out["trunk_advanced"] += 1
    elif allow_cancel and pr.get("state") == "closed":   # closed && !merged → human-cancel
        if node:
            await _cancel_node(node["id"], "PR closed unmerged (human-cancel) — replan")
            out["cancelled"] += 1


async def _handle_event(ev, out: dict, tokens: dict) -> None:
    repo = await _resolve_repo(ev)
    if repo is None:
        return  # not a repo we manage — the audit row remains, nothing to do
    event, action = ev["event"], ev["action"]
    owner, name = repo["owner"], repo["name"]
    trunk_branch = repo["trunk_branch"] or "main"

    if event == "pull_request" and action in ("closed", "dequeued") and ev["pr_number"]:
        token = await _token(name, repo["id"], tokens)
        node = await _node_for_pr(repo["id"], ev["pr_number"])
        try:
            pr = await gh.get_pr(token, owner, name, ev["pr_number"])   # verify-by-read
        except Exception as exc:
            # Surface on OUR node (if any) so a detection auth failure isn't invisible —
            # a node whose merged PR can never be confirmed would otherwise sit forever
            # in its pre-merge state with no UI signal. Re-raise so the event stays
            # undelivered and retries (5xx) or stays visibly ESCALATED (401/403/404).
            if node:
                await record_node_github_failure(node["id"], exc, context="detect")
            raise
        # `dequeued` (merge-queue split, §0.1) fires on EVERY queue removal INCLUDING a
        # successful merge — so we read get_pr().merged either way. A dequeue is NOT a
        # human-cancel: a CI/conflict eject leaves the PR open and the grind loop
        # re-drives it (sync / fix), so only a human `closed` may cancel.
        await _finalize_or_cancel(repo, node, pr, token, out,
                                  allow_cancel=(action == "closed"))
    elif event == "push":
        # Any push to trunk advances the observed sha. A live read is idempotent
        # (a push to a non-trunk branch leaves trunk HEAD unchanged → no-op update).
        token = await _token(name, repo["id"], tokens)
        try:
            new_trunk = await gh.get_trunk_sha(token, owner, name, trunk_branch)
        except Exception:
            return
        if await _advance_trunk(repo, trunk_branch, new_trunk):
            out["trunk_advanced"] += 1
    # check_run/check_suite/status/pull_request_review wakes are consumed by the
    # grind loop (build §6), not here; they just need to be marked delivered.


async def _process_events(out: dict, tokens: dict) -> None:
    pool = get_pool()
    events = await pool.fetch(
        "SELECT delivery_id, event, action, git_repo_id, repo_full_name, pr_number, merged "
        "FROM git_webhook_events WHERE delivered = FALSE "
        "ORDER BY received_at ASC LIMIT $1",
        _EVENT_BATCH,
    )
    for ev in events:
        try:
            await _handle_event(ev, out, tokens)
        except Exception:
            # Leave delivered=FALSE → retried next pass. A persistent poison-pill at
            # the head is re-fetched each pass but does NOT block newer events in the
            # same batch (we continue the loop). A retry-cap/dead-letter is deferred.
            _log.exception("git-engine: detection failed for delivery %s", ev["delivery_id"])
            continue
        await pool.execute(
            "UPDATE git_webhook_events SET delivered = TRUE WHERE delivery_id = $1",
            ev["delivery_id"],
        )
        out["events"] += 1


async def _poll_open_prs(out: dict, tokens: dict) -> None:
    """Backstop: re-read non-terminal nodes that have an open PR (catches a
    dropped/missed webhook). No-op until a merge story has pinned a pr_number
    (build §7)."""
    pool = get_pool()
    placeholders = ", ".join(f"${i + 2}" for i in range(len(_POLL_SKIP_STATES)))
    nodes = await pool.fetch(
        "SELECT n.id, n.state, n.pr_number, n.trunk_branch, n.git_repo_id, "
        "       r.owner, r.name, r.trunk_branch AS repo_trunk "
        "FROM git_nodes n JOIN git_repos r ON r.id = n.git_repo_id "
        f"WHERE n.pr_number IS NOT NULL AND n.state NOT IN ({placeholders}) "
        "LIMIT $1",
        _POLL_LIMIT, *_POLL_SKIP_STATES,
    )
    for n in nodes:
        # repo carries the DEFAULT trunk; the node carries its per-goal trunk.
        repo = {"id": n["git_repo_id"], "owner": n["owner"], "name": n["name"],
                "trunk_branch": n["repo_trunk"]}
        node = {"id": n["id"], "state": n["state"], "trunk_branch": n["trunk_branch"]}
        try:
            token = await _token(n["name"], n["git_repo_id"], tokens)
            pr = await gh.get_pr(token, n["owner"], n["name"], n["pr_number"])
        except Exception as exc:
            # Surface on the node (UI error column) instead of log-only — a node whose
            # PR poll keeps 401/403'ing would never finalize, invisibly.
            await record_node_github_failure(n["id"], exc, context="detect-poll")
            _log.exception("git-engine: PR poll failed for node %s", n["id"])
            continue
        # Poll is FINALIZE-ONLY (allow_cancel=False): a transient `closed` read could
        # be a reopen-in-progress; immediate cancels come only from the discrete `closed`
        # event. But a MISSED close webhook would strand a human-cancelled PR forever, so
        # the poll DOES cancel a PR that has stayed closed-unmerged past the debounce
        # (well beyond any reopen race) — the cancel backstop.
        if (not pr["merged"] and pr.get("state") == "closed"
                and _closed_long_enough(pr.get("closed_at"))):
            await _cancel_node(
                node["id"], "PR closed unmerged (poll backstop, webhook missed) — replan")
            out["cancelled"] += 1
            continue
        await _finalize_or_cancel(repo, node, pr, token, out, allow_cancel=False)
        # A clean poll (finalize-only → never escalates) means the node is healthy
        # (merged, or still open & waiting) → clear any stale error.
        await clear_node_error(get_pool(), n["id"])
