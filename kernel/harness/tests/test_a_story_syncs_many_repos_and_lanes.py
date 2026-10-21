"""Four writable mounts, two repositories, two lanes of one of them.

THE CASE THAT USED TO COLLAPSE. The lane map was keyed
`repos/<owner>__<name>`, which has no lane in it -- so a story writing
two lanes of one repository produced one key, and whichever row the
goal-wide query returned last won. One mount then merged the OTHER
lane's trunk, silently, and the second mount had no entry at all and
came back `no trunk lane for this mount`.

This runs the real thing: `lanes_for_mounts` over rows, then `op_sync`
over real git repositories on disk. Only two of the four mounts have
work on them, because that is the ordinary shape -- a story edits some
of what it is granted -- and a sync that only works when every mount
was touched is not a working sync.

Each lane lands a DIFFERENTLY NAMED file on its trunk, so a merge that
took the wrong one leaves evidence on disk and fails here rather than
as a confusing conflict in production.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from kernel.harness.sync_lanes import lanes_for_mounts
from kernel.workspace.podfs import op_sync


def _git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


def _origin(tmp_path, name, lanes):
    """A bare origin carrying one commit on each named lane."""
    work = tmp_path / "build" / name
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", lanes[0])
    _git(work, "config", "user.email", "t@t.invalid")
    _git(work, "config", "user.name", "t")
    (work / "README").write_text(f"{name}\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    for lane in lanes[1:]:
        _git(work, "checkout", "-q", "-b", lane)
    bare = tmp_path / "origins" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)],
                   check=True)
    return bare, work


def _advance(work, bare, lane, filename):
    """Land a commit on `lane` in the origin, after the clones were cut."""
    _git(work, "checkout", "-q", lane)
    (work / filename).write_text("moved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"trunk moves on {lane}")
    _git(work, "push", "-q", str(bare), lane)
    return _git(work, "rev-parse", "HEAD")


def _clone(root, at, bare, lane, branch):
    """A story's working clone: a feature branch cut from `lane`."""
    dest = root / at.replace("/", os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "-b", lane, str(bare), str(dest)],
                   check=True)
    _git(dest, "config", "user.email", "t@t.invalid")
    _git(dest, "config", "user.name", "t")
    _git(dest, "checkout", "-q", "-b", branch)
    return dest


class FakeConn:
    """Only the branch-mount query matters here; the rest return none."""

    def __init__(self, branch_mounts):
        self._branch = branch_mounts

    async def fetch(self, sql, *args):
        if "gr.kind = 'branch'" in sql and "em.at" in sql:
            return self._branch
        return []


def _mounts(*ats, writable=True):
    return [{"at": at, "binding": "git", "writable": writable,
             "kind": "repo"} for at in ats]


@pytest.fixture()
def workspace(tmp_path):
    """Two repos; `app` carries two lanes, `lib` one. Plus a read-only."""
    app_bare, app_work = _origin(tmp_path, "app", ["main", "release"])
    lib_bare, lib_work = _origin(tmp_path, "lib", ["main"])

    root = tmp_path / "workspace"
    root.mkdir()
    dirs = {
        "repos/acme__app": _clone(root, "repos/acme__app", app_bare,
                                  "main", "feat/g-s-main"),
        "repos/acme__app@release": _clone(root, "repos/acme__app@release",
                                          app_bare, "release",
                                          "feat/g-s-release"),
        "repos/acme__lib": _clone(root, "repos/acme__lib", lib_bare,
                                  "main", "feat/g-s-lib"),
        "repos/acme__docs": _clone(root, "repos/acme__docs", lib_bare,
                                   "main", "feat/g-s-docs"),
    }

    # The trunks move AFTER the clones -- which is what creates the
    # story. A different filename per lane, so a merge that took the
    # wrong trunk is visible in the tree.
    _advance(app_work, app_bare, "main", "FROM_APP_MAIN")
    _advance(app_work, app_bare, "release", "FROM_APP_RELEASE")
    _advance(lib_work, lib_bare, "main", "FROM_LIB_MAIN")

    # TWO OF THE FOUR HAVE WORK. The others are granted and untouched,
    # which is the ordinary shape of a multi-repo story.
    for at in ("repos/acme__app", "repos/acme__lib"):
        (dirs[at] / "story.txt").write_text("the story edit\n",
                                            encoding="utf-8")
        _git(dirs[at], "add", "-A")
        _git(dirs[at], "commit", "-qm", "story work")

    return {"root": root, "dirs": dirs}


