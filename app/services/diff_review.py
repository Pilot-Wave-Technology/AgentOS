"""Diff-review flow at DAG completion (Stage 3-l).

After all phase subtasks of a DAG transition to `done`:
  1. Capture the workspace diff (HEAD-vs-staged + unstaged) via gateway
  2. Write the patch to `docs/diff.patch` in the workspace so it shows
     up in the file viewer
  3. Post a diff-review card to chat with Approve / Reject buttons
  4. On human approve → final commit + push of the implementation,
     story → done
  5. On reject → feedback returns to Claude as a new dispatch
     (essentially a synthetic phase task that says "revise per
     feedback, then re-emit PHASE_TASK_DONE for the DAG parent")
"""
from __future__ import annotations

import json
import logging
import re

import asyncpg
import uuid

from app.database import get_pool
from app.ledger.journal import append_event
from app.openclaw import gateway_admin, GatewayError
from app.services.story_workspace_layout import resolve_workspace_clones, write_clones


_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID  # one canonical, registered actor
from app.ledger.actors import human_actor, instance_actor


_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


_REPO_DIVIDER_RE = re.compile(r"^=== (\S+) ===$", re.MULTILINE)


def _files_by_subdir(patch_text: str) -> dict[str, set[str]]:
    """Split a multi-repo combined patch into per-repo file sets, using the
    `=== repos/Owner__name ===` divider line `begin_diff_review`'s multi-repo
    capture inserts before each repo's own patch segment.

    Each repo's segment reuses its OWN repo-relative paths verbatim — there is
    no subdir prefix on the `diff --git a/X b/X` headers themselves, so the ONLY
    reliable way to attribute a header to a repo is the divider it falls after."""
    dividers = list(_REPO_DIVIDER_RE.finditer(patch_text))
    out: dict[str, set[str]] = {}
    for i, m in enumerate(dividers):
        subdir = m.group(1)
        seg_start = m.end()
        seg_end = dividers[i + 1].start() if i + 1 < len(dividers) else len(patch_text)
        segment = patch_text[seg_start:seg_end]
        out.setdefault(subdir, set()).update(
            a_path for a_path, _b_path in _DIFF_GIT_HEADER_RE.findall(segment)
        )
    return out


