"""Summaries follow the branches, not the repository.

WHERE THIS STARTED. A repo resource was summarised from
`goal_resources.default_branch` -- GitHub's `main` -- while the goal
worked elsewhere, so the card described a branch nobody was on.

THE FIX THAT WAS WRONG. The first attempt resolved "the trunk" here,
with its own precedence and its own ordering. That was a worse bug in
better clothes: `resolve_repo_trunk` -- what the engine cuts and merges
by -- already answered it, and mine took the NEWEST declared trunk where
it takes the OLDEST. On a two-trunk goal the engine would cut from one
branch while the summary described the other.

(The docstring there used to name a second resolver kept in step with
it, `_build_trunk_branch_map` in go_agent.py. That block stopped being
assembled when GO moved to tools; the function was dead and is now
deleted.)

THE MODEL, WHICH THE ENGINE ALREADY HAD. Work happens at BRANCH level.
A goal declares the branches it works on as resources; ONE REPOSITORY
CAN CARRY SEVERAL TRUNK LANES IN ONE GOAL -- a backend branch and a
frontend branch of one app -- and the lane a story lands on is the
trunk resource it was GRANTED (`repo_identity.resolve_ws_trunk`).
Nothing in the engine asks "what is THE repo's branch".

SO THE REPO CARD STOPS ANSWERING A QUESTION NOBODY ASKS. A summary is
one branch's tree; a repo card can only show one of the lanes, which
makes it an overview that silently equals one card beneath it and
contradicts the rest. Observed on NCAL2: two declared trunks, each
correctly summarised, and a repo card describing a third branch.

A goal that has declared NOTHING still gets a repo summary -- it is the
only thing there is to describe, and with no lanes there is nothing for
it to contradict.
"""
from __future__ import annotations

import pytest

from app.services import resource_scanner as scanner


class _Pool:
    def __init__(self, trunks=()):
        self.trunks = list(trunks)
        self.asked: list = []

    async def fetch(self, sql, *args):
        self.asked.append((sql, args))
        return [{"branch_name": b} for b in self.trunks]


@pytest.fixture()
def pool(monkeypatch):
    def _use(trunks=()):
        p = _Pool(trunks)
        monkeypatch.setattr(scanner, "get_pool", lambda: p)
        return p
    return _use


# ── which trunks a goal has declared ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_goal_can_declare_several_trunks_for_one_repo(pool):
    """One repository, a backend lane and a frontend lane, in one goal.
    This is the shape `resolve_ws_trunk` already supports and the shape
    the summaries now follow."""
    pool(["cal_backend", "cal_frontend"])
    assert sorted(await scanner.declared_trunks("NCAL2", "app_repo")) ==         ["cal_backend", "cal_frontend"]


@pytest.mark.asyncio
async def test_it_asks_only_for_trunk_role_branches_of_this_goal(pool):
    p = pool(["cal_backend"])
    await scanner.declared_trunks("NCAL2", "app_repo", "acme")
    sql, args = p.asked[0]
    assert "role = 'trunk'" in sql and "kind = 'branch'" in sql
    assert "goal_id = $1" in sql
    # OWNER AS WELL AS NAME: two repositories with the same name from
    # different owners in one goal would otherwise suppress each
    # other's summaries, and the symptom is a card never built.
    assert "repo_owner" in sql
    assert args == ("NCAL2", "app_repo", "acme")


@pytest.mark.asyncio
async def test_a_deleted_or_inactive_trunk_does_not_count(pool):
    p = pool([])
    await scanner.declared_trunks("NCAL2", "app_repo")
    assert "status" in p.asked[0][0], (
        "a removed trunk resource would still suppress the repo summary")


@pytest.mark.asyncio
async def test_no_goal_or_no_repo_declares_nothing(pool):
    pool(["x"])
    assert await scanner.declared_trunks("", "app_repo") == []
    assert await scanner.declared_trunks("NCAL2", "") == []


# ── the repo card steps aside ────────────────────────────────────────

def test_the_repo_scan_is_skipped_when_trunks_are_declared():
    import inspect
    src = inspect.getsource(scanner.scan_resource)
    assert "declared_trunks" in src
    assert "skipped" in src, (
        "the repo row is still summarised alongside the branch cards, "
        "so the overview can contradict them")


def test_a_goal_with_no_declared_trunk_still_gets_one():
    """The only thing there is to describe, and nothing to contradict."""
    import inspect
    src = inspect.getsource(scanner.scan_resource)
    assert "if trunks:" in src, (
        "the repo summary must be skipped CONDITIONALLY -- a goal that "
        "added only a repo would otherwise have no summary at all")


def test_there_is_no_second_trunk_resolver():
    """`resolve_repo_trunk` answers this for the engine. A second one
    disagreed with it."""
    import inspect
    src = inspect.getsource(scanner.trunk_branch_for)
    assert "git_nodes" not in src and "git_repos" not in src, (
        "the scanner resolves the trunk itself again, which is how it "
        "came to take the newest where the engine takes the oldest")


def test_a_branch_resource_still_scans_its_own_branch():
    import inspect
    src = inspect.getsource(scanner.scan_resource)
    assert 'resource.get("branch_name")' in src


# ── and the callers actually use it ──────────────────────────────────

def test_the_scanner_resolves_it_rather_than_trusting_the_caller():
    import inspect
    src = inspect.getsource(scanner.scan_resource)
    assert "trunk_branch_for" in src
    assert 'resource.get("default_branch", "main")' not in src, (
        "the repo branch is read straight off the resource again, which "
        "is the value that was wrong")


def test_a_branch_resource_still_scans_its_own_branch():
    """Only the FALLBACK changed. A branch resource is about its branch;
    scanning the trunk instead would summarise the wrong thing in the
    one case that was always right."""
    import inspect
    src = inspect.getsource(scanner.scan_resource)
    assert 'resource.get("branch_name")' in src


def test_the_creation_path_no_longer_passes_main():
    import inspect

    from app.routers import goal_resources
    src = inspect.getsource(goal_resources)
    assert "repo_name, default_branch))" not in src, (
        "creation hands GitHub's default branch to the scanner, which is "
        "the value that was wrong; passing none means 'the trunk'")


# ── starting a scan returns ──────────────────────────────────────────

def test_the_scan_endpoint_does_not_await_the_scan():
    import inspect

    from app.routers import go_internal
    src = inspect.getsource(go_internal.resource_scan)
    assert "create_task" in src, (
        "the endpoint awaits a GitHub fetch, a gitingest pass and an LLM "
        "summary, so whatever asked for it waits for all three")
    assert "202" in src


def test_a_missing_resource_is_still_refused():
    """Scheduling first would turn every mistyped id into 'started', with
    the failure only in a log line nobody reads."""
    import inspect

    from app.routers import go_internal
    src = inspect.getsource(go_internal.resource_scan)
    assert "404" in src
    assert src.index("404") < src.index("create_task")
