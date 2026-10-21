"""Local Execution Mode — diff-submit + landing token (M5, §14.2 / §8.2).

At DAG completion a local story is left in `awaiting_approval` (see
`sentinel_parser._on_dag_tasks_complete`). The extension commits + pushes the
feature branch with a short-lived, write-scoped token minted here, then submits
the captured diff, which reuses the shared `post_diff_review` (approval row +
card + ledger). A human then approves via the unchanged browser/Slack path;
`decide_diff_review` runs the local branch (no gateway push — already pushed).
"""
from __future__ import annotations

import logging

from app.database import get_pool
from app.services.diff_review import post_diff_review
# _repo_ref (single-repo shim), _resolve_live_repos (plural), _multi_repo_incomplete —
# see local_hydrate.py's own doc comments on why each exists.
from app.services.local_hydrate import _repo_ref, _resolve_live_repos, _multi_repo_incomplete
from app.github_tokens import generate_installation_token, verify_commit_pushed

_log = logging.getLogger(__name__)


# An errand's deliverable is an ANSWER, not a branch, and this is the gate
# that makes that true rather than merely stated.
#
# `profiles-local/errand-ops` gives an errand a `git` workspace driver -- an
# ops errand is almost always ABOUT a repository, so it needs a tree to read
# and run things in. What it does not need is a way to land commits on one.
# Before this, nothing in the local lane looked at `work_kind` at all: an
# errand could mint a write-scoped installation token and push the branch,
# and the only thing standing between it and a landed commit was the prompt
# asking it not to.
#
# REFUSED AT THE TOKEN, not only at submit, because the token IS the
# capability. A refusal at diff-submit would still have handed out a GitHub
# credential with `contents: write` on the repository.
async def _is_errand(pool, story_id: str) -> bool:
    return (await pool.fetchval(
        "SELECT COALESCE(work_kind, 'story') FROM stories WHERE id = $1",
        story_id)) == "errand"


_ERRAND_REFUSAL = {
    "ok": False,
    "reason": "errand_no_push",
    "error": ("this is an errand: it produces an answer, not a branch. Nothing "
              "here pushes or opens a pull request -- record what you found and "
              "mark the errand done."),
}


async def mint_push_token(story_id: str, *, multi_repo: bool = False) -> dict:
    """Short-lived, repo-scoped, WRITE token so the local clone can push the feature
    branch. Landing authority still stays CP-side — the git engine opens the PR and
    a human merges it (§8.3); this only pushes the branch tip.

    ONE token covers every repo in `repos[]` — same one-token-many-repos convention
    as `local_hydrate.mint_repo_clone_token` (C2) and the cloud/operator lane's
    `token_repo_names_for`. `repos` is present only when there is more than one repo;
    a single-repo response is byte-for-byte unchanged from before this capability
    existed.
    """
    pool = get_pool()
    if await _is_errand(pool, story_id):
        return dict(_ERRAND_REFUSAL)
    if not multi_repo:
        repo = await _repo_ref(pool, story_id)
        if not repo:
            return {"ok": False, "reason": "no_repo"}
        tok = await generate_installation_token(
            [repo["repo"]], permissions={"contents": "write", "metadata": "read"},
        )
        return {
            "ok": True, "repo": repo, "token": tok["token"], "expires_at": tok.get("expires_at"),
        }

    repos = await _resolve_live_repos(pool, story_id)
    if not repos or await _multi_repo_incomplete(pool, story_id, len(repos)):
        return {"ok": False, "reason": "no_repo"}
    # One name per repository -- see `mint_repo_clone_token`. Two lanes of
    # one repo are two clones, and the push token must not ask GitHub to
    # scope itself to the same repository twice.
    tok = await generate_installation_token(
        list(dict.fromkeys(r["repo"] for r in repos)),
        permissions={"contents": "write", "metadata": "read"},
    )
    result = {
        "ok": True, "repo": repos[0], "token": tok["token"], "expires_at": tok.get("expires_at"),
    }
    if len(repos) > 1:
        result["repos"] = repos
    return result