async def _discard_plan_only_sibling_writes(
    *, instance_id: str, story_id: str, current_plan_path: str | None, diff_patch_path: str,
) -> None:
    """Guard against landing a spurious commit into a write repo this story never
    actually needed to touch.

    A ripple-check sibling grant gives a story `modifies` access to every live sibling
    in its merge group defensively, in case the work spans them. `_copy_plan_to_other_
    write_repos` (plan_approval.py) then mirrors the plan + scope.md into every one of
    those repos so a task that DOES end up working there can find it. But the final
    approve step commits+pushes EVERY write repo unconditionally (`allRepos: True,
    paths: ["all"]`) — so a sibling the story never actually touched still gets a real
    commit containing nothing but that mirrored plan/scope scaffolding, landed on its
    live branch. Confirmed live (goal VKSDN, story ds_a3b026e09e4b18): agent_os_openclaw
    and agentos each got a 2-file/141-insertion commit pushed for a sync story
    whose only real work was in multi_agent_dummy_repo.

    Deletes the mirrored plan/scope files (both best-effort, uncommitted, so a delete
    is exactly the discard the extension would call `git checkout --`) from any write
    repo whose ENTIRE diff is that scaffolding and nothing else, so the subsequent
    gitCommit finds nothing there and silently skips it. A repo with ANY other change
    is left untouched. Never touches `clone_root` itself — this only ever considers
    OTHER write repos, so a single-repo exploratory story whose only deliverable IS a
    plan/analysis document is unaffected (there is no sibling to discard from, and its
    own repo is never a candidate).

    Splits `docs/diff.patch` on its `=== repos/Owner__name ===` divider lines rather
    than trying to bucket `diff --git a/X b/X` headers by a subdir PREFIX — a prior
    version assumed those headers were prefixed with the repo's subdir
    (`repos/Owner__name/...`); they are NOT — every repo's segment reuses its OWN
    repo-relative paths verbatim, so prefix-matching silently matched nothing and
    this guard no-op'd for every sibling. Confirmed live (goal VKSDN, story
    ds_3b41012654528a): both siblings' plan-only commits landed anyway despite this
    guard existing. The divider is the only thing that actually distinguishes one
    repo's segment from the next."""
    _log.debug(
        "diff_review: plan-only-sibling check starting (story=%s plan_path=%s)",
        story_id, current_plan_path,
    )
    if not current_plan_path:
        _log.debug(
            "diff_review: plan-only-sibling check skipped — story=%s has no "
            "current_plan_path recorded", story_id,
        )
        return
    from app.services.story_plan_root import resolve_plan_root  # noqa: PLC0415
    pool = get_pool()
    clone_root = await resolve_plan_root(pool, story_id)
    if not current_plan_path.startswith(f"{clone_root}/"):
        _log.debug(
            "diff_review: plan-only-sibling check skipped — story=%s plan_path=%s "
            "isn't under clone_root=%s (not a canonical in-repo plan path, so the "
            "mirror step never ran)", story_id, current_plan_path, clone_root,
        )
        return
    repo_relative_plan_path = current_plan_path[len(clone_root) + 1:]
    scope_rel = f"stories/{story_id}/scope.md"
    allowed = {repo_relative_plan_path, scope_rel}

    clones = await resolve_workspace_clones(pool, story_id)
    sibling_subdirs = [c["subdir"] for c in write_clones(clones) if c["subdir"] != clone_root]
    if not sibling_subdirs:
        _log.debug(
            "diff_review: plan-only-sibling check skipped — story=%s has no "
            "sibling write repos besides clone_root=%s", story_id, clone_root,
        )
        return

    try:
        patch = await gateway_admin.call(
            "shca.workspace.readFile",
            {"agentId": instance_id, "storyId": story_id, "path": diff_patch_path},
            timeout=30,
        )
        patch_text = patch.get("content") or ""
    except Exception as exc:
        _log.warning(
            "diff_review: couldn't read %s to check for plan-only siblings "
            "(story=%s siblings=%s): %s — leaving all write repos as-is, they will "
            "commit+push exactly as before this guard existed",
            diff_patch_path, story_id, sibling_subdirs, exc,
        )
        return
    changed_by_subdir = _files_by_subdir(patch_text)

    discarded_subdirs: list[str] = []
    for subdir in sibling_subdirs:
        changed = changed_by_subdir.get(subdir)
        if changed is None:
            _log.warning(
                "diff_review: plan-only-sibling check — story=%s repo=%s has no "
                "divider segment in %s at all — the combined patch format may "
                "have changed; leaving it as-is", story_id, subdir, diff_patch_path,
            )
            continue
        if not changed:
            _log.debug(
                "diff_review: plan-only-sibling check — story=%s repo=%s has no "
                "changes at all, nothing to discard", story_id, subdir,
            )
            continue
        if not changed.issubset(allowed):
            _log.info(
                "diff_review: plan-only-sibling check — story=%s repo=%s has real "
                "work (%s) alongside/instead of the mirrored plan/scope — left "
                "alone, will commit+push normally",
                story_id, subdir, ", ".join(sorted(changed - allowed)),
            )
            continue
        _log.info(
            "diff_review: story=%s repo=%s diff is plan/scope scaffolding only "
            "(%s) — discarding before commit so nothing lands there",
            story_id, subdir, ", ".join(sorted(changed)),
        )
        discarded_subdirs.append(subdir)
        for rel in changed:
            try:
                await gateway_admin.call(
                    "shca.workspace.deleteFile",
                    {"agentId": instance_id, "storyId": story_id, "path": f"{subdir}/{rel}"},
                    timeout=30,
                )
            except Exception as exc:
                _log.warning(
                    "diff_review: couldn't discard %s/%s before commit (story=%s): %s "
                    "— it may still land as a plan-only commit", subdir, rel, story_id, exc,
                )
    _log.info(
        "diff_review: plan-only-sibling check complete (story=%s checked=%d "
        "discarded=%d discarded_repos=%s)",
        story_id, len(sibling_subdirs), len(discarded_subdirs), discarded_subdirs,
    )


async def _story_write_repo_count(pool, story_id: str) -> int:
    """How many WRITE repos this story's workspace holds (multi-repo plan M4).

    Read from `story_workspace_nodes` — what provisioning ACTUALLY cloned — rather
    than from the grants, because the diff/commit RPCs operate on what is on disk. A
    story whose second repo was skipped at provisioning (branch not cut yet) must be
    treated as single-repo here, or we would ask the gateway to diff a clone that does
    not exist.

    Returns 0 for a legacy workspace with no node rows; every caller treats <= 1 as
    "use the single-repo path", so 0 and 1 behave identically and pre-multi-repo
    stories are untouched.
    """
    # `strict=True` preserves the rule this function has always enforced: a missing table
    # degrades to single-repo (an un-migrated DB legitimately has no layouts), but any
    # OTHER failure raises rather than quietly becoming "single repo". Quietly answering
    # 1 here would diff and commit only the primary repo while the human approves what
    # they believe is the whole change — a silently partial multi-repo landing. A visible
    # error the human can retry is strictly better than a plausible wrong answer.
    clones = await resolve_workspace_clones(pool, story_id, strict=True)
    return len(write_clones(clones))


