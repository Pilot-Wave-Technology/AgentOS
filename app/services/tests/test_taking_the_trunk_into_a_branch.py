"""A branch takes the trunk's changes, and only the worker can fetch.

THE WORKFLOW THIS SERVES. The engine cuts a feature branch from a trunk
lane and hands it to the workspace; the story works there and pushes
there; a person merges it to trunk and the branch is deleted. When the
trunk MOVES first -- somebody else lands work -- the branch has to take
those changes before it can merge, and that is an ordinary coding story
with a brief.

WHY IT NEEDED A TOOL. Two parties who look like they could do it cannot:

  the engine   its GitHub permissions are read-only by design --
               "detection never writes to GitHub (the human merges)" --
               and `merge_pr`/`merge_ref_into` are retired from the live
               path. There is no API shortcut.
  the model    `git fetch` needs a credential. The helper IS in
               `.git/config` where the sandbox reads it, but it is a
               POINTER: the bearer lives in a worker-private directory
               the sandbox has no mount for, so a `bash` fetch
               authenticates as nobody and fails on a private repo.

So it is worker-side, like `story_commit` and `publish` before it, and
for the same reason.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the lane resolves, which it did not ────────────────────────────────

def test_the_lane_is_looked_up_the_way_a_clone_stores_it():
    """A DARK FEATURE, fixed. `lanes` carries a bare branch name from
    `goal_resources.branch_name`, and a clone holds every branch it did
    not check out only as `refs/remotes/origin/<name>`. Git resolves a
    bare name against refs/heads and refs/remotes/<name> -- never
    refs/remotes/origin/<name> -- so `merge-base calculator HEAD`
    printed nothing and `op_changed`, which is written never to raise,
    returned "". The assessor fell back to a tree listing on every story
    and nothing said so."""
    body = _src("kernel/workspace/podfs.py")

    assert "def _lane_ref" in body
    assert 'f"origin/{lane}"' in body, (
        "the lane is still resolved by its bare name, which a clone "
        "does not have")
    i = body.index("def op_changed")
    assert "_lane_ref" in body[i:i + 3000], (
        "op_changed does not use the resolver, so merge-base still gets "
        "a name that cannot resolve")


def test_origin_is_preferred_over_a_local_branch_of_the_same_name():
    """After a fetch, `origin/<lane>` is the fresher of the two -- and
    being behind is the whole reason this code runs."""
    body = _src("kernel/workspace/podfs.py")
    i = body.index("for candidate in (")
    window = body[i:i + 120]

    assert window.index("origin/") < window.index(", lane)"), (
        "the bare local name is tried first, so a stale local branch "
        "wins over the ref that was just fetched")


# ── the sync itself ────────────────────────────────────────────────────

def test_it_fetches_before_it_merges():
    """The clone is as old as the pod and the trunk moving is usually
    what created the story. Merging `origin/<lane>` without fetching
    takes the trunk as it was at dispatch, leaves the branch behind, and
    looks exactly like success."""
    body = _src("kernel/workspace/podfs.py")
    i = body.index("def _sync_one")
    window = body[i:i + 2500]

    assert '"fetch"' in window, "sync merges without fetching"
    assert window.index('"fetch"') < window.index('"merge"'), (
        "the merge happens before the fetch, so it merges a stale trunk")


def test_a_fetch_that_failed_does_not_merge_anyway():
    """R1's shape: an operation that did not run has not said the branch
    is up to date."""
    body = _src("kernel/workspace/podfs.py")
    i = body.index("def _sync_one")
    window = body[i:i + 2500]
    at_fetch = window.index('"fetch"')
    at_return = window.index('"state": "error"', at_fetch)
    at_merge = window.index('"merge"')

    assert at_return < at_merge, (
        "a failed fetch falls through to the merge, which then reports "
        "success over a branch that is still behind")


def test_it_merges_and_never_rebases():
    """The branch is pushed and `git_nodes` and the PR name its commits.
    A rebase rewrites them and needs a force-push."""
    body = _src("kernel/workspace/podfs.py")
    i = body.index("def _sync_one")
    window = body[i:i + 2500]

    assert "rebase" not in window, "sync rebases the branch"
    assert '"merge", "--no-edit"' in window


def test_a_conflict_is_reported_as_work_and_not_as_a_broken_tool():
    """Resolving a conflict is editing files, which needs no credential
    -- so it is the one part of this the model CAN finish. Reporting it
    as a failed tool call sends it hunting for a broken tool instead of
    at the markers in its tree."""
    tools = _src("kernel/harness/executor_tools.py")
    i = tools.index("def _sync_result")
    window = tools[i:i + 2500]

    assert "CONFLICT" in window
    assert "story_commit" in window, (
        "a conflict does not tell the model how to finish")
    assert "exit_code=0" in window, (
        "a conflict is returned as a tool failure")


def test_the_lane_comes_from_the_record_and_not_from_the_model():
    """Which branch a story merges into is a fact about the goal. Taking
    it as a tool argument would let a story merge whatever it named.

    The query moved to `kernel/harness/sync_lanes.py` when the lane map
    was scoped per mount; the property is unchanged and the next test
    carries the scoping half of it.
    """
    lanes = _src("kernel/harness/sync_lanes.py")

    assert "goal_resources" in lanes
    assert "role = 'trunk'" in lanes

    catalog = _src("kernel/tools/catalog/workspace.py")
    j = catalog.index('"sync_trunk": _schema')
    assert "        {},\n        [],\n" in catalog[j:j + 1600], (
        "sync_trunk takes arguments, so the model chooses what to merge")


def test_the_lane_is_resolved_per_mount_and_not_per_goal():
    """Goal-wide was wrong three ways, and all three were live on next.

    A lane the story does not hold reached the pod, which cannot reach
    that directory, and the whole call failed E_WORKSPACE_UNREACHABLE --
    so one unrelated lane anywhere in the goal disabled `sync_trunk` for
    every story in it. A deleted lane still counted, because nothing
    filtered on status. And two lanes of one repository collapsed onto
    one key, so one of them merged the other's trunk in silence.

    `kernel/harness/tests/test_sync_trunk_lanes_are_per_mount.py` holds
    the behaviour; this holds the shape, beside the rest of the
    sync_trunk contract.
    """
    lanes = _src("kernel/harness/sync_lanes.py")

    assert "execution_mounts" in lanes, (
        "the lane map is built from the goal again rather than from what "
        "this execution actually mounted")
    assert "em.writable" in lanes, (
        "a read-only mount has no branch to merge into")
    assert lanes.count("COALESCE(gr.status, 'active') = 'active'") == 2, (
        "a deleted or archived lane counts again")

    tools = _src("kernel/harness/executor_tools.py")
    i = tools.index("async def remote_sync")
    assert "lanes_for_mounts" in tools[i:i + 2500]


# ── and the model is told it cannot do this itself ─────────────────────

def test_the_tool_says_why_bash_will_not_work():
    """Without this the model tries `git fetch`, gets an auth failure,
    and reads it as a broken repository rather than a missing surface."""
    body = _src("kernel/tools/catalog/workspace.py")
    i = body.index('"sync_trunk": _schema')
    window = body[i:i + 1600]

    assert "bash" in window.lower()
    assert "credential" in window


def test_the_profile_that_runs_diff_stories_has_the_tool():
    assert '"sync_trunk"' in _src("profiles/story-coding/profile.toml")


def test_the_prompt_carries_the_merge_not_rebase_rule():
    body = _src("profiles/story-coding/prompt.md")

    assert "sync_trunk" in body
    assert "never rebases" in body.lower()
    assert "conflict is not a failure" in body.lower()


# ── the clone stays deep, on purpose ───────────────────────────────────

def test_the_clone_records_why_it_is_not_shallow():
    """The next person to notice a 95 KB patch in a two-file repository
    will reach for --depth, and it would break the merge above."""
    body = _src("kernel/workspace/bindings.py")
    i = body.index('"clone", spec.source, path')
    window = body[max(0, i - 1800):i]

    assert "--depth 1" in window and "merge" in window, (
        "nothing explains why the clone is full, so the next reader "
        "makes it shallow and the diff story stops working")
    assert "blob:none" in window, (
        "the cheap lever that KEEPS the commit graph is not named, so "
        "the only remembered option is the one that breaks merge-base")


# ── an advertised check must be a satisfiable one ──────────────────────

def test_every_planning_profile_can_satisfy_an_artifact_check():
    """`register_plan` offers `{expect: "artifact"}` to every profile
    that can plan, and `artifact_kind` answers it from
    `artifact_manifest` -- which only `emit_artifact` writes.

    story-coding had the check available and not the tool, so a coding
    story that registered one could never satisfy it: the claim is
    refused at `propose_completion`, the model fixes nothing because
    nothing is broken, and it burns its legs. The schema advertises the
    option, so every profile that can plan has to be able to take it.
    """
    from kernel.profiles.loader import load_all

    for key, profile in load_all().items():
        if "register_plan" not in profile.tools:
            continue
        assert "emit_artifact" in profile.tools, (
            f"{key} can register an artifact check and cannot emit one, "
            f"so such a plan refuses its own claim for ever")