async def _hand_it_back(pool, story_id: str, refusal: dict) -> dict:
    """A refused submit returns the story to the agent. Returns the refusal.

    THE DEADLOCK THIS ENDS, which `release_claim` was written for and
    nothing ever called. `propose_completion` moves the story to
    `submitting`; the boundary hook refuses source edits in that phase,
    because a change after the work is submitted is one the person
    merging never saw. Both correct -- and together, with a refusal in
    between, a trap: the checks fail, the story stays `submitting`, the
    agent CANNOT edit the file that would fix them, and the extension's
    poll re-submits every sixty seconds for ever. Seen on 9519fc57.

    So a refusal withdraws the claim. The story goes back to
    `executing` under the SAME approved plan -- no new approval, which
    is the rule already agreed for the integration cycle -- and the
    agent is told what failed.
    """
    from app.services.local_claim import release_claim  # noqa: PLC0415

    try:
        await release_claim(story_id, refusal.get("error") or
                            refusal.get("reason") or "the submit was refused",
                            pool=pool)
    except Exception:                                       # noqa: BLE001
        _log.exception("could not release the completion claim on %s", story_id)
    return refusal


async def _park_for_review(pool, story_id: str, result: dict) -> dict:
    """The submit succeeded. Land the story in `awaiting_merge` and settle
    the diff, because in this lane SUBMITTING IS APPROVING.

    THERE IS NO DIFF REVIEW HERE ANY MORE (2026-10-14). The cloud lane
    never had one -- its executor claims completion, a PR opens, and a
    human merges on GitHub. Local grew a second gate that cloud does not
    have, so the same work was approved twice: once as a diff and again
    as a merge. The plan is the gate before code is written; the pull
    request is the gate before it lands.

    THE APPROVAL ROW STAYS, decided rather than deleted. It is also the
    RECORD of what was submitted -- the diff the panel shows and the
    ledger points at -- and dropping it would take the diff away along
    with the gate. Decided by the submitter, at the moment of submitting,
    which is exactly what "submitting is approving" means.

    `awaiting_merge` IS THE CLOUD'S OWN STATE, not a local invention: the
    story has work on a branch that is not on trunk yet. Everything that
    already understands it -- `reclaim` leaving the checkout alone,
    dependents staying blocked, Mark Done refusing -- understands this
    story too.

    Only on success. A refused submission -- an errand, an unverified
    push, a spent plan -- must leave the story where it was.
    """
    if not result.get("ok"):
        return result
    # THE STREAK IS "IN A ROW", so a submit that lands ends it. Without
    # this, one flake early in a story would leave the counter at 1 and
    # the next unrelated refusal, weeks later, would park on its first
    # occurrence.
    try:
        from app.services import local_push_park  # noqa: PLC0415
        await local_push_park.clear(pool, story_id)
    except Exception:                                       # noqa: BLE001
        _log.exception("could not clear the push-refusal streak for %s", story_id)
    # THREE `pool.execute` CALLS, NOT A TRANSACTION, and that is deliberate.
    # Every other writer in this module uses the pool directly; wrapping
    # these in `pool.acquire()` broke six tests whose fake pool offers an
    # `async def acquire()` rather than an async context manager. The three
    # writes are also independently safe to repeat -- each is conditional
    # or idempotent -- so a partial failure is recovered by the next submit
    # rather than leaving a half-state.
    # AND AN ERRAND NEVER REACHES THIS STATE, re-asserted here rather than
    # relied on from `submit_local_diff` a hundred lines up. `awaiting_merge`
    # means "waiting for a merge", and an errand's work does not land: its
    # branch is scaffolding and `deliver.py` refuses to publish it. A story
    # put there by mistake waits for something that cannot happen, and
    # nothing looks wrong until the deadline takes it.
    #
    # This is a chokepoint both submit paths go through, so the guard belongs
    # at it -- a future third caller inherits the protection instead of
    # needing to remember it.
    if await _is_errand(pool, story_id):
        _log.warning("story %s is an errand; not moving it to awaiting_merge",
                     story_id)
        return result

    # THE STORY STATE GOES FIRST, and the order is the point. The work is
    # already pushed by the time this runs, so the one thing that must not
    # fail is the story saying so -- a push nobody can see is the stall this
    # whole change exists to remove. The approval settle below is
    # bookkeeping, and bookkeeping must not be able to strand a story.
    await pool.execute(
        "UPDATE stories SET state = 'awaiting_merge', "
        "       current_phase = 'awaiting_merge', updated_at = NOW() "
        " WHERE id = $1", story_id)
    # The cycle is spent BY THE SUBMIT, which is what makes "more work from
    # here needs a new plan" true without a second gate to hang it on.
    from app.services.local_cycles import spend_cycle  # noqa: PLC0415
    await spend_cycle(pool, story_id, "submit")
    # THE HOLD HAS LIFTED. An instruction given while the platform was
    # taking the trunk in was queued rather than refused; this is the
    # moment it becomes answerable, and it is replayed now so it takes
    # the mode that is right NOW -- the cycle is spent, so it starts a
    # new plan, which is what the person asked for. Best-effort: losing
    # the release must not fail a submit that succeeded.
    try:
        from app.services.local_more_work import drain_queued  # noqa: PLC0415
        goal_id = await pool.fetchval(
            "SELECT goal_id FROM stories WHERE id = $1", story_id)
        if goal_id:
            await drain_queued(pool, goal_id, story_id)
    except Exception:                                       # noqa: BLE001
        _log.exception("could not release held instructions on %s", story_id)

    approval_id = result.get("approval_id")
    if approval_id:
        try:
            await pool.execute(
                "UPDATE approvals SET decided_at = NOW(), decision = 'approved', "
                "       decided_by = 'local:submit' "
                " WHERE id = $1 AND decided_at IS NULL", approval_id)
        except Exception:                                   # noqa: BLE001
            _log.exception(
                "story %s submitted and is awaiting_merge, but its diff "
                "approval %s could not be settled; the gate is gone either "
                "way and the row is the record, not the decision",
                story_id, approval_id)
    _log.info("local diff submitted story=%s -> awaiting_merge", story_id)
    return result