async def begin_diff_review(
    goal_id: str, story_id: str, dag_task_id: str,
) -> dict:
    """Called when sentinel_parser detects all DAG subtasks are done.

    Captures the diff (writing patch file in workspace) and posts the
    review card. Returns the diff summary so callers can log.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        """SELECT shca_instance_id, active_thread_id, workspace_path
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story or not story["shca_instance_id"]:
        return {"ok": False, "error": "story / instance missing"}

    instance_id = story["shca_instance_id"]
    thread_id = story["active_thread_id"]

    # Multi-repo (plan M4.1): when the story writes to more than one repo, ask the
    # gateway for every write repo's diff so ONE approval covers the whole change.
    # `_story_write_repo_count` reads what was actually provisioned, so a single-repo
    # story sends the identical params it always did.
    multi_repo = await _story_write_repo_count(pool, story_id) > 1

    # Capture diff via gateway. Also write to docs/diff.patch so the
    # file viewer can render it.
    try:
        diff_result = await gateway_admin.call(
            "shca.workspace.gitDiff",
            {
                "agentId": instance_id,
                "storyId": story_id,
                "scope": "all",  # staged + unstaged
                "writeToPath": "docs/diff.patch",
                **({"allRepos": True} if multi_repo else {}),
            },
            timeout=60 if not multi_repo else 120,
        )
    except GatewayError as exc:
        _log.warning("diff_review: gitDiff RPC failed (story=%s): %s", story_id, exc)
        return {"ok": False, "error": f"diff capture failed: {exc}"}

    diff_text = diff_result.get("diff") or ""
    stat = diff_result.get("stat_summary") or ""
    truncated = bool(diff_result.get("truncated"))
    # Per-repo breakdown, present only for a multi-repo capture. Kept on the approval
    # so the review card can show which repos changed rather than one merged blob.
    per_repo = [
        {
            "subdir": r.get("subdir") or "",
            "stat": r.get("stat_summary") or "",
            "truncated": bool(r.get("truncated")),
        }
        for r in (diff_result.get("repos") or [])
    ]
    if per_repo:
        _log.info(
            "diff_review: story=%s captured a grouped diff across %d repo(s): %s",
            story_id, len(per_repo),
            "; ".join(f"{r['subdir']}={r['stat'] or 'no changes'}" for r in per_repo),
        )

    return await post_diff_review(
        goal_id=goal_id, story_id=story_id, dag_task_id=dag_task_id, thread_id=thread_id,
        diff_text=diff_text, stat=stat, truncated=truncated, diff_path="docs/diff.patch",
        per_repo=per_repo,
    )


