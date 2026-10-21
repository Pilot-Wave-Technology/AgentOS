"""Local Execution Mode — read-only hydration bundle + clone token (M2).

Assembles what the extension needs to open a story locally: story metadata, the
SHCA persona (template files + applicable skills), the current plan, and the
feature branch to clone — plus a repo-scoped, read-only GitHub clone token. All
read-only; no execution, no gateway workspace. See LOCAL_EXECUTION_MODE_PLAN.md
§7 W1, §8.2/§8.3, §14.3.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.database import get_pool
from app.github_tokens import generate_installation_token
from kernel.go.drivable import autonomous_errand_predicate
from app.services.story_workspace_layout import (
    resolve_workspace_clones,
    to_bundle_repos,
)

_log = logging.getLogger(__name__)

_SHCA_TEMPLATE_ID = "kernel-assistant"   # template_files key (per-template)
_SHCA_TYPE = "SHCA"                   # skills.applicable_to holds agent TYPES, not ids
_TERMINAL = ("done", "reviewed", "cancelled", "rejected")
# git_node states where the feature branch physically exists on the remote (so it's
# cloneable). Excludes not-yet-cut (PLANNED/BRANCHING) + terminal (MERGED/ABANDONED).
_BRANCH_LIVE_STATES = ("BRANCH_CREATED", "IN_PROGRESS", "READY", "ESCALATED")

# AND THE STATES A LANE PASSES THROUGH WITH ITS PULL REQUEST OPEN, which
# a LOCAL story must still be served in.
#
# The branch physically exists in every one of these -- that is what the
# list above is for -- and the only reason they were excluded is that in
# the pod lane nobody clones a branch that is on its way to trunk. A
# local story is different in the one way that matters: it integrates
# ITSELF. Trunk moves, the person approves a catch-up, and the catch-up
# has to hydrate the clone and push the result -- to the SAME branch,
# updating the SAME pull request.
#
# Excluded, the catch-up could not run at all: hydrate answers `no_repo`
# and the push token is refused, on a story whose whole remaining job is
# to push. `QUEUED` is reachable in the ordinary way (`enqueue_ready_
# merges` moves a READY node there), so this is not a corner case.
#
# `MERGED` and `ABANDONED` stay out. The branch is finished with, and
# serving a clone of it would invite work nobody can land.
_PR_OPEN_STATES = ("MERGING", "QUEUED", "CONFLICTED")


# AND A MERGED LANE, WHILE THE STORY IS STILL BEING CLOSED OUT.
#
# "The branch is finished with, so serving a clone of it would invite
# work nobody can land" -- which I wrote, and which is wrong about the
# window between the last merge and `done`. In that window the story
# still owes three things that all need the tree: the in-session review
# (`owes_review` has the session READ WHAT IT WROTE), the memory
# consolidation, and the person's own look at what landed.
#
# On 66317ffc the lane merged, the story sat at `awaiting_closeout`, and
# the import failed with "the engine hasn't cut this story's feature
# branch yet" -- about a branch that had been cut, worked, pushed and
# merged. There was no way back in to finish the story.
#
# THE "INVITE WORK" WORRY IS ANSWERED AT THE RIGHT LAYER NOW. The
# boundary hook refuses source edits in `awaiting_merge` and
# `awaiting_closeout`, so a clone served here cannot be written to. A
# guard enforced by withholding the tree is a guard that also withholds
# everything else the tree is for.
_MERGED_STATE = ("MERGED",)

# Once the story is finished there is genuinely nothing left to do with
# it, and ABANDONED means the lane was given up on in any state.
_CLOSING_STATES = ("awaiting_merge",)


async def _served_states(pool, story_id: str) -> list[str]:
    """Which node states this story's clone may be served in.

    LOCAL-ONLY WIDENING. The pod lane's list is unchanged byte for byte:
    its branches are integrated by somebody else, so a lane with a PR
    open genuinely has nothing left for its own story to do, and its
    workspace is collected the moment the run ends.
    """
    row = await pool.fetchrow(
        "SELECT COALESCE(execution_mode, '') = 'local' AS local, state "
        "  FROM stories WHERE id = $1", story_id)
    if not row or not row["local"]:
        return list(_BRANCH_LIVE_STATES)
    states = list(_BRANCH_LIVE_STATES) + list(_PR_OPEN_STATES)
    if (row["state"] or "") in _CLOSING_STATES:
        states += list(_MERGED_STATE)
    return states


async def _persona(pool) -> dict:
    """SHCA persona files + applicable skills, straight from the DB templates."""
    files = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id = $1",
        _SHCA_TEMPLATE_ID,
    )
    # applicable_to is keyed by agent TYPE ('SHCA'), not template id — matches how
    # openclaw.py / agent_registry.py resolve skills. Using the id yields zero rows.
    skills = await pool.fetch(
        "SELECT name, content FROM skills "
        "WHERE $1 = ANY(applicable_to) AND status = 'active' ORDER BY name",
        _SHCA_TYPE,
    )
    return {
        "files": {r["filename"]: r["content"] for r in files},
        "skills": {r["name"]: r["content"] for r in skills},
    }


async def _resolve_live_repos(pool, story_id: str) -> list[dict]:
    """Every CLONE this story needs right now — one entry per live, engine-cut
    (repo, trunk lane), most-recently-updated node per lane winning.

    KEYED BY LANE, NOT BY REPO. This used to key on (owner, name) alone,
    reasoning that "several live nodes in the SAME repo are the normal
    re-cut/reconcile case, not a second repo". True of a re-cut, and false of
    the case the lane model exists for: a story writing a backend lane and a
    frontend lane of one repository has two live nodes of that repo and needs
    BOTH clones. Story 7f995412 on dev had exactly that, and the frontend lane
    was dropped silently -- the session was handed one clone and reported the
    two branches "have never been combined", which was true and not its fault.
    A re-cut is still collapsed, because a re-cut produces two nodes on the
    SAME trunk; two lanes differ by trunk, which is what the key now says.

    The plural sibling of `_repo_ref`, and the one multi-repo-aware callers
    (`mint_repo_clone_token`, `local_diff.mint_push_token`, `build_hydration_bundle`,
    `local_workspace_nodes.record_local_workspace_node`) use instead of it once the
    calling client has declared the `multi-repo` capability. Read-only; does not by
    itself decide whether the set is COMPLETE — see `_multi_repo_incomplete` for that.

    Two separate orderings are at work here, deliberately: WHICH node represents a
    repo (when a re-cut leaves more than one live node for it) is decided by
    `updated_at DESC` — the most recently touched node is the current one. WHAT ORDER
    the repos themselves come back in is decided by that chosen node's `created_at`
    ASCENDING — the first-cut repo is entry 0, consistently, for the life of the
    checkout. Every consumer of this list (`materializeAll`'s clone loop,
    `build_hydration_bundle`'s scalar `repo`, `pushAll`'s primary/extras split) trusts
    entry 0 as "the" primary repo; ordering the OUTER list by `updated_at` too would
    make that flip mid-session every time anyone pushes to any branch, which is
    exactly the instability `materializeAll`'s own doc comment says it is trusting
    the control plane not to produce.
    """
    rows = await pool.fetch(
        """SELECT r.owner, r.name, r.clone_url, n.branch_name, n.trunk_branch, n.created_at
             FROM git_node_stories gns
             JOIN git_nodes n ON n.id = gns.git_node_id
             JOIN git_repos r ON r.id = n.git_repo_id
            WHERE gns.story_id = $1
              AND n.state = ANY($2::text[])
              AND n.branch_name IS NOT NULL
            ORDER BY n.updated_at DESC""",
        story_id, await _served_states(pool, story_id),
    )
    by_lane: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row["owner"].lower(), row["name"].lower(),
               (row["trunk_branch"] or "").lower())
        if key in by_lane:
            continue  # first occurrence per LANE = its most recently updated node
        by_lane[key] = {
            "owner": row["owner"], "repo": row["name"], "clone_url": row["clone_url"],
            "branch": row["branch_name"], "trunk": row["trunk_branch"],
            "_created_at": row["created_at"],
        }
    # `created_at` ties to the microsecond for lanes cut in one batch, and a tie
    # makes the sort order arbitrary -- which is how the recorded workspace row
    # for 7f995412 named one lane while the hydrate served the other. Branch
    # name is the tiebreak: arbitrary, but the SAME arbitrary answer every call.
    ordered = sorted(by_lane.values(),
                     key=lambda e: (e["_created_at"], e["branch"] or ""))
    for entry in ordered:
        del entry["_created_at"]

    # Sticky primary (research/NREPO_LOCAL_EXECUTION_PLAN.md §4.8): `created_at ASC`
    # is NOT actually a stable tiebreak for a multi-write story — every repo's node is
    # created in the SAME batched transaction, so they tie on `created_at` down to the
    # microsecond, and Postgres does not guarantee a consistent order for tied rows
    # across separate calls (confirmed live: two checkouts of the same story picked
    # two different repos as entry 0). Once a primary has been decided and recorded
    # (`story_workspace_nodes.is_primary`), honor it unconditionally instead of
    # re-deriving from any sort key — a decision made once must never flip.
    if len(ordered) > 1:
        # (repo, BRANCH), not repo alone: two lanes of one repository both match
        # a bare repo_name, so the rotation below would pick whichever happened
        # to sort first and the "sticky" decision would not be sticky at all.
        prim = await pool.fetchrow(
            "SELECT repo_name, branch_name FROM story_workspace_nodes "
            "WHERE story_id = $1 AND is_primary LIMIT 1",
            story_id,
        )
        if prim and prim["repo_name"]:
            primary_lower = (prim["repo_name"] or "").lower()
            primary_branch = (prim["branch_name"] or "").lower()
            idx = next(
                (i for i, e in enumerate(ordered)
                 if e["repo"].lower() == primary_lower
                 and (not primary_branch
                      or (e["branch"] or "").lower() == primary_branch)), None,
            )
            if idx is not None and idx != 0:
                ordered.insert(0, ordered.pop(idx))
    return ordered


async def _multi_repo_incomplete(pool, story_id: str, live_count: int) -> bool:
    """True when this story is granted more WRITE repos than currently have a live cut
    branch — i.e. at least one repo's branch is still being cut by the engine.

    Write repos only. Read-only (`needs`) context repos never get a branch, so counting
    them here compares two different populations and the predicate can never become
    False. See `count_story_write_repos`.

    This is the local-execution analog of `operator_provisioning.
    is_write_repo_set_incomplete` (MULTI_REPO_STORIES_PLAN.md §12), but the fix it
    enables is different, not the same one: the operator lane can safely RE-provision
    once the missing branch lands, because SHCA dispatch retries continuously. A local
    checkout is a one-shot client action — `CheckoutManager.checkout()` runs exactly
    once — so there is no later pass that would ever add the missing repo. The only
    safe response here is to keep reporting "not ready" (both hydrate's `repo` field
    and `mint_repo_clone_token` stay refused) so the extension's existing
    `waitForBranch` poll loop keeps waiting, exactly as it already does for the
    single-repo case, until every granted repo is live at once.
    """
    from app.services.story_repo_scope import count_story_write_repos  # noqa: PLC0415
    # WRITE repos only. A read-only (`needs`) context repo never gets a branch cut for
    # it, so it can never appear in `live_count` — counting it as granted made the two
    # sides permanently unequal and this predicate permanently True. Every local
    # checkout of a story with any read-only grant was then refused with
    # `repo: null`, which the extension reports as "the engine hasn't cut this story's
    # feature branch yet" — a message that can never come true, so retrying never helps.
    # Confirmed live 2026-08-25 on goal AGRF1: 3 granted (1 modifies + 2 needs) against
    # 1 live branch. `is_write_repo_set_incomplete`, the operator-lane equivalent, has
    # always filtered on `is_write`; this is the same rule, finally applied here too.
    granted = await count_story_write_repos(pool, story_id)
    return granted > live_count


async def _repo_ref(pool, story_id: str) -> Optional[dict]:
    """The engine-cut feature branch the extension clones (§8.3). None until cut.

    SINGLE-REPO SHIM over `_resolve_live_repos`, kept for every caller that has not
    declared the `multi-repo` capability. Refuses (returns None) rather than picking
    one when several live repos exist — an ambiguous answer would let the clone, read
    token, and write token each resolve independently and disagree, so a write token
    could be scoped to a repo the user never cloned with nothing downstream noticing.

    Multi-repo stories are already blocked from local checkout in
    `local_checkout._import_readiness`, and from acquiring a second repo in
    `resource_allocation`, for any client that has not declared the capability. This is
    the last line of defence: if a story reaches here with several live nodes anyway,
    both of those were bypassed, so fail loudly instead of guessing. See
    `research/LOCAL_EXEC_MULTIREPO_INTEGRATION.md` and `research/
    NREPO_LOCAL_EXECUTION_PLAN.md`.
    """
    repos = await _resolve_live_repos(pool, story_id)
    if not repos:
        return None
    if len(repos) > 1:
        _log.error(
            "local_hydrate: story=%s has %d live repos (%s) — refusing to resolve a "
            "single repo ref for a caller that hasn't declared multi-repo support.",
            story_id, len(repos),
            ", ".join(sorted(f"{r['owner']}/{r['repo']}" for r in repos)),
        )
        return None
    return repos[0]


async def build_hydration_bundle(
    story_id: str, *, path_contract: bool = False, multi_repo: bool = False,
) -> dict:
    """Everything the extension needs to open the story locally (read-only).

    `multi_repo` (the calling client declared the capability) switches repo
    resolution from `_repo_ref`'s single-repo shim to `_resolve_live_repos` — but
    ONLY reports the set as ready once `_multi_repo_incomplete` says every granted
    repo has a live branch. Reporting a partial set here would send the extension's
    `waitForBranch` poll loop back a `bundle.repo` it treats as "done waiting", and a
    local checkout never gets a second pass to pick up the repo that arrived late
    (see `_multi_repo_incomplete`'s doc comment) — so an incomplete set must look
    identical to "not cut yet" (`repo: null`), not a subset of the truth.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        """SELECT id, title, goal_id, current_phase, story_kind, execution_mode,
                  current_plan_id, current_plan_path
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story:
        return {"ok": False, "reason": "story_not_found"}
    plan = None
    if story["current_plan_id"]:
        prow = await pool.fetchrow(
            "SELECT plan_doc_path, task_dag FROM plan_versions WHERE id = $1",
            story["current_plan_id"],
        )
        if prow:
            plan = {"plan_path": prow["plan_doc_path"], "task_dag": prow["task_dag"]}
    # THE enforcement point for "a workspace is never cloned unrecorded".
    #
    # The absence of a `story_workspace_nodes` row is the system-wide "legacy workspace"
    # flag, so a clone that happens without one would permanently read as legacy and
    # send every consumer looking under `repo/`. This is the right place to guarantee it
    # because the row write and the repo resolution share a precondition — both need the
    # engine to have cut the branch — and because this function is the ONLY thing that
    # hands the extension a repo to clone. Recording at checkout is an optimisation
    # (the row exists earlier for other readers); recording here is the guarantee.
    #
    # Idempotent: `record_local_workspace_node` preserves an existing subdir, so a
    # resume or a re-hydrate never moves a clone that is already on disk.
    repo: Optional[dict] = None
    if multi_repo:
        live = await _resolve_live_repos(pool, story_id)
        if live and await _multi_repo_incomplete(pool, story_id, len(live)):
            _log.info(
                "local_hydrate: story=%s has %d live repo(s) but its granted set is "
                "still incomplete — reporting not-ready rather than a partial set",
                story_id, len(live),
            )
            live = []
        repo = live[0] if live else None
    else:
        repo = await _repo_ref(pool, story_id)
    if repo:
        from app.services.local_workspace_nodes import (  # noqa: PLC0415
            record_local_workspace_node,
        )
        try:
            recorded = await record_local_workspace_node(
                pool, story_id, path_contract=path_contract, multi_repo=multi_repo,
            )
        except Exception as exc:  # noqa: BLE001 — must not be swallowed; see above
            _log.error(
                "local_hydrate: could not record the workspace layout for story=%s "
                "(%s) — refusing to serve a repo the extension would clone unrecorded",
                story_id, exc,
            )
            return {"ok": False, "reason": "workspace_record_failed"}
        if not recorded:
            _log.error(
                "local_hydrate: workspace layout unrecorded for story=%s — refusing to "
                "serve a repo", story_id,
            )
            return {"ok": False, "reason": "workspace_record_failed"}

    # The repo SET, with each repo's on-disk directory. This is what moves layout
    # ownership from the client to the control plane: `materialize()` has always joined
    # a hardcoded "repo", which cannot be changed without shipping a new extension to
    # everyone. With `path` on the wire the layout becomes a value the CP decides per
    # story, and a future convention change needs no client release at all.
    #
    # The key is OMITTED, not empty, for a legacy workspace. Absence is the flag: an
    # extension that finds no `repos` falls back to "repo" — exactly what a legacy
    # workspace has on disk — while an empty array would be indistinguishable from
    # "recorded, but zero repos", which is not a state that exists.
    #
    # Deliberately unlike the gateway snapshot, which only publishes `repos` when there
    # is more than one (`story_state`): there, the field is extra detail for the
    # multi-repo case. Here it is the addressing scheme, so a single repo needs it too.
    from app.services.local_bundle import build_local_bundle  # noqa: PLC0415
    from app.services.local_resources import docs_for_story  # noqa: PLC0415
    clones = await resolve_workspace_clones(pool, story_id)
    bundle: dict = {
        "ok": True,
        "story": {
            "id": story["id"], "title": story["title"], "goal_id": story["goal_id"],
            "current_phase": story["current_phase"], "story_kind": story["story_kind"],
            "execution_mode": story["execution_mode"],
            "current_plan_path": story["current_plan_path"],
        },
        # Kept for extensions that predate `repos` — they read this and clone into
        # their own hardcoded directory, which is correct for every workspace that
        # has no recorded layout.
        "repo": repo,
        # persona = the legacy gateway SHCA template (kept for back-compat with older
        # extensions). local_bundle = the single-role local assistant the harness
        # actually boots as (agent_md + claude_md); the extension prefers it.
        "persona": await _persona(pool),
        "local_bundle": await build_local_bundle(story_id),
        "plan": plan,
        # THE DOCUMENTS THE LAYOUT ALREADY PROMISED. Every local
        # session's CLAUDE.md draws `docs/ — resources served to you`,
        # and until now nothing in this lane put anything there: the
        # pod builds it from the same grants and the editor did not, so
        # the same story could read its brief in one lane and not the
        # other while being told in both that it could.
        #
        # The list only; the bytes come one at a time from
        # `/internal/local/resource-object`, which re-checks the same
        # grant. Inlining them would put a 25MB PDF in a JSON body the
        # extension has to hold in memory to parse.
        "docs": await docs_for_story(pool, story_id),
    }
    if clones:
        bundle["repos"] = to_bundle_repos(clones)
    return bundle


async def mint_repo_clone_token(story_id: str, *, multi_repo: bool = False) -> dict:
    """Repo-scoped, READ-only, short-lived GitHub token for the local clone (§8.2).

    ONE token authenticates every repo in the returned `repos[]` — a GitHub App
    installation token is naturally scoped to a repo LIST (`generate_installation_token`
    already takes one), so N repos still need exactly one mint, matching the
    cloud/operator lane's `token_repo_names_for` convention. `repos` is present only
    when there is more than one repo — a single-repo response is byte-for-byte
    unchanged from before this capability existed.
    """
    pool = get_pool()
    if not multi_repo:
        repo = await _repo_ref(pool, story_id)
        if not repo:
            return {"ok": False, "reason": "no_branch_yet"}
        tok = await generate_installation_token(
            [repo["repo"]], permissions={"contents": "read", "metadata": "read"},
        )
        return {"ok": True, "repo": repo, "token": tok["token"], "expires_at": tok.get("expires_at")}

    repos = await _resolve_live_repos(pool, story_id)
    if not repos or await _multi_repo_incomplete(pool, story_id, len(repos)):
        return {"ok": False, "reason": "no_branch_yet"}
    # ONE NAME PER REPOSITORY. `repos` is one entry per CLONE, and two
    # lanes of one repository are two clones of the SAME repo -- so this
    # list carried the repo name twice, and GitHub is asked to scope a
    # token to a duplicate. Before lanes were resolved separately the
    # list could never repeat, which is why the dedupe was not needed
    # until now (story 7f995412, dev: the clone silently did not happen
    # and `repos/` was left empty).
    tok = await generate_installation_token(
        list(dict.fromkeys(r["repo"] for r in repos)),
        permissions={"contents": "read", "metadata": "read"},
    )
    result = {
        "ok": True, "repo": repos[0], "token": tok["token"], "expires_at": tok.get("expires_at"),
    }
    if len(repos) > 1:
        result["repos"] = repos
    return result



async def list_local_stories(uid: str) -> list[dict]:
    """The caller's stories eligible for local work (the extension's 'My Stories').
    Shows every ACCEPTED story assigned to the caller in an ACTIVE goal, plus their own
    in-flight local story (so a checked-out/crashed one can resume). Import readiness
    (dependencies terminal) is enforced at checkout, not here — an accepted story with
    unmet deps still shows, but checkout refuses it until its deps finish. A story
    running on the gateway (in_progress, non-local) is not shown."""
    rows = await get_pool().fetch(
        """SELECT s.id, s.title, s.goal_id, s.state, s.current_phase, s.execution_mode,
                  s.checked_out_to_uid,
                  COALESCE((
                      SELECT array_agg(d.title)
                        FROM stories d
                       WHERE d.id = ANY(s.depends_on)
                         AND d.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
                  ), ARRAY[]::text[]) AS blocked_by
             FROM stories s
             JOIN goals g ON g.id = s.goal_id
            WHERE s.assigned_to = $1
              AND g.status = 'active'
              AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
              -- PROPOSED TOO, when it is already assigned to the caller.
              -- Accepting is one button on the web board, and making
              -- somebody go there to press it before they can start the
              -- story they have already been given is a round trip for
              -- nothing. `/work` in Slack has always accepted-and-
              -- imported in one step; this is the same thing for the
              -- extension, and `_import_readiness` now allows it.
              AND (s.state IN ('accepted', 'proposed')
                   OR s.execution_mode = 'local')
              -- Local execution is for HUMAN-authored code stories only. Engine-owned
              -- stories stay in the Git Engine lane: merge stories land on PR-merge (never
              -- worked), and engine diff/fix stories (created_by='engine') are auto-dispatched
              -- by the engine — importing either locally would be nonsensical / double-drive.
              AND s.story_kind <> 'merge'
              AND s.created_by IS DISTINCT FROM 'engine'
              -- AND NOT an AUTONOMOUS errand. A hand-started errand is
              -- welcome here: `kernel.go.drivable` says why, in the other
              -- direction -- an errand somebody started by hand "belongs to
              -- the assistant that started it", and a local session IS that
              -- assistant's conversation. An errand a person approved to run
              -- unattended belongs to the GO and a pod, and importing it
              -- locally double-drives the same branch.
              AND NOT {AUTONOMOUS}
            ORDER BY s.updated_at DESC LIMIT 100""".replace(
            "{AUTONOMOUS}", autonomous_errand_predicate("s")),
        uid,
    )
    out = []
    for r in rows:
        d = dict(r)
        blocked_by = list(d.pop("blocked_by", []) or [])
        # Ready to import = accepted with no unmet deps, OR my own in-flight local story
        # (resume, exempt). Mirrors _import_readiness in local_checkout.
        # `awaiting_merge` IS RESUMABLE TOO -- see `_import_readiness`, which
        # this mirrors and which was fixed with it. Left out, the story is
        # listed as not ready and the sidebar offers no way back into the
        # state a local story now spends most of its life in.
        is_resume = d["execution_mode"] == "local" and d["state"] in (
            "in_progress", "blocked", "awaiting_merge")
        d["ready"] = is_resume or (
            d["state"] in ("accepted", "proposed") and not blocked_by)
        d["blocked_by"] = blocked_by
        out.append(d)
    return out