async def submit_local_diff(
    story_id: str, diff_text: str, stat: str, truncated: bool, commit_sha: str,
    *, repos: list[dict] | None = None,
) -> dict:
    """Record the pushed diff as a diff-review approval + card for human sign-off.

    `repos`, when given, is the extension's per-repo breakdown from
    `captureAndCommitAll`/`pushAll` (D-5: one call, one entry per repo — `{name, diff,
    stat, truncated, commit_sha}` — so ONE approval covers the whole story, mirroring
    how the gateway's `allRepos` path already gets one approval for several repos).
    The scalar `diff_text`/`stat`/`truncated`/`commit_sha` are then IGNORED — kept as
    the only parameters for the single-repo caller, which predates this and must see
    no behaviour change.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT goal_id, active_thread_id FROM stories WHERE id = $1", story_id,
    )
    if not story:
        return {"ok": False, "reason": "story_not_found"}
    # Second gate, and not redundant: the token is refused above, but a person
    # with their own git credentials can push by hand. Accepting the diff would
    # then open a review card proposing to land an errand's commits.
    if await _is_errand(pool, story_id):
        return dict(_ERRAND_REFUSAL)
    # THE DAG PRECONDITION IS GONE. This required a `claude-code-dag` parent
    # task sitting at `awaiting_approval` before a diff could be submitted --
    # the graph's way of saying "the work is finished". A local run dispatches
    # no graph, so there is nothing to be awaiting: the person decides they
    # have something worth reviewing and submits it.
    #
    # AN APPROVED PLAN IS STILL REQUIRED, and it is the same requirement
    # `story_done_blocker` keeps: a diff arriving with nothing on the board
    # saying what was meant to happen is a diff nobody can review against
    # anything.
    # `approved`, NOT `approved OR executed`. An `executed` plan is one whose
    # cycle already landed (`diff_review` spends it when the diff is approved),
    # and accepting it here meant a second cycle could be submitted against
    # work nobody had agreed to. More work needs a new plan -- enforced, not
    # asked for.
    has_plan = await pool.fetchval(
        "SELECT 1 FROM plan_versions WHERE story_id = $1 "
        "AND status = 'approved' LIMIT 1",
        story_id,
    )
    if not has_plan:
        spent = await pool.fetchval(
            "SELECT 1 FROM plan_versions WHERE story_id = $1 "
            "AND status = 'executed' LIMIT 1", story_id)
        return {
            "ok": False, "reason": "no_approved_plan",
            "error": ("the last plan's cycle already landed, so this is new work: "
                      "register a new plan and have it approved before submitting "
                      "another diff")
            if spent else
            ("no approved plan for this story: register one with `register_plan` "
             "and have it approved first"),
        }

    # AND THE CHECKS HAVE TO HAVE PASSED AT THIS COMMIT.
    #
    # THE PLATFORM RUNS THEM, which is the cloud's rule and the reason for
    # it holds here unchanged: an agent that both does the work and
    # reports whether the work is good is marking its own homework, and
    # the failure is not dishonesty but optimism -- a suite it believes it
    # ran. The extension runs them and posts the results; this reads them.
    #
    # AT THIS COMMIT, not ever. A pass recorded before the last three
    # commits says nothing about what is being pushed, which is why the
    # results carry the sha and the gate names it.
    #
    # Only when there IS a sha to ask about. A multi-repo submit carries
    # its shas per repo (`repos`), and the per-repo gate belongs with the
    # per-repo push rather than here -- asking this one about the scalar
    # would refuse every multi-repo submit on an empty string.
    if commit_sha and not repos:
        from app.services import local_checks  # noqa: PLC0415
        refusal = await local_checks.refusal(pool, story_id, commit_sha)
        if refusal:
            return await _hand_it_back(pool, story_id, refusal)

        # AND A LATER CYCLE HAS TO CONTAIN THE TRUNK IT IS BEHIND.
        #
        # The catch-up sweep only offers a plan to a story at
        # `awaiting_merge`, and asking for more work moves the story off
        # it -- so a second cycle on a stale branch gets no offer and
        # nothing else would make it take trunk in. It does it as part
        # of its own submit (decision 6), and this is where that is
        # true rather than merely asked for.
        from app.services import local_trunk_gate  # noqa: PLC0415
        refusal = await local_trunk_gate.refusal(pool, story_id, commit_sha)
        if refusal:
            return await _hand_it_back(pool, story_id, refusal)

    # CARRIED EMPTY, NOT REMOVED. `dag_task_id` rides into the approval
    # payload and the review card as linkage, and the OPERATOR lane still
    # supplies a real one -- so the parameter stays and the local lane says
    # honestly that it has none. Dropping it from the signature would change
    # a shared function for one of its two callers.
    dag_task_id = ""
    # ONE SUBMIT AT A TIME PER STORY. The dedupe below reads, then the post awaits a
    # GitHub round trip before inserting -- two submits arriving together (the button
    # and the auto-submit poll) both read "nothing pending" and both posted a review.
    # A session-level advisory lock held for the check AND the post serialises them;
    # the second then finds the first's approval and reuses it.
    async with pool.acquire() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(hashtext($1))", f"local-diff:{story_id}")
        try:
            # Idempotent: a retried submit (network flake) must not double-post the
            # approval + card. If an undecided diff approval exists, reuse it.
            existing = await pool.fetchval(
                "SELECT id FROM approvals WHERE story_id = $1 AND kind = 'diff' "
                "AND decided_at IS NULL ORDER BY proposed_at DESC LIMIT 1",
                story_id,
            )
            if existing:
                return {"ok": True, "approval_id": str(existing), "deduped": True}
            if repos:
                return await _post_multi_repo_review(pool, story, story_id, dag_task_id, repos)
            return await _post_single_repo_review(
                pool, story, story_id, dag_task_id, diff_text, stat, truncated, commit_sha,
            )
        finally:
            await lock_conn.execute(
                "SELECT pg_advisory_unlock(hashtext($1))", f"local-diff:{story_id}")


async def _post_single_repo_review(
    pool, story, story_id: str, dag_task_id: str,
    diff_text: str, stat: str, truncated: bool, commit_sha: str,
) -> dict:
    """The path every caller used before `repos[]` existed — unchanged. See
    `submit_local_diff`'s doc comment for the VERIFY-not-trust rationale, which
    applies identically per-repo in `_post_multi_repo_review` below."""
    push_verified: bool | None = None
    if commit_sha:
        repo = await _repo_ref(pool, story_id)
        if repo:
            status, detail = await verify_commit_pushed(
                f"{repo['owner']}/{repo['repo']}", repo["branch"], commit_sha,
            )
            if status == "missing":
                _log.error(
                    "submit_local_diff: story=%s claims commit %s on %s but it is not "
                    "on the remote (%s) — refusing the submission",
                    story_id, commit_sha[:12], repo["branch"], detail,
                )
                # COUNTED, AND THE SECOND ALIKE PARKS IT. The extension
                # re-submits on a timer: right for a flake, wrong for a
                # protected branch or a revoked installation, which refuse
                # identically for ever while the loop makes one identical
                # card a minute.
                from app.services import local_push_park  # noqa: PLC0415
                return await local_push_park.park_if_stuck(pool, story_id, {
                    "ok": False, "reason": "push_not_on_remote",
                    "error": (
                        f"The commit {commit_sha[:12]} isn't on "
                        f"{repo['owner']}/{repo['repo']}@{repo['branch']} — the push "
                        f"didn't land ({detail}). Re-run Submit Diff; if it keeps "
                        f"failing, check that the branch isn't protected."
                    ),
                })
            push_verified = status == "verified"
            _log.info(
                "submit_local_diff: story=%s push %s (%s) for %s@%s",
                story_id, status, detail, commit_sha[:12], repo["branch"],
            )

    return await _park_for_review(pool, story_id, await post_diff_review(
        goal_id=story["goal_id"], story_id=story_id, dag_task_id=dag_task_id,
        thread_id=story["active_thread_id"] or "", diff_text=diff_text, stat=stat,
        truncated=truncated, diff_ref=diff_text, commit_sha=commit_sha,
        push_verified=push_verified,
    ))


async def _post_multi_repo_review(
    pool, story, story_id: str, dag_task_id: str, repos: list[dict],
) -> dict:
    """The N-repo counterpart of `_post_single_repo_review`: verify EVERY repo's push
    before accepting ANY of them. A story with even one repo's commit missing on the
    remote must not be approvable — that is the exact defect the single-repo VERIFY
    step already exists to prevent, and it applies with the same force when it is one
    repo out of several rather than the only one.

    Combines each repo's diff/stat into ONE preview, matching the gateway multi-repo
    path's own approach of capturing a single combined diff plus a `per_repo`
    breakdown — not a second combination scheme. Each already-capped diff
    (`landing.ts`'s `MAX_DIFF`) is simply concatenated; `post_diff_review` truncates
    the stored preview to 2000 chars regardless, so no additional cap is needed here.
    """
    # KEYED BY (name, LANE). A repo name is not a clone: a story writing two
    # trunk lanes of one repository has two clones sharing a name, so this map
    # kept only the last one and EVERY entry then resolved to it. The push
    # verification below asked GitHub whether one lane's commit was on the
    # OTHER lane's branch, got "diverged", and refused a submission whose
    # pushes had both landed perfectly (story 7f995412: c8b781d, the backend's
    # HEAD, checked against feat/...-calculator_frontend).
    #
    # `types.ts` states the rule for the client side -- "every map keyed by a
    # clone must use repoKey(name, trunk)" -- and the extension already obeys
    # it. This did not.
    live = await _resolve_live_repos(pool, story_id)
    live_by_key = {
        (f"{r['owner']}/{r['repo']}".lower(), (r.get("trunk") or "").lower()): r
        for r in live
    }
    by_name: dict[str, list] = {}
    for r in live:
        by_name.setdefault(f"{r['owner']}/{r['repo']}".lower(), []).append(r)
    # THE SUBMISSION MUST BE THE STORY, NOT PART OF IT. Each of these was accepted
    # and only flagged "unverified": an entry with no commit, an entry naming a repo
    # this story has no lane in, and a lane nobody sent at all. Any of them lets a
    # person approve a story one of whose lanes was never checked -- or never pushed.
    def _resolve(entry):
        key = ((entry.get("name") or "").lower(), (entry.get("trunk") or "").lower())
        ref = live_by_key.get(key)
        if ref is None:
            same_name = by_name.get(key[0]) or []
            ref = same_name[0] if len(same_name) == 1 else None
        return ref
    if live:
        covered: set[int] = set()
        for entry in repos:
            name = (entry.get("name") or "").lower() or "(unnamed)"
            if not (entry.get("commit_sha") or ""):
                return {"ok": False, "reason": "lane_without_commit",
                        "error": f"{name}: no commit was sent for this lane -- re-run Submit Diff"}
            ref = _resolve(entry)
            if ref is None:
                return {"ok": False, "reason": "unknown_lane",
                        "error": f"{name}: this story has no lane for that repo/trunk"}
            covered.add(id(ref))
        missing = [f"{r['owner']}/{r['repo']}" + (f"@{r['trunk']}" if r.get("trunk") else "")
                   for r in live if id(r) not in covered]
        if missing:
            return {"ok": False, "reason": "lane_missing",
                    "error": "this submission leaves out " + ", ".join(missing)
                             + " -- every lane is submitted together; re-run Submit Diff"}

    per_repo: list[dict] = []
    diff_parts: list[str] = []
    stat_parts: list[str] = []
    any_truncated = False
    for entry in repos:
        name = (entry.get("name") or "").lower()
        entry_diff = entry.get("diff") or ""
        entry_stat = entry.get("stat") or ""
        entry_truncated = bool(entry.get("truncated"))
        entry_commit_sha = entry.get("commit_sha") or ""
        any_truncated = any_truncated or entry_truncated

        push_verified: bool | None = None
        # The lane disambiguates. A client that sends none is only safe to
        # resolve when the name is unambiguous -- with two lanes of one repo
        # there is no honest answer, and guessing is what produced the wrong
        # verification in the first place.
        entry_trunk = (entry.get("trunk") or "").lower()
        ref = live_by_key.get((name, entry_trunk))
        if ref is None:
            same_name = by_name.get(name) or []
            ref = same_name[0] if len(same_name) == 1 else None
        if entry_commit_sha and ref:
            status, detail = await verify_commit_pushed(
                f"{ref['owner']}/{ref['repo']}", ref["branch"], entry_commit_sha,
            )
            if status == "missing":
                _log.error(
                    "submit_local_diff: story=%s repo=%s claims commit %s on %s but it "
                    "is not on the remote (%s) — refusing the WHOLE submission",
                    story_id, name, entry_commit_sha[:12], ref["branch"], detail,
                )
                return {
                    "ok": False, "reason": "push_not_on_remote",
                    "error": (
                        f"{name}: the commit {entry_commit_sha[:12]} isn't on "
                        f"{ref['branch']} — the push didn't land ({detail}). Local "
                        f"commits are retained, so re-run Submit Diff; only the "
                        f"repo(s) that failed need to re-push."
                    ),
                }
            push_verified = status == "verified"
        elif entry_commit_sha and not ref:
            _log.warning(
                "submit_local_diff: story=%s repo=%s has a commit to verify but no "
                "matching live node — cannot verify its push", story_id, name,
            )

        per_repo.append({
            "subdir": entry.get("subdir") or "", "name": name, "stat": entry_stat,
            "truncated": entry_truncated, "commit_sha": entry_commit_sha,
            "push_verified": push_verified,
        })
        if entry_diff:
            diff_parts.append(f"# {name}\n{entry_diff}")
        if entry_stat:
            stat_parts.append(f"{name}: {entry_stat}")

    _log.info(
        "submit_local_diff: story=%s multi-repo submission — %d repo(s): %s",
        story_id, len(per_repo),
        "; ".join(f"{r['name']}={r['stat'] or 'no changes'}" for r in per_repo),
    )
    combined_diff = "\n\n".join(diff_parts)
    return await _park_for_review(pool, story_id, await post_diff_review(
        goal_id=story["goal_id"], story_id=story_id, dag_task_id=dag_task_id,
        thread_id=story["active_thread_id"] or "",
        diff_text=combined_diff, stat="; ".join(stat_parts), truncated=any_truncated,
        diff_ref=combined_diff, per_repo=per_repo,
    ))