async def post_diff_review(
    *, goal_id: str, story_id: str, dag_task_id: str, thread_id: str,
    diff_text: str, stat: str, truncated: bool,
    diff_path: str = "", diff_ref: str = "", commit_sha: str = "",
    per_repo: list | None = None, push_verified: bool | None = None,
) -> dict:
    """Store the diff-review approval + post the card + ledger, with the diff ALREADY
    captured. Shared by the gateway path (`begin_diff_review`, which supplies a
    workspace `diff_path`) and the local path (`/internal/local/diff-submit`, which
    supplies the full diff as `diff_ref` + the already-pushed `commit_sha`, since
    there's no gateway workspace file). Everything here is content-agnostic — the
    only gateway-specific piece is the upstream `gitDiff` capture.

    `per_repo` is the multi-repo breakdown captured by `begin_diff_review`. It MUST be
    a parameter rather than a value read from the enclosing scope: this function has two
    callers (the gateway path here, and `local_diff.submit_local_diff`), and only one of
    them has a repo breakdown to give. Absent/empty means "one repo" — which is what
    every single-repo gateway story and every local story passes, so their card and
    payload are identical to the pre-multi-repo behaviour on `main`."""
    # Normalise once so the payload/card branches below can treat this as a plain list.
    per_repo = per_repo or []
    pool = get_pool()
    approval_id = uuid.uuid4()
    _log.info(
        "post_diff_review: story=%s dag_task=%s approval=%s source=%s repos=%d "
        "diff_bytes=%d truncated=%s",
        story_id, dag_task_id, approval_id,
        "local" if commit_sha else "gateway", len(per_repo), len(diff_text), truncated,
    )
    payload = {
        "dag_task_id": dag_task_id,
        "diff_path": diff_path,
        "stat": stat,
        "truncated": truncated,
        "preview": diff_text[:2000],
        # Empty for a single-repo story — readers should treat absence as "one repo".
        "repos": per_repo,
    }
    if commit_sha:
        payload["commit_sha"] = commit_sha
        # Whether the control plane CONFIRMED that commit is on the remote branch.
        # True = checked and present. False = could not be checked (GitHub unreachable,
        # rate-limited); the submission was still accepted because an outage is not a
        # failed push, but nothing may claim it landed. A definitively-absent commit
        # never reaches here — `submit_local_diff` refuses it outright.
        payload["push_verified"] = bool(push_verified)
    await pool.execute(
        """INSERT INTO approvals (
               id, story_id, kind, payload, payload_ref, channels, proposed_at
           ) VALUES ($1, $2, 'diff', $3::JSONB, $4, ARRAY['ui'], NOW())""",
        approval_id, story_id, json.dumps(payload), diff_ref or None,
    )

    if thread_id:
        card = {
            "kind": "diff_review",
            "approval_id": str(approval_id),
            "dag_task_id": dag_task_id,
            "stat": stat,
            "diff_path": diff_path,
            "truncated": truncated,
            "repos": per_repo,
        }
        # Multi-repo: spell out the per-repo breakdown, and say plainly that the repos
        # merge independently — one approval starts N pull requests, it does not land
        # them together, and the reviewer needs to know that before approving.
        if per_repo:
            repo_lines = "\n".join(
                f"- **`{r['subdir']}/`** — {r['stat'] or 'no changes'}"
                + ("  _(diff truncated)_" if r["truncated"] else "")
                for r in per_repo
            )
            body = (
                f"# Diff ready for review — {len(per_repo)} repositories\n\n"
                f"All DAG tasks completed. This change spans several repositories:\n\n"
                f"{repo_lines}\n\n"
                f"Combined patch saved to **`docs/diff.patch`** — open it in the file "
                f"viewer to review.\n\n"
                f"Approving commits and pushes **every** repository above and opens a "
                f"pull request for each. They merge **independently** — the story is "
                f"complete only once all of them land.\n\n"
                f"Reject with feedback to send Claude back for revisions."
            )
        else:
            # diff_path is empty for the local-execution path (/internal/local/diff-submit),
            # which supplies the diff inline via diff_ref instead of a workspace file.
            where = (
                f"Patch saved to **`{diff_path}`** — open it in the file viewer to review.\n\n"
                if diff_path
                else "The full diff is attached to this review.\n\n"
            )
            body = (
                f"# Diff ready for review\n\n"
                f"All DAG tasks completed. {stat or 'See the diff.'}\n\n"
                f"{where}"
                f"Approve to land the implementation. "
                f"Reject with feedback to send back for revisions."
            )
        msg_id = str(uuid.uuid4())
        await pool.execute(
            """INSERT INTO messages (
                   id, thread_id, role, sender,
                   content, card_data, created_at
               ) VALUES ($1, $2, 'agent', 'task-controller',
                         $3, $4::JSONB, NOW())""",
            msg_id, thread_id, body, json.dumps(card),
        )
        try:
            from app.chat_module.repository import notify_new_message
            await notify_new_message(thread_id, msg_id)
        except Exception:
            _log.exception(
                "diff_review: fan-out notify failed (story=%s) — card is in "
                "DB, page refresh will show it", story_id,
            )
        # The Slack relay used to be a second call here. It now happens
        # inside notify_new_message above, for every card-bearing message,
        # so a new card kind reaches Slack without anybody adding a call --
        # and twenty-four of twenty-seven kinds never got one. Relaying in
        # both places would post the card twice.

    # Ledger. The proposed diff is the SHCA's work → attribute to that instance.
    _dp_actor, _dp_type = await instance_actor(task_id=dag_task_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                actor_id=_dp_actor, actor_type=_dp_type,
                event_type="diff.proposed",
                payload={
                    "story_id": story_id,
                    "dag_task_id": dag_task_id,
                    "approval_id": str(approval_id),
                    "stat": stat,
                    "diff_truncated": truncated,
                },
                context_goal_id=goal_id,
            )

    return {
        "ok": True,
        "approval_id": str(approval_id),
        "stat": stat,
        "truncated": truncated,
    }


