"""Local Execution Mode — per-story checkout lock (M0).

Lets a local editor claim a story so the gateway dispatcher never touches it
(see the checkout gates in phase_dispatcher / dag_scheduler / merge_orchestrator).
Claim is an atomic CAS; a lock past its expiry is reclaimable. No execution here
— just the lock lifecycle. See LOCAL_EXECUTION_MODE_PLAN.md §4.2 / §14.5.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_pool
from kernel.go.drivable import is_autonomous_errand
from app.services.story_repo_scope import (
    count_story_repos,
    multi_repo_local_refusal_message,
)

_log = logging.getLogger(__name__)

# How long a checkout stays valid without a heartbeat before it's reclaimable.
CHECKOUT_TTL_SECONDS = 300  # 5 min; extended on every heartbeat

_TERMINAL = ("done", "reviewed", "cancelled", "rejected")


async def _import_readiness(
    pool, story_id: str, goal_id: str, *, multi_repo: bool = False,
) -> Optional[tuple[str, str]]:
    """Gate a local import (§ policy): a story is importable ONLY if it was ACCEPTED on
    the board (no local auto-accept of a proposed story) AND every story it depends on
    is terminal. A story already checked out locally (in_progress/blocked + local mode)
    is a RESUME and always allowed. Returns (reason, message) to refuse, or None to allow.

    `multi_repo` is the caller's declared capability (research/
    NREPO_LOCAL_EXECUTION_PLAN.md, C9) — relaxes the repo-count refusal below for a
    client that can actually clone/diff/push more than one repo. It does NOT relax
    anything else here: whether every granted repo's branch is actually live yet is a
    separate, later question (`local_hydrate._multi_repo_incomplete`), because a local
    checkout has no re-provisioning pass to catch a repo that arrives late — see that
    function's own doc comment.
    """
    story = await pool.fetchrow(
        "SELECT state, execution_mode, depends_on, story_kind, created_by "
        "FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id,
    )
    if not story:
        return ("not_found", "story not found")
    # Local execution is for HUMAN-authored code stories only. Engine-owned stories stay in
    # the Git Engine lane and are never worked in a local session: merge stories land on
    # PR-merge, and engine diff/fix stories (created_by='engine') are auto-dispatched by the
    # engine — importing either locally is nonsensical / a double-drive.
    if (story["story_kind"] or "code") == "merge":
        return ("engine_owned",
                "This is a Git Engine merge story — it lands automatically when its PR merges; "
                "it isn't worked locally.")
    if (story["created_by"] or "") == "engine":
        return ("engine_owned",
                "This is an engine-managed story (auto-integrated by the Git Engine); "
                "it isn't worked locally.")
    # MULTI-REPO INTERLOCK (audit decision D3). Local execution resolves the story's
    # repo as a SINGLE value that independently feeds the clone, the read token and the
    # push token, and the local landing path reports success without a per-repo check —
    # so an N-repo story would silently land one repo and approve as if all had landed.
    # Refused server-side until local execution has a real repo-set contract.
    #
    # Placed BEFORE the resume short-circuit below on purpose: a story that acquired a
    # second repo after being checked out must also be refused, rather than resuming and
    # quietly working whichever repo `_repo_ref` happens to pick. `resource_allocation`
    # blocks that grant too, so this is the second layer, not the only one.
    repo_count = await count_story_repos(pool, story_id)
    if repo_count > 1 and not multi_repo:
        _log.warning(
            "local_checkout: refusing local import of story=%s goal=%s — targets %d "
            "repos and the calling client hasn't declared multi-repo support",
            story_id, goal_id, repo_count,
        )
        return ("multi_repo_unsupported", multi_repo_local_refusal_message(repo_count))

    # AN AUTONOMOUS ERRAND IS NOT LOCAL WORK, and a hand-started one is.
    # `kernel.go.drivable` draws the line and this is the same line read
    # the other way: an errand somebody started by hand "belongs to the
    # assistant that started it", and a local session is where that
    # assistant's person is. An errand approved to run unattended belongs
    # to the GO and a pod -- and because the kernel cannot see
    # `execution_mode`, importing one locally does not replace the pod
    # run, it ADDS to it: two writers on one branch, neither aware.
    #
    # BEFORE THE RESUME SHORT-CIRCUIT, for the reason the multi-repo
    # interlock above gives: a story that ACQUIRED a grant after being
    # checked out must be refused too, not resumed.
    #
    # THE ERROR IS NOT SWALLOWED. `is_autonomous_errand` raises rather
    # than answering False on a bad query, so a database that cannot say
    # refuses the import instead of admitting it.
    try:
        if await is_autonomous_errand(pool, goal_id, story_id):
            return ("autonomous_errand",
                    "This errand runs unattended on an approved standing grant — "
                    "it belongs to the goal's orchestrator, not to a local "
                    "session. Revoke its grant to work it by hand, or start a "
                    "new errand from the goal.")
    except Exception:                                       # noqa: BLE001
        _log.exception(
            "local_checkout: could not establish whether story=%s goal=%s is an "
            "autonomous errand — refusing the import", story_id, goal_id)
        return ("autonomous_errand_unknown",
                "Could not establish whether this errand runs unattended, so it "
                "is not importable right now. Try again in a moment.")

    state, mode = story["state"], (story["execution_mode"] or "")
    # RESUMING MY OWN LOCAL STORY, and `awaiting_merge` belongs in that list.
    #
    # It was written before that state existed and never revisited, so a
    # story whose work is pushed and whose pull request is open could not
    # be re-imported at all: the refusal said "Accept this story on the
    # board before importing it locally", about a story nobody can accept
    # because it is years past `accepted`. Seen on 66317ffc, 2026-10-14.
    #
    # THAT IS THE STATE A LOCAL STORY SPENDS MOST OF ITS LIFE IN now that
    # the lane waits for the merge, and it is exactly when a person needs
    # the checkout back: to mark it done, to ask for more work, or to take
    # the trunk in. Losing the window used to mean losing the story.
    if mode == "local" and state in ("in_progress", "blocked", "awaiting_merge"):
        return None  # resume of my own local story
    # PROPOSED IS IMPORTABLE, AND THAT IS A DELIBERATE POLICY CHANGE.
    #
    # This used to refuse anything but `accepted`: "Accept this story on
    # the board before importing it locally", on the rule that there be
    # no local auto-accept of a proposed story. In practice that is a
    # round trip to the web board to press one button, for a story
    # already assigned to the person asking -- and the rest of the
    # system does not hold that line anyway: Slack's `/work` accepts
    # and imports in one step, and `_prepare_local_story` below has
    # always moved `proposed/accepted -> in_progress`, so the machinery
    # was there and only this gate stood in front of it.
    #
    # THE PROTECTION THAT MATTERED IS SOMEWHERE ELSE AND STAYS. Only the
    # assignee may check a story out, and `local_checkout` enforces that
    # before it reaches here -- so "accepted" was never what stopped
    # somebody taking a story that was not theirs.
    if state not in ("accepted", "proposed"):
        return ("not_importable",
                f"A story in `{state}` cannot be imported locally.")
    deps = list(story["depends_on"] or [])
    if deps:
        unmet = await pool.fetch(
            "SELECT title FROM stories WHERE id = ANY($1::text[]) "
            "AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')",
            deps,
        )
        if unmet:
            titles = ", ".join(r["title"] for r in unmet)
            return ("blocked_by_deps", f"Blocked — depends on unfinished stories: {titles}")
    return None


async def _prepare_local_story(
    pool, story_id: str, goal_id: str, uid: str, *,
    first_checkout: bool | None = None, path_contract: bool = False,
    multi_repo: bool = False,
) -> bool:
    """Local checkout == import: bind the story to the user's assistant, drive it to
    in_progress, cut its branch, and RECORD the workspace layout.

    Thread binding reuses the CANONICAL `provision_assistant_thread_for_story` — the
    same path the web "Open in assistant" / Slack /work use — so the story attaches to
    the user's SINGLE (goal, user) assistant thread (all the user's stories in a goal
    share one thread, matching the UI) and moves proposed/accepted → in_progress. That
    in_progress state is also what the git engine's LAZY branch cut needs
    (`branches._is_cut_ready`), so we then reconcile + run the cut pass to create the
    feature branch.

    Returns whether the workspace layout was recorded. A False here is informational,
    NOT fatal: the engine's branch cut is lazy, so on a fresh story the branch often does
    not exist yet, there is nothing to record, and the user simply retries. The invariant
    that matters — a workspace is never CLONED unrecorded — is enforced in
    `build_hydration_bundle`, the only thing that hands the extension a repo.

    The row itself is not bookkeeping, though: its ABSENCE is the system-wide "this is a
    legacy workspace" flag, so a silently-missing row would make a brand-new workspace
    read as legacy and send every consumer looking under `repo/` for a directory that was
    never created there."""
    try:
        # SHCA identity row (token aiid + memory key) — no live gateway agent.
        from app.services.local_assistant import (  # noqa: PLC0415
            bind_local_story_thread, ensure_local_assistant_instance,
        )
        await ensure_local_assistant_instance(goal_id, uid, story_id)

        # Local-aware import: single shared (goal,user) thread + active-focus + in_progress.
        # (The gateway provision path refuses local stories, so we bind directly here.)
        try:
            await bind_local_story_thread(goal_id, uid, story_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("local checkout: assistant-thread bind failed for %s: %s", story_id, exc)

        from app.services.git_graph.reconcile import reconcile  # noqa: PLC0415
        await reconcile(goal_id)
        from app.services.git_graph.branches import run_create_pass  # noqa: PLC0415
        await run_create_pass(goal_id)

    except Exception as exc:  # noqa: BLE001 — branch prep alone never blocks the claim
        _log.warning("local checkout: branch prep failed for %s: %s", story_id, exc)

    # Record the clone in `story_workspace_nodes`. Runs AFTER the cut pass because the
    # row's branch_name is part of its primary key and comes from the cut branch, and
    # OUTSIDE the try above so a prep warning cannot mask a recording failure.
    #
    # Recording here is an OPTIMISATION: it puts the row in place early, so the readers
    # that run before the extension hydrates (resources doc, DAG validation, credential
    # broker) see a recorded layout rather than falling back to legacy. The guarantee
    # lives at hydrate — see the docstring.
    try:
        from app.services.local_workspace_nodes import (  # noqa: PLC0415
            record_local_workspace_node,
        )
        recorded = await record_local_workspace_node(
            pool, story_id,
            first_checkout=first_checkout, path_contract=path_contract,
            multi_repo=multi_repo,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, not swallowed
        _log.error(
            "local checkout: workspace-node record RAISED for story=%s: %s — refusing "
            "the checkout so the workspace is not used unrecorded", story_id, exc,
        )
        return False

    if not recorded:
        # No branch cut yet (the engine's lazy cut may not have caught up). Correct to
        # refuse: recording nothing here is indistinguishable from a legacy workspace.
        # The user retries once the branch exists.
        _log.warning(
            "local checkout: no workspace node recorded for story=%s (branch not cut "
            "yet or repo unresolvable) — refusing the checkout", story_id,
        )
        return False
    return True


def _now_and_expiry() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now, now + timedelta(seconds=CHECKOUT_TTL_SECONDS)


async def claim_checkout(
    story_id: str, goal_id: str, uid: str, *,
    path_contract: bool = False, multi_repo: bool = False,
) -> dict:
    """Atomically claim a story for local execution.

    Wins only if it's not already held by someone else on a LIVE (non-expired)
    lock. Sets execution_mode='local' so the gateway gates exclude it. Returns
    {ok, checkout_session_id, expires_at} or {ok:False, reason,...}.
    """
    pool = get_pool()
    # ONLY THE ASSIGNEE. `uid` comes from the request body, and nothing checked it:
    # any holder of a token for the story could claim it in somebody else's name. The
    # story token is minted only for the assignee (`mint_story_token`), so this is the
    # same rule applied at the door that actually takes the lock.
    assigned = await pool.fetchval(
        "SELECT assigned_to FROM stories WHERE id = $1 AND goal_id = $2", story_id, goal_id)
    if (assigned or "").strip() != uid:
        return {"ok": False, "reason": "not_assignee",
                "error": "only the person this story is assigned to can check it out"}
    # Import gate: only an accepted story with all dependencies terminal may be
    # imported locally (a resume of my own local story is exempt). Checked BEFORE the
    # lock claim so a not-ready story never gets checked out.
    refusal = await _import_readiness(pool, story_id, goal_id, multi_repo=multi_repo)
    if refusal:
        return {"ok": False, "reason": refusal[0], "error": refusal[1]}
    # Concurrency guard: the same limit the board and the assistant import
    # enforce — one story for the operator, four live threads for the kernel
    # assistant. A local checkout is a story going into flight like any other, so
    # it counts against the same ceiling; enforcing a different rule here is how
    # a person ends up with five when the limit says four. Resuming my OWN story
    # is exempt (the story being started is excluded from the count). Checked
    # BEFORE the lock claim so a refused checkout never mutates anything or
    # clones a repo.
    from app.services.thread_ceiling import refusal_for  # noqa: PLC0415
    refusal_text = await refusal_for(pool, goal_id, uid, story_id)
    if refusal_text:
        return {"ok": False, "reason": "user_busy", "error": refusal_text}
    # Story provenance, captured BEFORE the UPDATE below overwrites `checked_out_at`.
    # This is the only moment it is knowable: after the claim, a brand-new story and a
    # story checked out months ago look identical. It decides whether a NEW workspace
    # may use the `repos/<owner>__<name>` layout, because a story that was checked out
    # before has a clone sitting in `repo/` on someone's disk — with that session's
    # bridge token, agent bundle, memory and possibly uncommitted work. NULL means it
    # has never been checked out, which covers both a fresh story and an old story that
    # was never imported: in both cases nothing exists on disk, which is the fact that
    # actually matters. See `local_workspace_nodes._subdir_for`.
    prior_checkout_at = await pool.fetchval(
        "SELECT checked_out_at FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id,
    )
    first_checkout = prior_checkout_at is None

    session_id = uuid.uuid4().hex
    now, expires = _now_and_expiry()
    row = await pool.fetchrow(
        """
        UPDATE stories
           SET checked_out_to_uid = $3, checkout_session_id = $4,
               checked_out_at = $5, checkout_expires_at = $6,
               execution_mode = 'local', updated_at = now(),
               -- A NEW SESSION OWES ITS OWN CHECKPOINT. The stamp is what
               -- suppresses the gateway story-complete flush; one left over
               -- from a previous cycle would suppress it for work this
               -- session has not done and cannot have consolidated.
               local_memory_consolidated_at = NULL
         WHERE id = $1 AND goal_id = $2
           AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
           AND (checked_out_to_uid IS NULL
                OR checked_out_to_uid = $3
                OR checkout_expires_at < now())
        RETURNING checkout_session_id, checkout_expires_at
        """,
        story_id, goal_id, uid, session_id, now, expires,
    )
    if not row:
        held = await pool.fetchrow(
            "SELECT checked_out_to_uid, state FROM stories WHERE id=$1 AND goal_id=$2",
            story_id, goal_id,
        )
        if not held:
            return {"ok": False, "reason": "not_found"}
        if held["state"] in _TERMINAL:
            return {"ok": False, "reason": "terminal", "state": held["state"]}
        return {"ok": False, "reason": "held_by_other", "held_by": held["checked_out_to_uid"]}
    # The lock is ours: import the story (accept if needed) + reconcile so the engine
    # cuts the feature branch the extension will clone, then record the layout.
    #
    # A false return is NOT fatal here. The engine's branch cut is lazy, so on a fresh
    # story the branch often does not exist yet and there is nothing to record — the
    # extension handles that at hydrate ("the engine hasn't cut this story's feature
    # branch yet") and the user retries. The invariant that matters — a workspace is
    # never CLONED unrecorded — is enforced in `build_hydration_bundle`, which is the
    # only place that hands the extension a repo to clone.
    await _prepare_local_story(
        pool, story_id, goal_id, uid,
        first_checkout=first_checkout, path_contract=path_contract, multi_repo=multi_repo,
    )
    return {
        "ok": True,
        "checkout_session_id": row["checkout_session_id"],
        "expires_at": row["checkout_expires_at"].isoformat(),
    }


async def heartbeat_checkout(story_id: str, session_id: str) -> dict:
    """Extend the lock's expiry — only the holder of this session_id may."""
    pool = get_pool()
    _now, expires = _now_and_expiry()
    row = await pool.fetchrow(
        """UPDATE stories SET checkout_expires_at = $3, updated_at = now()
            WHERE id = $1 AND checkout_session_id = $2
              AND checked_out_to_uid IS NOT NULL
         RETURNING checkout_expires_at""",
        story_id, session_id, expires,
    )
    if not row:
        return {"ok": False, "reason": "not_holder_or_released"}
    return {"ok": True, "expires_at": row["checkout_expires_at"].isoformat()}


