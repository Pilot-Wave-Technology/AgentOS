"""A calculator whose frontend and backend live on different branches.

WHICH LAYER DECIDES, because there are two and only one of them is the
authority for the case you are probably here about.

`story_resources` derives the path for a TRUNK lane in SQL and ignores
whatever was stored: `CASE WHEN gr.kind = 'branch' AND gr.role =
'trunk' THEN NULL ELSE sr.at END`. Its derivation suffixes every write
lane when a window function counting write grants per (story, repo)
exceeds one -- set-aware, which `mount_for` cannot be, since it sees one
resource at a time. So a story writing two trunk lanes has always
received two directories, and the first version of this file claimed
otherwise.

`mount_for`'s `qualify` is the same rule for the grants that SQL does
NOT re-derive: `role='feature'` branches, where `sr.at` survives and
this function's answer is what lands. The tests below pin its behaviour
on that path, and pin that both layers still state the rule, because a
helper that disagreed with the authority is what produced the wrong
reading in the first place.

THE REST OF THE PLATFORM WAS ALREADY BUILT FOR THE PLURAL CASE, which is
what makes the gap worth recording rather than just closing:

    publish_execution   iterates `[m for m in mounts if m.writable]`
    story_commit        "shells out to git four times PER MOUNT"
    sync_trunk          "merges into EVERY writable repository you hold"
    plan.py             a write target is (repo, lane); "two lanes of one
                        repo are two targets exactly like two repos",
                        with the node key and branch name lane-qualified
                        whenever two targets share a repository

So the engine could already plan it, the harness could already commit it
and the delivery path could already push it. One path name stood in the
way of all of that, and it failed by losing half a workspace quietly --
the most expensive way for anything here to be wrong.

MULTI-REPO WAS NEVER AFFECTED and must stay that way: two repositories
have different sources, so `repo_path` already separates them. Every
assertion below that names two repositories is guarding that, not
testing the new flag.
"""
from __future__ import annotations

import pytest

from kernel.workspace.mounts import mount_for, repo_path

FRONTEND = "https://github.com/Pilot-Wave-Technology/multi_agent_dummy_repo.git"
OTHER = "https://github.com/Pilot-Wave-Technology/another_repo.git"
CANON = repo_path(FRONTEND)


def test_the_primary_write_lane_keeps_the_canonical_path():
    """Unchanged, and that is the point of the default.

    `repos/<owner>__<name>` is in WORKSPACE.md, in the executor prompts
    and in every story's habits. Qualifying it for everyone to serve the
    two-lane case would have been a large blast radius for a small
    feature, so `qualify` defaults to False and a single-lane story
    renders byte-identically to before.
    """
    m = mount_for("branch", FRONTEND, ref="cal_frontend", writable=True)
    assert m.at == CANON
    assert m.writable is True
    # STILL UNPINNED. The story works on the engine's feature branch for
    # this lane; a stored ref would pin the checkout to the merge target.
    assert m.ref == ""


def test_a_second_write_lane_gets_its_own_directory():
    """The fix, stated as the behaviour it buys."""
    m = mount_for("branch", FRONTEND, ref="cal_backend", writable=True,
                  qualify=True)
    assert m.at == f"{CANON}@cal_backend"
    assert m.writable is True
    # THE PATH CARRIES THE LANE AND THE REF STAYS EMPTY, deliberately:
    # the directory says which lane this checkout serves, the empty ref
    # says the engine resolves its feature branch later. A qualified
    # write lane that pinned its ref would check out the merge target.
    assert m.ref == ""


def test_the_two_lanes_do_not_collide():
    """What `qualify` buys, on the path where this function decides."""
    front = mount_for("branch", FRONTEND, ref="cal_frontend", writable=True)
    back = mount_for("branch", FRONTEND, ref="cal_backend", writable=True,
                     qualify=True)
    assert front.at != back.at, (
        "both write lanes claim one directory; mounts_for de-duplicates "
        "by path, so a story would silently lose one of them")


def test_a_read_lane_is_unchanged_and_still_pins_its_ref():
    """Read lanes were always suffixed. Nothing here touches them."""
    m = mount_for("branch", FRONTEND, ref="cal_backend", writable=False)
    assert m.at == f"{CANON}@cal_backend"
    assert m.ref == "cal_backend", (
        "a read lane pins its ref: that branch's landed state IS what "
        "was granted")


def test_two_repositories_never_needed_the_flag():
    """Multi-repo already worked, and this guards that it still does.

    Different sources give different `repo_path`s, so the collision this
    flag exists for cannot arise between repositories. A regression that
    made `qualify` necessary for multi-repo would be a silent narrowing
    of something that has always worked.
    """
    a = mount_for("branch", FRONTEND, ref="main", writable=True)
    b = mount_for("branch", OTHER, ref="main", writable=True)
    assert a.at != b.at
    assert a.at == repo_path(FRONTEND) and b.at == repo_path(OTHER)


@pytest.mark.parametrize("writable", [True, False])
def test_a_lane_with_no_ref_is_never_qualified(writable):
    """There is nothing to qualify it WITH.

    `f"{at}@"` would be a directory named after nothing, and two of them
    would collide exactly as before while looking as though they had
    been disambiguated.
    """
    m = mount_for("branch", FRONTEND, ref="", writable=writable,
                  qualify=True)
    assert m.at == CANON
    assert "@" not in m.at


def test_the_engine_and_the_mount_agree_on_when_to_qualify():
    """Two layers, one rule, and they are only correct together.

    `plan.py` lane-qualifies a node key and branch name when two write
    targets share a repository; `mount_for` lane-qualifies a directory
    for the same reason. If one qualified and the other did not, a story
    would work in a directory whose branch belonged to the other lane --
    and it would commit, and push, and be wrong in the repository rather
    than in the workspace.

    This pins the shared vocabulary rather than the behaviour: the
    engine's condition lives in a module the kernel must not import, so
    the honest thing this test can do is assert the rule is written down
    in both places.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    plan = (root / "app" / "services" / "git_graph" / "plan.py").read_text(
        encoding="utf-8")
    mounts = (root / "kernel" / "workspace" / "mounts.py").read_text(
        encoding="utf-8")
    assert "qualify" in plan, (
        "plan.py no longer speaks of qualifying a lane; if it changed "
        "how it decides, mount_for has to change with it")
    assert "qualify" in mounts
    assert "two lanes of one repo are two" in plan, (
        "the engine's statement of the rule has moved or gone; "
        "kernel/workspace/mounts.py quotes it")
