"""`sync_trunk` merges from the lane of EACH MOUNT, not of the goal.

WHAT WENT WRONG, ON NEXT. The lane map was a goal-wide query -- every
`kind='branch' role='trunk'` row in the goal -- keyed by
`repos/<owner>__<name>`. Each of the three defects below was live:

  * a lane the story does not hold reached the pod, which cannot reach
    that directory, and the whole call failed E_WORKSPACE_UNREACHABLE.
    Goal GOAL1 had three lanes and its pod had one mount, so no story
    in that goal could sync at all;
  * a DELETED lane still counted (no `status` filter);
  * two lanes of one repository collapsed onto one key, so one of them
    silently merged the other's trunk.

Each is a test here. They run against rows, not a pod, because the
rows are where the defect was.
"""

from __future__ import annotations

import pytest

from kernel.harness.sync_lanes import lanes_for_mounts


class FakeConn:
    """Answers the three queries by what they select, in any order."""

    def __init__(self, branch_mounts=(), bare_mounts=(), defaults=()):
        self._branch = [dict(r) for r in branch_mounts]
        self._bare = [dict(r) for r in bare_mounts]
        self._defaults = [dict(r) for r in defaults]

    async def fetch(self, sql, *args):
        if "gr.kind = 'branch'" in sql and "em.at" in sql:
            return self._branch
        if "gr.kind <> 'branch'" in sql:
            return self._bare
        return self._defaults


def _mount(at, lane, ref, owner="Pilot-Wave-Technology", name="agentos"):
    return {"at": at, "ref": ref, "repo_owner": owner, "repo_name": name,
            "lane": lane}


@pytest.mark.asyncio
async def test_only_the_lanes_this_execution_mounts_are_synced():
    """GOAL1: three lanes in the goal, one mount in the pod."""
    conn = FakeConn(branch_mounts=[
        _mount("repos/Pilot-Wave-Technology__agentos",
               "develop", "feat/GOAL1-3b8bebbe"),
    ])
    assert await lanes_for_mounts(conn, 408) == {
        "repos/Pilot-Wave-Technology__agentos": "develop",
    }


@pytest.mark.asyncio
async def test_two_lanes_of_one_repository_keep_their_own_trunks():
    """The key is the mount point, which carries the `@<lane>` suffix."""
    conn = FakeConn(branch_mounts=[
        _mount("repos/acme__app", "main", "feat/g-s-main"),
        _mount("repos/acme__app@release", "release", "feat/g-s-release"),
    ])
    assert await lanes_for_mounts(conn, 1) == {
        "repos/acme__app": "main",
        "repos/acme__app@release": "release",
    }


@pytest.mark.asyncio
async def test_a_mount_checked_out_on_its_own_lane_still_gets_an_entry():
    """Leaving it out would invent a failure.

    `op_sync` reports a writable mount with no lane as an error, so a
    deliberate omission here reads downstream as a broken tool. Merging
    `origin/main` into `main` is a fast-forward and is what that story
    wants; the earlier draft of this fix skipped it and turned a
    working case into `no trunk lane for this mount`.
    """
    conn = FakeConn(branch_mounts=[
        _mount("repos/acme__app", "main", "main"),
    ])
    assert await lanes_for_mounts(conn, 1) == {"repos/acme__app": "main"}


@pytest.mark.asyncio
async def test_a_lane_less_grant_takes_the_repositorys_only_trunk():
    conn = FakeConn(
        bare_mounts=[{"at": "repos/acme__app", "ref": "feat/x",
                      "repo_owner": "acme", "repo_name": "app"}],
        defaults=[{"repo_owner": "acme", "repo_name": "app",
                   "branch_name": "main"}])
    assert await lanes_for_mounts(conn, 1) == {"repos/acme__app": "main"}


@pytest.mark.asyncio
async def test_a_lane_less_grant_on_a_multi_lane_repo_is_left_alone():
    """Guessing would merge the story into a trunk nobody chose."""
    conn = FakeConn(
        bare_mounts=[{"at": "repos/acme__app", "ref": "feat/x",
                      "repo_owner": "acme", "repo_name": "app"}],
        defaults=[{"repo_owner": "acme", "repo_name": "app",
                   "branch_name": "main"},
                  {"repo_owner": "acme", "repo_name": "app",
                   "branch_name": "release"}])
    assert await lanes_for_mounts(conn, 1) == {}


@pytest.mark.asyncio
async def test_the_queries_filter_out_deleted_lanes():
    """Afsheen's deleted `main` kept breaking syncs after it was gone."""
    import inspect
    src = inspect.getsource(lanes_for_mounts)
    assert src.count("COALESCE(gr.status, 'active') = 'active'") == 2, (
        "both lane queries must exclude archived and deleted resources")
    assert "em.writable" in src, (
        "a read-only mount has nothing to merge into")