async def release_checkout(story_id: str, session_id: str) -> dict:
    """Release the lock (completion / abandon), fenced by session_id.

    Leaves execution_mode='local' — mode is a deliberate per-story choice; a
    later re-checkout resumes locally. (Reverting to gateway mode is a separate
    action, not part of release.)
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """UPDATE stories
              SET checked_out_to_uid = NULL, checkout_session_id = NULL,
                  checked_out_at = NULL, checkout_expires_at = NULL,
                  updated_at = now()
            WHERE id = $1 AND checkout_session_id = $2
         RETURNING id""",
        story_id, session_id,
    )
    if not row:
        return {"ok": False, "reason": "not_holder_or_already_released"}
    return {"ok": True}


async def reap_local_checkouts() -> dict:
    """Resilience backstop (§14.5): guarantees no story stays "checked out forever".

    (A) A live checkout whose heartbeat expired (crash / abandon) has its lock freed
        so it's re-checkoutable — execution_mode stays 'local' (sticky by design;
        re-open re-hydrates from the CP).
    (B) A terminal story still carrying checkout columns / local mode is fully
        cleaned and reverted to the gateway default 'operator'.
    Idempotent; safe to run on a periodic tick."""
    pool = get_pool()
    expired = await pool.fetch(
        """UPDATE stories
              SET checked_out_to_uid = NULL, checkout_session_id = NULL,
                  checked_out_at = NULL, checkout_expires_at = NULL, updated_at = now()
            WHERE checked_out_to_uid IS NOT NULL
              AND checkout_expires_at < now()
              AND state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
              -- AND NOT WHILE IT IS WAITING FOR A MERGE. This clause
              -- frees a lock whose heartbeat stopped, on the reading
              -- that a live checkout with no heartbeat was abandoned.
              -- That reading is right while somebody is working and
              -- wrong the moment the story is waiting on a PR: the wait
              -- is measured in hours or days, the person is not at
              -- their desk, and closing the editor would hand their
              -- story to somebody else.
              --
              -- An abandoned one is still reaped — by the
              -- `awaiting_merge` deadline, which is the same deadline
              -- the pod lane uses and is measured in days rather than
              -- in heartbeats.
              AND state <> 'awaiting_merge'
         RETURNING id""",
    )
    # `done` IS NOT TERMINAL FOR A STORY THAT OWES A REVIEW. The review runs
    # in the author's own session AFTER the story reaches `done`, and it takes
    # as long as reading every changed file takes -- minutes. This tick is
    # every 60s, so it used to reclaim the checkout out from under a review
    # in progress: the lease stops being refreshed, the story stops looking
    # held, and another session can claim it while the first is still
    # reviewing. `reviewed` is the terminal state for those; `done` is a
    # waypoint. (Found while testing the review flow on dev, 2026-10-06.)
    terminal = await pool.fetch(
        """UPDATE stories
              SET checked_out_to_uid = NULL, checkout_session_id = NULL,
                  checked_out_at = NULL, checkout_expires_at = NULL,
                  execution_mode = 'operator', updated_at = now()
            WHERE state IN ('done', 'reviewed', 'cancelled', 'rejected')
              AND (checked_out_to_uid IS NOT NULL OR execution_mode = 'local')
              AND NOT (
                    state = 'done'
                AND coalesce(work_kind, 'story') <> 'errand'
                AND coalesce(story_kind, '') <> 'merge'
                AND coalesce(created_by, '') <> 'engine'
                AND NOT EXISTS (SELECT 1 FROM story_reviews sr
                                 WHERE sr.story_id = stories.id)
              )
         RETURNING id""",
    )
    return {"reclaimed": [r["id"] for r in expired], "cleaned": [r["id"] for r in terminal]}