async def decide_diff_review(
    *, approval_id: str, decision: str, feedback: str,
    decided_by_uid: str,
) -> dict:
    """Human approves or rejects the diff.

    On 'approve': commit + push everything, mark story done.
    On 'reject': feedback is forwarded to Claude as a synthetic dispatch
        on the DAG parent (new phase=coder task to revise per feedback).
    """
    pool = get_pool()
    approval = await pool.fetchrow(
        "SELECT a.*, s.goal_id FROM approvals a "
        "JOIN stories s ON s.id = a.story_id WHERE a.id = $1",
        approval_id,
    )
    if not approval:
        return {"ok": False, "error": "approval not found"}
    if approval["decided_at"] is not None:
        return {"ok": False, "error": f"approval already {approval['decision']}"}
    if approval["kind"] != "diff":
        return {"ok": False, "error": f"approval kind mismatch: {approval['kind']}"}

    goal_id = approval["goal_id"]
    story_id = approval["story_id"]

    # Kill switch — approve commits+pushes and closes the story; reject dispatches
    # a revision task. Both would bypass a system/goal freeze. Shared with the web
    # "Approve/Reject diff" buttons and the Slack diff_approve/diff_reject buttons +
    # reject modal, so one check covers all of them. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _frozen = await execution_control.frozen_response(goal_id)
    if _frozen is not None:
        _log.info(
            "decide_diff_review refused — execution paused, goal=%s story=%s "
            "approval=%s uid=%s", goal_id, story_id, approval_id, decided_by_uid,
        )
        return _frozen

    payload = approval["payload"] if isinstance(approval["payload"], dict) else json.loads(approval["payload"])
    dag_task_id = payload.get("dag_task_id")
    # The diff decision is a human action → attribute to the deciding user.
    dr_actor, dr_type = await human_actor(decided_by_uid)

    story = await pool.fetchrow(
        """SELECT shca_instance_id, current_branch_name, execution_mode, current_plan_path
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    instance_id = story["shca_instance_id"]
    is_local = story["execution_mode"] == "local"

    if decision == "approve":
        # Bound BEFORE the branch so it exists on every path to the merge point below.
        # The gateway performs its own push and observes the result directly, so True is
        # the correct value there; only the local branch can lower it. (Binding this in
        # one branch and reading it after the join is precisely the defect class
        # `scripts/check_unbound_names.py` was written for.)
        push_confirmed = True
        # Per-repo shas for the ledger (below), multi-repo LOCAL only — bound here for
        # the same unbound-name reason as push_confirmed above. Deliberately NOT
        # merged into `repo_results` (the gateway's own per-repo list, bound further
        # down): that list also drives a "which repos failed to push JUST NOW"
        # narrative that describes a live push attempt at approve-time, which is true
        # for the gateway but not for local — a local push already completed before
        # submit, and `_post_multi_repo_review` already refused the whole submission
        # if any repo's push didn't verify, so there is no partial-failure state left
        # to narrate here, only a completed set to record.
        local_per_repo: list = []

        if is_local:
            # Local story: the extension already committed + pushed the feature
            # branch at diff-submit (the CP can't push a clone that lives on the
            # user's machine). Approval therefore records the decision + runs the
            # same state transitions; the git engine opens the PR once the story
            # settles (§8.3). No gateway RPC on this path.
            commit_sha = payload.get("commit_sha") or ""
            push_error = None
            # `pushed` stays True here, and that is NOT the old blind assertion.
            #
            # It must not carry the verification result, because on the gateway path
            # `pushed=False` means "the commit landed but the push failed, click Approve
            # to retry" — it leaves the approval pending and returns an error. None of
            # that applies locally: the control plane never performs this push, so there
            # is nothing for a retry to do, and routing an unverified local push down
            # that branch would leave the story permanently unapprovable with advice
            # that can never work.
            #
            # The real guarantee moved EARLIER instead. `submit_local_diff` refuses a
            # commit that is definitively not on the remote, so a phantom push never
            # reaches approval at all. What survives to here is at worst "GitHub was
            # unreachable when we checked", which is not a failed push — but must not be
            # reported as a confirmed landing either, hence the separate flag below.
            #
            # A payload with no `push_verified` key predates verification entirely; those
            # approvals behave exactly as they did before, which is why the default is
            # True rather than False.
            #
            # Multi-repo (D-5/C6): `payload["repos"]` non-empty means
            # `local_diff._post_multi_repo_review` verified EACH repo separately and this
            # must be the conjunction of all of them — one unconfirmed repo among several
            # is exactly as unconfirmed as the whole story, mirroring the gateway
            # multi-repo branch's own all-repos-pushed conjunction below. A repo entry
            # with no `push_verified` key (or `None` — "could not check") counts as NOT
            # confirmed here, same as the top-level scalar already gets coerced to by
            # `post_diff_review`'s `bool(push_verified)` when it is written.
            local_per_repo = payload.get("repos") or []
            push_confirmed = (
                all(bool(r.get("push_verified")) for r in local_per_repo) if local_per_repo
                else bool(payload.get("push_verified", True))
            )
            pushed = True
            if not push_confirmed:
                _log.warning(
                    "diff_review: story=%s approving a local diff whose push could not "
                    "be confirmed on the remote (commit=%s) — approval proceeds, but the "
                    "landing is unverified", story_id, commit_sha[:12] or "none",
                )
        else:
            # Refresh GitHub token inline so the credential helper holds a
            # fresh password right before the final push.
            from app.services.token_refresh import refresh_story_token
            refreshed, refresh_err = await refresh_story_token(story_id)
            if not refreshed:
                _log.info(
                    "diff_review: pre-push token refresh skipped/failed "
                    "(story=%s): %s — attempting push with existing credentials",
                    story_id, refresh_err,
                )

            # Final commit + push. Multi-repo (plan M4.2): one approval commits and pushes
            # EVERY write repo. `pushed` below is then an ALL-repos conjunction, which is
            # what keeps the existing retry semantics correct — a partial push leaves the
            # approval pending so the human can re-click Approve, and gitCommit re-pushes
            # already-committed repos as a no-op while retrying the ones that failed.
            multi_repo = await _story_write_repo_count(pool, story_id) > 1
            if multi_repo:
                await _discard_plan_only_sibling_writes(
                    instance_id=instance_id, story_id=story_id,
                    current_plan_path=story["current_plan_path"],
                    diff_patch_path=payload.get("diff_path") or "docs/diff.patch",
                )
            try:
                commit_result = await gateway_admin.call(
                    "shca.workspace.gitCommit",
                    {
                        "agentId": instance_id,
                        "storyId": story_id,
                        "paths": ["all"],
                        "message": f"impl: complete DAG {dag_task_id}",
                        "push": True,
                        **({"allRepos": True} if multi_repo else {}),
                    },
                    timeout=120 if not multi_repo else 300,
                )
            except GatewayError as exc:
                return {"ok": False, "error": f"commit RPC failed: {exc}"}

            push_error = commit_result.get("push_error")
            commit_sha = commit_result.get("commit_sha") or ""
            pushed = bool(commit_result.get("pushed"))

        # commit_result only exists on the gateway (non-local) path above.
        repo_results = commit_result.get("repos") or [] if not is_local else []
        if repo_results:
            failures = [
                f"{r.get('subdir')}: {r.get('error') or r.get('push_error')}"
                for r in repo_results
                if r.get("error") or r.get("push_error")
            ]
            landed = [r.get("subdir") for r in repo_results if r.get("pushed")]
            _log.info(
                "diff_review: story=%s multi-repo push — %d/%d pushed (%s)%s",
                story_id, len(landed), len(repo_results), ", ".join(landed) or "none",
                f"; failures: {'; '.join(failures)}" if failures else "",
            )
            if failures:
                # Surface WHICH repos failed. A bare "push failed" on a multi-repo
                # story is unactionable — some repos may already be pushed, and the
                # human needs to know the retry is partial, not a fresh start.
                push_error = "; ".join(failures)

        if pushed:
            # Mark the approval decided ONLY on a successful push. On push failure we
            # deliberately leave the approval PENDING + the card actionable so the human
            # can re-click Approve to RETRY the push — the commit already landed, and
            # gitCommit re-pushes the existing commit on nothing-to-commit. (Previously
            # the card was marked decided before the push result was checked, so a failed
            # push consumed the card and stranded the story in awaiting_review with no
            # retry path and no surfaced error.)
            # ATOMIC, so two approvals arriving together cannot both get here: the
            # check at the top reads `decided_at`, then awaits, and a double click
            # used to pass it twice and write the ledger twice. The loser gets the
            # same reply a later click gets.
            won = await pool.fetchval(
                """UPDATE approvals SET
                       decision = 'approved',
                       decided_at = NOW(),
                       decided_by = $1
                    WHERE id = $2 AND decided_at IS NULL
                RETURNING id""",
                decided_by_uid, approval_id,
            )
            if won is None:
                return {"ok": False, "error": "approval already approved"}
            await _mark_card_decided(approval_id, "approve")
            # Phase 4 Stage 5: DAG is done, but the STORY is not. After
            # diff approve + push, the story stays in_progress and the
            # phase transitions to 'awaiting_next' so the human can
            # either start another DAG (more features / fixes) or
            # explicitly mark the story done. Marking done is a
            # separate human action — never automatic.
            await pool.execute(
                """UPDATE tasks SET state='done', completed_at=NOW(),
                       updated_at=NOW() WHERE id = $1""",
                dag_task_id,
            )
            await pool.execute(
                """UPDATE stories SET
                       current_phase='awaiting_next',
                       updated_at=NOW()
                    WHERE id = $1""",
                story_id,
            )
            # THE PLAN IS NOT SPENT HERE, and that is a deliberate correction.
            #
            # It was, briefly: a landed diff marked the plan `executed`. That
            # conflated two different events. A diff landing means SOME AGREED
            # WORK is on the branch -- which happens mid-plan all the time when
            # a person lands a safe subset -- while a plan being spent should
            # mean the agreed intent is finished with. Spending it here blocked
            # the REST of a plan the person had already approved: the next
            # submit refused with "this is new work" for work they agreed to
            # three steps ago.
            #
            # A plan is spent where new intent appears instead: `more-work`
            # (explicitly asking for another cycle) and marking the story done.
            # See `local_plan_gate.request_more_work`.
            try:
                from app.services.go_events import recalculate_goal_completion
                await recalculate_goal_completion(goal_id)
            except Exception as _exc:
                _log.warning("recalculate_goal_completion failed after DAG done: %s", _exc)
            # NOTE (plan R10): `awaiting_next` is set on PUSH, not on merge. Coupling
            # it to "merge story done" was considered and deliberately NOT done — it
            # would change single-repo behaviour that works today, and the merge story
            # already tracks landing. For a multi-repo story this means the phase
            # advances once all repos are pushed; whether they have MERGED is the merge
            # story's business, not this gate's.
            await _emit_ledger(goal_id, "diff.approved_and_pushed", {
                "story_id": story_id, "dag_task_id": dag_task_id,
                "commit_sha": commit_sha,
                # Per-repo shas for a multi-repo push; omitted when single-repo. Exactly
                # one of `repo_results` (gateway) / `local_per_repo` (local) is ever
                # non-empty — `is_local` decides which, same as everywhere else in
                # this function.
                **({"repos": [
                    {"subdir": r.get("subdir"), "commit_sha": r.get("commit_sha")}
                    for r in (repo_results or local_per_repo)
                ]} if (repo_results or local_per_repo) else {}),
                # Recorded ONLY when the landing could not be confirmed, so the audit
                # trail distinguishes "observed on the remote" from "assumed". Omitted on
                # the normal path so existing ledger consumers see no new field.
                **({} if push_confirmed else {"push_unverified": True}),
            }, actor_id=dr_actor, actor_type=dr_type)

            # Write a task-controller message announcing the push and
            # asking the human what's next. SHCA gets this via the
            # gateway-forward path (sentinel_parser plumbing).
            await _post_awaiting_next_prompt(
                goal_id=goal_id, story_id=story_id,
                commit_sha=commit_sha, dag_task_id=dag_task_id,
            )

            return {
                "ok": True,
                "decision": "approve",
                "pushed": True,
                "commit_sha": commit_sha,
            }
        else:
            # Commit landed but push FAILED. Approval left PENDING (card still actionable
            # above) so the human can re-click Approve to retry the push. Return ok=False
            # so the route surfaces the real error instead of a misleading "approved".
            await _emit_ledger(goal_id, "diff.approved_but_push_failed", {
                "story_id": story_id, "dag_task_id": dag_task_id,
                "commit_sha": commit_sha, "push_error": push_error,
            }, actor_id=dr_actor, actor_type=dr_type)
            return {
                "ok": False,
                "error": (
                    f"Commit succeeded but the push to the feature branch failed: "
                    f"{push_error or 'unknown error'}. Click Approve again to retry the "
                    f"push (the commit is already saved)."
                ),
                "decision": "approve",
                "pushed": False,
                "commit_sha": commit_sha,
                "push_error": push_error,
            }

    if decision == "reject":
        # 1. Record the rejection on the approval row.
        won = await pool.fetchval(
            """UPDATE approvals SET
                   decision = 'rejected',
                   decided_at = NOW(),
                   decided_by = $1,
                   feedback = $2
                WHERE id = $3 AND decided_at IS NULL
            RETURNING id""",
            decided_by_uid, feedback, approval_id,
        )
        if won is None:
            return {"ok": False, "error": "approval already rejected"}
        await _mark_card_decided(approval_id, "reject")

        # 2. Close out the DAG. It's terminal in `rejected` — a new DAG
        #    will be created when SHCA's planning skill (MODE C) gets
        #    the next plan approved.
        await pool.execute(
            "UPDATE tasks SET state = 'rejected', updated_at = NOW() "
            "WHERE id = $1",
            dag_task_id,
        )
        # DAG tasks are umbrella records, not leaf executions, so the
        # handle is usually absent — but staying uniform across cancel
        # paths avoids a future bug if dag-level credentials ever land.
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(dag_task_id, "diff_review:dag_rejected")

        # 3. Send the story back to planning. The state snapshot built
        #    on SHCA's next turn will reflect phase='planning' and the
        #    unified assistant_code_planning skill will pick MODE C (NEW
        #    CYCLE) because the DAG has executed.
        await pool.execute(
            "UPDATE stories SET current_phase = 'planning', "
            "current_plan_path = NULL, "  # new cycle — keep Approve disabled until the new plan is written
            "updated_at = NOW() WHERE id = $1",
            story_id,
        )

        # 4. Post a task-controller message so SHCA sees the rejection
        #    + the human's feedback in chat. SHCA's planning skill
        #    reads this to brief Claude for the new cycle.
        try:
            from app.services.sentinel_parser import write_task_progress_card
            body = (
                f"♻ Diff review rejected. DAG `{dag_task_id}` is closed; "
                f"story is back at `planning` for a new cycle.\n\n"
                f"Human feedback:\n> {feedback}"
            )
            await write_task_progress_card(
                goal_id=goal_id,
                story_id=story_id,
                task_id=f"{dag_task_id}-reject",
                phase="planning",
                kind="output",
                text=body,
                sentinel_kind="DIFF_REJECTED",
            )
        except Exception:
            _log.exception(
                "diff_review reject: failed to post task-controller message "
                "(story=%s dag=%s)", story_id, dag_task_id,
            )

        await _emit_ledger(goal_id, "diff.rejected", {
            "story_id": story_id, "dag_task_id": dag_task_id,
            "feedback": feedback,
        }, actor_id=dr_actor, actor_type=dr_type)
        return {"ok": True, "decision": "reject"}

    return {"ok": False, "error": f"unknown decision {decision!r}"}


async def _post_awaiting_next_prompt(
    *, goal_id: str, story_id: str, commit_sha: str, dag_task_id: str,
) -> None:
    """After diff approve + push, write a task-controller message
    asking what's next. Story stays in_progress; phase=awaiting_next.

    Uses the same plumbing as sentinel_parser.write_task_progress_card
    so SHCA sees the message in real-time via the gateway-forward.
    """
    try:
        from app.services.sentinel_parser import write_task_progress_card
        body = (
            f"Diff approved and pushed (commit `{(commit_sha or '')[:12]}`). "
            f"DAG `{dag_task_id}` is complete. "
            f"Story is open for the next plan, or click ✓ Mark story done "
            f"when there's nothing more to add."
        )
        await write_task_progress_card(
            goal_id=goal_id,
            story_id=story_id,
            task_id=f"{dag_task_id}-await",
            phase="awaiting_next",
            kind="awaiting_next_prompt",
            text=body,
            sentinel_kind="AWAITING_NEXT",
        )
    except Exception:
        _log.exception(
            "diff_review: failed to post awaiting_next prompt "
            "(story=%s) — phase is set but human may not see the prompt",
            story_id,
        )


async def _mark_card_decided(approval_id: str, decision: str) -> None:
    """Stamp the chat message's card_data with `decided` so the template
    knows to hide the Approve/Reject buttons after page reload."""
    pool = get_pool()
    try:
        await pool.execute(
            """UPDATE messages
                  SET card_data = card_data || jsonb_build_object(
                          'decided', $1::text,
                          'decided_at', to_jsonb(NOW())
                      )
                WHERE card_data->>'kind' = 'diff_review'
                  AND card_data->>'approval_id' = $2""",
            decision, approval_id,
        )
    except Exception:
        _log.exception(
            "diff_review: failed to mark card decided (approval=%s) — "
            "approval row was still updated, this just keeps the chat "
            "card buttons live on next reload", approval_id,
        )


async def _emit_ledger(goal_id: str, event_type: str, payload: dict, *,
                       actor_id=SYSTEM_ACTOR_ID, actor_type="system") -> None:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    actor_id=actor_id, actor_type=actor_type,
                    event_type=event_type,
                    payload=payload,
                    context_goal_id=goal_id,
                )
    except Exception:
        _log.exception("ledger emit failed: %s", event_type)