def _row(at, ref, lane, repo_name="app"):
    return {"at": at, "ref": ref, "lane": lane,
            "repo_owner": "acme", "repo_name": repo_name}


@pytest.mark.asyncio
async def test_each_lane_merges_its_own_trunk(workspace):
    conn = FakeConn([
        _row("repos/acme__app", "feat/g-s-main", "main"),
        _row("repos/acme__app@release", "feat/g-s-release", "release"),
        _row("repos/acme__lib", "feat/g-s-lib", "main", "lib"),
        _row("repos/acme__docs", "feat/g-s-docs", "main", "lib"),
    ])
    lanes = await lanes_for_mounts(conn, 1)
    assert lanes == {
        "repos/acme__app": "main",
        "repos/acme__app@release": "release",
        "repos/acme__lib": "main",
        "repos/acme__docs": "main",
    }, ("two lanes of one repository collapsed onto one key -- the "
        "defect this module exists for")

    out = op_sync({"root": str(workspace["root"]),
                   "mounts": _mounts(*lanes),
                   "lanes": lanes})["synced"]
    assert all(got["state"] == "merged" for got in out.values()), out

    dirs = workspace["dirs"]
    # THE DECISIVE ASSERTION. `app@release` took release's commit and
    # never saw main's, and `app` the reverse. Under the old key one of
    # these two directories got the other's trunk.
    assert (dirs["repos/acme__app"] / "FROM_APP_MAIN").exists()
    assert not (dirs["repos/acme__app"] / "FROM_APP_RELEASE").exists()
    assert (dirs["repos/acme__app@release"] / "FROM_APP_RELEASE").exists()
    assert not (dirs["repos/acme__app@release"] / "FROM_APP_MAIN").exists()
    assert (dirs["repos/acme__lib"] / "FROM_LIB_MAIN").exists()


@pytest.mark.asyncio
async def test_an_untouched_mount_syncs_as_readily_as_an_edited_one(
        workspace):
    """M of N. A mount with no story work on it is still behind its
    trunk, and leaving it stale is how a later merge finds surprises."""
    lanes = {"repos/acme__app": "main",
             "repos/acme__app@release": "release"}
    out = op_sync({"root": str(workspace["root"]),
                   "mounts": _mounts(*lanes),
                   "lanes": lanes})["synced"]

    # `app` carries a story commit, `app@release` carries none.
    assert out["repos/acme__app"]["state"] == "merged"
    assert out["repos/acme__app@release"]["state"] == "merged"
    assert (workspace["dirs"]["repos/acme__app@release"]
            / "FROM_APP_RELEASE").exists()


@pytest.mark.asyncio
async def test_a_read_only_mount_is_never_merged_into(workspace):
    """It holds no branch of this story and nothing may be written to it."""
    out = op_sync({
        "root": str(workspace["root"]),
        "mounts": (_mounts("repos/acme__app")
                   + _mounts("repos/acme__docs", writable=False)),
        "lanes": {"repos/acme__app": "main", "repos/acme__docs": "main"},
    })["synced"]

    assert set(out) == {"repos/acme__app"}
    assert not (workspace["dirs"]["repos/acme__docs"]
                / "FROM_LIB_MAIN").exists()


@pytest.mark.asyncio
async def test_one_unresolvable_mount_does_not_sink_the_others(workspace):
    """Partial failure is reported, not escalated.

    A goal where one grant cannot name its lane should still sync every
    mount that can. `_sync_result` only fails the whole call when
    NOTHING succeeded; this is the half of that contract living here.
    """
    out = op_sync({
        "root": str(workspace["root"]),
        "mounts": _mounts("repos/acme__app", "repos/acme__lib"),
        "lanes": {"repos/acme__app": "main"},
    })["synced"]

    assert out["repos/acme__app"]["state"] == "merged"
    assert out["repos/acme__lib"]["state"] == "error"
    assert "no trunk lane" in out["repos/acme__lib"]["detail"]
