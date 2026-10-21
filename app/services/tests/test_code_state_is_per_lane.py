"""`code_state` reports per TRUNK LANE, and `stale` means stale.

WHAT IT DID. One row per repository, its trunk read from
`git_repos.trunk_branch` and its HEAD from the one `git_repo_trunks` row
that matched it. Then every branch of that repository was compared
against that single sha:

    stale = t_sha and last_synced_trunk_sha and t_sha != last_synced…

On a goal with two lanes of one repo — a backend branch and a frontend
branch of one app, which `resolve_ws_trunk` has supported all along —
that is two wrong answers at once. A branch perfectly in sync with the
lane it lands on reads STALE because the OTHER lane moved; and a branch
genuinely behind its own lane reads fresh when the reported lane happens
to sit at the sha it synced to. The planner then resequences work that
did not need it and leaves work that did.

AND `git_repos.trunk_branch` IS NOT AUTHORITATIVE. Its own migration
says so: "git_repos.trunk_branch / trunk_sha are KEPT (additive rule) as
a legacy default + coarse cache; the node's trunk_branch +
git_repo_trunks are authoritative going forward." The tool read the
legacy column and called it the repo's trunk.

WHERE LANES COME FROM NOW — `kernel/go/lanes.py`: the lanes nodes are
landing on, plus the lanes the goal has DECLARED as resources and has
not cut a branch on yet, and the repo's own default ONLY for a repo that
has neither. Adding the default unconditionally is how a card comes to
describe a third branch nobody works on, which is the bug one layer up
that `test_repo_summary_uses_the_trunk` exists for.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.go import lanes as lanes_mod
from kernel.go.code_state import code_state


class _Conn:
    """Answers each query by the table it reads. Rows in, dicts out."""

    def __init__(self, *, repos=(), lane_rows=(), nodes=(), trunk_rows=()):
        self.repos, self.lane_rows = list(repos), list(lane_rows)
        self.nodes, self.trunk_rows = list(nodes), list(trunk_rows)
        self.sql: list = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "FROM git_repos gr" in sql:
            return self.repos
        if "WITH repo AS" in sql:
            return self.lane_rows
        if "FROM git_repo_trunks WHERE" in sql:
            return self.trunk_rows
        if "FROM git_nodes gn" in sql:
            return self.nodes
        if "git_node_stories" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql[:80]}")


def _repo(rid="gr_1", trunk="main"):
    return {"git_repo_id": rid, "owner": "acme", "name": "cal",
            "trunk_branch": trunk}


def _lane(rid="gr_1", trunk="cal_backend", sha="aaa"):
    return {"git_repo_id": rid, "trunk_branch": trunk,
            "trunk_sha": sha, "trunk_sha_seen_at": None}


def _node(nid, branch, lands_on, synced, rid="gr_1"):
    return {"id": nid, "git_repo_id": rid, "branch_name": branch,
            "state": "OPEN", "trunk_branch": lands_on, "mergeable": True,
            "conflict_paths": None, "pr_number": None,
            "last_synced_trunk_sha": synced, "last_error": None,
            "updated_at": None}


# ── the shape ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_repo_reports_every_lane_it_carries():
    out = await code_state(_Conn(
        repos=[_repo()],
        lane_rows=[_lane(trunk="cal_backend", sha="aaa"),
                   _lane(trunk="cal_frontend", sha="bbb")]), "G")
    [repo] = out["repos"]
    assert [l["trunk"] for l in repo["lanes"]] == ["cal_backend",
                                                   "cal_frontend"]
    assert [l["trunk_sha"] for l in repo["lanes"]] == ["aaa", "bbb"]


@pytest.mark.asyncio
async def test_a_branch_says_which_lane_it_lands_on():
    """Without it a two-lane repo reads as one undifferentiated pile of
    branches, and nothing in the output says which merge each is for."""
    out = await code_state(_Conn(
        repos=[_repo()], lane_rows=[_lane()],
        nodes=[_node("n1", "feat/a", "cal_backend", "aaa")]), "G")
    assert out["nodes"][0]["lands_on"] == "cal_backend"


# ── and what it is actually for ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_branch_in_sync_with_its_own_lane_is_not_stale():
    """THE BUG, in one assertion. `feat/a` is exactly at its lane's head;
    the OTHER lane has moved. It used to read stale."""
    out = await code_state(_Conn(
        repos=[_repo()],
        lane_rows=[_lane(trunk="cal_backend", sha="aaa"),
                   _lane(trunk="cal_frontend", sha="zzz")],
        nodes=[_node("n1", "feat/a", "cal_backend", "aaa")]), "G")
    assert out["nodes"][0]["stale"] is False


@pytest.mark.asyncio
async def test_a_branch_behind_its_own_lane_still_is():
    out = await code_state(_Conn(
        repos=[_repo()],
        lane_rows=[_lane(trunk="cal_backend", sha="ccc"),
                   _lane(trunk="cal_frontend", sha="aaa")],
        nodes=[_node("n1", "feat/a", "cal_backend", "aaa")]), "G")
    assert out["nodes"][0]["stale"] is True


@pytest.mark.asyncio
async def test_a_node_on_an_unknown_lane_claims_nothing():
    """No sha for the lane means no comparison. `stale: false` here is
    "not known to be behind", and inventing a comparison against some
    other lane's sha is exactly what this replaced."""
    out = await code_state(_Conn(
        repos=[_repo()], lane_rows=[_lane(trunk="cal_backend", sha="aaa")],
        nodes=[_node("n1", "feat/a", "", "aaa")]), "G")
    assert out["nodes"][0]["stale"] is False
    assert out["nodes"][0]["lands_on"] is None


# ── where lanes come from ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_goal_that_declared_nothing_still_gets_one_lane():
    """A repo with no declared trunk and no node yet has exactly one
    honest answer, and it is the only one there is to contradict."""
    conn = _Conn(repos=[_repo(trunk="dev")], lane_rows=[],
                 trunk_rows=[{"git_repo_id": "gr_1", "trunk_branch": "dev",
                              "trunk_sha": "ddd",
                              "trunk_sha_seen_at": None}])
    out = await code_state(conn, "G")
    assert out["repos"][0]["lanes"] == [
        {"trunk": "dev", "trunk_sha": "ddd", "trunk_moved_at": None}]


@pytest.mark.asyncio
async def test_the_repo_default_is_not_added_alongside_real_lanes():
    """The third-card bug, one layer down. `git_repos.trunk_branch` is
    often `main` on a goal that works on `cal_backend` and
    `cal_frontend`; listing it too invents a lane nobody works on."""
    conn = _Conn(repos=[_repo(trunk="main")],
                 lane_rows=[_lane(trunk="cal_backend")])
    out = await code_state(conn, "G")
    assert [l["trunk"] for l in out["repos"][0]["lanes"]] == ["cal_backend"]
    assert not any("FROM git_repo_trunks WHERE" in s for s in conn.sql), (
        "the fallback query ran for a repo that already has a lane")


def test_the_lane_query_reads_declared_trunks_and_live_nodes():
    src = lanes_mod._LANES_SQL
    assert "role = 'trunk'" in src and "kind = 'branch'" in src
    assert "FROM git_nodes n" in src
    # OWNER AND NAME, case-insensitively, with the PARENT repo resource
    # standing in: a trunk row's own repo_owner is frequently empty,
    # which is why resolve_repo_trunk matches by parentage.
    assert "p.id = b.parent_id" in src
    assert "lower(r.owner)" in src and "lower(r.name)" in src


def test_it_does_not_link_lanes_through_git_repo_id():
    """`goal_resources.git_repo_id` is written only for engine-cut
    FEATURE branches (`branches.ensure_branch_resource`), so on every
    GO-registered repo and lane row it is NULL. Joining on it finds
    nothing and reports a goal with no lanes."""
    assert "b.git_repo_id" not in lanes_mod._LANES_SQL


def test_the_legacy_single_trunk_is_no_longer_reported_as_the_trunk():
    src = inspect.getsource(code_state)
    assert "t.trunk_branch = gr.trunk_branch" not in src, (
        "the repo row is joined to one git_repo_trunks row again, which "
        "is how one lane's sha came to stand for the whole repository")
