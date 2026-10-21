"""Trunk integration: one plan file per cycle, the full suite, every lane.

Three defects found on BMIF1 run 2, each pinned here:

  * the integration plans overwrote plan-01.md, because a story holding
    its workspace finishes on a `landing` park and only `acceptance`
    counted as the end of a cycle;
  * the canned plan ran no test suite and asked for a report artifact;
  * the sweep sent `lanes` and `shas` as two separately sorted lists, so
    a plan for several repos or lanes could not say which was which.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX
from app.services.tests.test_trunk_catches_up_without_asking_twice import (
    _PoolOnConn, _conn)


# ── the suite check, run for real ────────────────────────────────────

def _run_suite(tmp_path, files: dict) -> subprocess.CompletedProcess:
    from kernel.harness.suite_check import suite_command
    mount = "repos/acme__app"
    root = tmp_path / mount
    root.mkdir(parents=True)
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return subprocess.run([shutil.which("sh"), "-c", suite_command(mount)],
                          cwd=tmp_path, capture_output=True, text=True,
                          timeout=120)


_needs_sh = pytest.mark.skipif(shutil.which("sh") is None,
                               reason="no POSIX shell here")


@_needs_sh
def test_a_repo_with_no_tests_passes(tmp_path):
    got = _run_suite(tmp_path, {"README.md": "hello"})
    assert got.returncode == 0, got.stderr
    assert "no test suite" in got.stdout


@_needs_sh
def test_npms_placeholder_script_is_not_a_suite(tmp_path):
    pkg = {"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}
    got = _run_suite(tmp_path, {"package.json": json.dumps(pkg)})
    assert got.returncode == 0, got.stdout + got.stderr


@_needs_sh
def test_a_passing_python_suite_passes(tmp_path):
    got = _run_suite(tmp_path, {"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    assert got.returncode == 0, got.stdout + got.stderr


@_needs_sh
def test_a_failing_python_suite_fails(tmp_path):
    got = _run_suite(tmp_path, {"test_bad.py": "def test_bad():\n    assert False\n"})
    assert got.returncode != 0


@_needs_sh
def test_vendored_tests_are_not_this_repos_suite(tmp_path):
    got = _run_suite(tmp_path, {
        ".venv/lib/test_vendored.py": "def test_x():\n    assert False\n"})
    assert got.returncode == 0, got.stdout + got.stderr
    assert "no test suite" in got.stdout


def test_the_suite_check_is_a_guard_with_a_budget():
    from kernel.harness.suite_check import suite_check
    c = suite_check("repos/acme__app@web")
    assert c["label"] == "guard" and c["timeout_s"] >= 1200
    assert c["run"].startswith("cd 'repos/acme__app@web' && ")


# ── every lane, paired, mapped to its mount ──────────────────────────

def test_targets_keep_each_sha_with_its_repo_and_lane():
    from app.services.trunk_integration_targets import parse_targets
    t = parse_targets(["acme/b\tweb\tsha-b", "acme/a\tapi\tsha-a"])
    assert t == [{"repo": "acme/a", "lane": "api", "sha": "sha-a"},
                 {"repo": "acme/b", "lane": "web", "sha": "sha-b"}]


def test_two_lanes_of_one_repo_find_their_own_mounts():
    """`assign_subdirs` gives a second write lane `…@<lane>`."""
    from app.services.trunk_integration_targets import mount_for
    mounts = {"repos/acme__x": "api", "repos/acme__x@web": "web",
              "repos/acme__y": "api"}
    assert mount_for({"repo": "acme/x", "lane": "api"}, mounts) == "repos/acme__x"
    assert mount_for({"repo": "acme/x", "lane": "web"}, mounts) == "repos/acme__x@web"
    assert mount_for({"repo": "acme/y", "lane": "api"}, mounts) == "repos/acme__y"
    assert mount_for({"repo": "acme/z", "lane": "api"}, mounts) is None


def test_the_question_names_repo_lane_and_sha_together():
    from kernel.harness.integration_plan import canned_plan
    p = canned_plan(["s1", "s2"], ["api", "web"], targets=[
        {"repo": "acme/x", "lane": "api", "sha": "aaaaaaaaaaaaZZ", "mount": "repos/acme__x"},
        {"repo": "acme/x", "lane": "web", "sha": "bbbbbbbbbbbbZZ", "mount": "repos/acme__x@web"}])
    assert "`acme/x` `api` (aaaaaaaaaaaa)" in p["question"]
    assert "`acme/x` `web` (bbbbbbbbbbbb)" in p["question"]
    assert p["steps"][0].startswith("Lanes to take in:")
    assert "`repos/acme__x@web` <- `acme/x` `web`" in p["steps"][0]


# ── the sweep, end to end against the schema ─────────────────────────

async def _mounted_story(conn, lanes_by_repo: dict):
    """A parked, behind story writing several repos/lanes, with mounts
    and a contract: one command check and one artifact check."""
    from kernel.worker.contracts import new_version
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state) "
        "VALUES ($1,$2,'behind','person','awaiting_merge')", sid, gid)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation) VALUES ($1,$2,'story','code','PARKED_HOT',2) "
        "RETURNING id", gid, sid)
    for name, lanes in lanes_by_repo.items():
        rid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name, trunk_branch) "
            "VALUES ($1,'acme',$2,$3)", rid, name, lanes[0])
        for i, lane in enumerate(lanes):
            nid = uuid.uuid4().hex
            await conn.execute(
                "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
                "state, trunk_branch, last_synced_trunk_sha, created_at) "
                "VALUES ($1,$2,$3,$4,'BRANCH_CREATED',$5,'old',"
                "        now() - interval '1 day')",
                nid, rid, gid, f"feat/{sid}-{name}-{lane}", lane)
            await conn.execute(
                "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
                "VALUES ($1,$2,1)", nid, sid)
            await conn.execute(
                "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, "
                "trunk_sha, trunk_sha_seen_at) VALUES ($1,$2,$3, now())",
                rid, lane, f"new-{name}-{lane}")
            res = f"{sid}-{name}-{lane}"
            await conn.execute(
                "INSERT INTO goal_resources (id, goal_id, kind, name, created_by,"
                " status, repo_owner, repo_name, role, branch_name) "
                "VALUES ($1,$2,'branch',$3,$4,'active','acme',$5,'trunk',$6)",
                res, gid, f"{name}:{lane}", f"{PREFIX}-user", name, lane)
            at = f"repos/acme__{name}" + (f"@{lane}" if i else "")
            await conn.execute(
                "INSERT INTO kernel.execution_mounts (id, execution_id, at, "
                "binding, writable, resource_id) VALUES "
                "((SELECT coalesce(max(id),0)+1 FROM kernel.execution_mounts),"
                " $1,$2,'git',true,$3)", eid, at, res)
    await new_version(conn, eid, [
        {"run": "pytest tests/test_api.py", "label": "goal"},
        {"expect": "artifact", "role": "report", "label": "goal"}],
        "feature plan", "tester")
    return sid, eid


@pytest.mark.asyncio
async def test_the_sweep_plans_every_repo_and_lane_with_its_suite(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _mounted_story(conn, {"x": ["api", "web"], "y": ["api"]})
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 1
        row = await conn.fetchrow(
            "SELECT payload::text AS p, checks::text AS c FROM kernel.questions "
            " WHERE execution_id = $1 AND kind = 'authorization'", eid)
        plan = json.loads(row["p"])
        got = {(t["repo"], t["lane"], t["sha"], t["mount"]) for t in plan["targets"]}
        assert got == {("acme/x", "api", "new-x-api", "repos/acme__x"),
                       ("acme/x", "web", "new-x-web", "repos/acme__x@web"),
                       ("acme/y", "api", "new-y-api", "repos/acme__y")}
        runs = [c["run"] for c in json.loads(row["c"])]
        assert runs[0] == "pytest tests/test_api.py", (
            "the story's own command check was not carried forward -- "
            "approving would replace the contract without it")
        assert not any("artifact" in r for r in runs), (
            "an artifact check was carried into a leg that cannot emit")
        for mount in ("repos/acme__x", "repos/acme__x@web", "repos/acme__y"):
            mine = [r for r in runs if r.startswith(f"cd '{mount}' && ")]
            assert len(mine) == 2, mine
            assert "git grep" in mine[0], "the marker check runs first"
            assert "pytest" in mine[1], "then the suite"


# ── one file per cycle, and the plan spent at the landing ────────────

async def _execution(conn):
    gid = f"{PREFIX}-{uuid.uuid4().hex[:8]}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    return await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, kind, domain, state) "
        "VALUES ($1,'story','code','PARKED_HOT') RETURNING id", gid)


@pytest.mark.asyncio
async def test_an_answered_landing_ends_a_cycle():
    from kernel.harness.cycles import finished_cycles
    async with _conn() as conn:
        eid = await _execution(conn)
        assert await finished_cycles(conn, eid) == 0
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "question_text, payload, status, answered_at, answer_text) VALUES "
            "($1,1,'landing','merge it','{}'::jsonb,'ANSWERED', now(),"
            " 'merged')", eid)
        assert await finished_cycles(conn, eid) == 1


@pytest.mark.asyncio
async def test_an_open_landing_does_not():
    from kernel.harness.cycles import finished_cycles
    async with _conn() as conn:
        eid = await _execution(conn)
        await conn.execute(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "question_text, payload) VALUES ($1,1,'landing','merge it',"
            "'{}'::jsonb)", eid)
        assert await finished_cycles(conn, eid) == 0


def test_both_readers_use_the_one_definition():
    """Two separate predicates drifted once already -- that is the bug."""
    import inspect
    from kernel.harness import park_approval, planfiles
    assert "finished_cycles" in inspect.getsource(planfiles.persist_plan)
    src = inspect.getsource(park_approval)
    assert "CYCLE_ENDING_KINDS" in src
    assert "kind = 'acceptance'" not in src


# ── follow-ups found on BMIF1 run 3 ──────────────────────────────────

@_needs_sh
@pytest.mark.skipif(shutil.which("node") is None, reason="no node here")
def test_nodes_own_runner_is_a_suite(tmp_path):
    """BMIF1's frontend runs `node --test` with no npm script; that read
    as "no test suite" and passed without running anything."""
    got = _run_suite(tmp_path, {"tests/ui.test.js":
        "const t=require('node:test');t('bad',()=>{throw new Error('x')});\n"})
    assert got.returncode != 0, got.stdout + got.stderr
    assert "no test suite" not in got.stdout


def test_the_last_step_commits_the_plan_file():
    """On a clean merge nothing else calls story_commit, so the cycle's
    plan file would be left uncommitted and never reach the PR."""
    from kernel.harness.integration_plan import STEPS
    last = STEPS[-1]
    assert "`story_commit` anything still uncommitted" in last
    assert "plan file" in last
    assert "listed below" not in " ".join(STEPS)


async def _plan_row(conn, eid, payload, *, answered=True):
    await conn.execute(
        "INSERT INTO kernel.questions (execution_id, generation, kind, "
        "question_text, payload, status, answered_at, answer_text) VALUES "
        "($1,1,'authorization','plan approval',$2::jsonb,$3,"
        " CASE WHEN $4 THEN now() END, CASE WHEN $4 THEN 'approve' END)",
        eid, json.dumps(payload), "ANSWERED" if answered else "OPEN", answered)


@pytest.mark.asyncio
async def test_a_platform_plan_is_mirrored_with_a_pinned_timestamp(monkeypatch):
    """Nothing in the card path writes the tree, so the integration
    cycle had no plan-02.md at all. Mirrored where the leg reads the
    scope, stamped with the APPROVAL time so a second leg rewrites the
    same bytes."""
    from kernel.harness import planfiles
    seen = []

    async def _fake(conn, eid, payload, registered_at=None):
        seen.append((payload.get("platform_canned"), registered_at))
        return ["x"]
    monkeypatch.setattr(planfiles, "persist_plan", _fake)
    async with _conn() as conn:
        eid = await _execution(conn)
        await _plan_row(conn, eid, {"steps": ["a"], "platform_canned": True})
        assert await planfiles.mirror_canned_plan(conn, eid) == ["x"]
        assert seen and seen[0][0] is True and seen[0][1] is not None


@pytest.mark.asyncio
async def test_an_executors_own_plan_is_not_mirrored_again(monkeypatch):
    """`register_plan` already wrote it; writing again here would race it."""
    from kernel.harness import planfiles
    seen = []

    async def _fake(*a, **k):
        seen.append(a)
        return ["x"]
    monkeypatch.setattr(planfiles, "persist_plan", _fake)
    async with _conn() as conn:
        eid = await _execution(conn)
        await _plan_row(conn, eid, {"steps": ["a"]})
        assert await planfiles.mirror_canned_plan(conn, eid) == []
        assert not seen


def test_the_pinned_timestamp_makes_the_file_identical():
    import datetime as dt
    from kernel.harness.planfiles import _render
    at = dt.datetime(2026, 10, 13, 13, 38, tzinfo=dt.timezone.utc)
    p = {"steps": ["a"], "checks": [], "rationale": "r"}
    assert _render("s", 2, p, at) == _render("s", 2, p, at)
    assert "2026-10-13 13:38" in _render("s", 2, p, at)


def test_both_leg_paths_mirror_the_live_platform_plan():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("kernel/harness/leg_runner.py", "kernel/worker/loop.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "mirror_canned_plan" in src, rel


# ── merge results the model can edit, and markers that cannot land ───

_needs_git = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="no git/sh here")


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], cwd=cwd, capture_output=True, text=True)


def _repo_with(tmp_path, readme: str):
    from kernel.harness.suite_check import marker_check
    mount = "repos/acme__app"
    root = tmp_path / mount
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text(readme, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    return subprocess.run([shutil.which("sh"), "-c", marker_check(mount)["run"]],
                          cwd=tmp_path, capture_output=True, text=True)


@_needs_git
def test_committed_conflict_markers_fail_the_check(tmp_path):
    """BMIF1 run 3 merged exactly this to trunk with every test green."""
    got = _repo_with(tmp_path, "<<<<<<< HEAD\nours\n=======\ntheirs\n"
                               ">>>>>>> origin/bmi_frontend\n")
    assert got.returncode != 0


@_needs_git
def test_a_markdown_underline_is_not_a_marker(tmp_path):
    got = _repo_with(tmp_path, "Title\n=======\n\ntext\n")
    assert got.returncode == 0, got.stdout + got.stderr


def test_the_merge_runs_with_a_group_writable_umask():
    """The worker merges; the sandbox (another uid, same group) edits."""
    import inspect
    from kernel.workspace import podfs
    src = inspect.getsource(podfs._sync_one)
    assert "**_group_writable_umask()" in src
    assert "_make_merged_files_group_writable(subprocess, path)" in src
    assert src.index("_make_merged_files_group_writable") < src.index(
        '"state": "merged"'), "must run for a clean merge too"


@pytest.mark.skipif(os.name != "posix" or shutil.which("git") is None,
                    reason="POSIX modes and git needed")
def test_a_conflicted_file_is_left_group_writable(tmp_path):
    import stat
    from kernel.workspace import podfs
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "README.md").write_text("base\n")
    (origin / "docs").mkdir()
    (origin / "docs" / "a.md").write_text("base a\n")
    _git(origin, "add", "-A"); _git(origin, "commit", "-q", "-m", "base")
    _git(origin, "checkout", "-q", "-b", "lane")
    (origin / "README.md").write_text("lane side\n")
    (origin / "docs" / "a.md").write_text("lane a\n")
    _git(origin, "commit", "-qam", "lane")
    _git(origin, "checkout", "-q", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    # The pod's clone has an identity; a CI container has none, and
    # `sync_trunk`'s own `git merge` refuses to run without one.
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("branch side\n")
    (work / "docs" / "a.md").write_text("branch a\n")
    _git(work, "commit", "-qam", "branch")
    # As an earlier worker-side merge would have left them.
    os.chmod(work / "README.md", 0o644)
    os.chmod(work / "docs", 0o755)
    got = podfs._sync_one(subprocess, str(work), "lane")
    # A CONFLICT ACROSS SEVERAL FILES, one in a subdirectory.
    assert got["state"] == "conflict"
    assert {"README.md", "docs/a.md"} <= set(got["conflicts"]), got
    for p in (work / "README.md", work / "docs" / "a.md", work / "docs"):
        assert os.stat(p).st_mode & stat.S_IWGRP, p


# ── no sync story for an errand ──────────────────────────────────────

@pytest.mark.asyncio
async def test_an_errand_branch_is_recognised():
    from app.services.tests.test_trunk_catches_up_without_asking_twice import (
        _behind_story)
    from app.services.trunk_integration import node_is_errand
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        node = await conn.fetchval(
            "SELECT git_node_id FROM git_node_stories WHERE story_id = $1", sid)
        assert not await node_is_errand(_PoolOnConn(conn), node)
        await conn.execute(
            "UPDATE stories SET work_kind = 'errand' WHERE id = $1", sid)
        assert await node_is_errand(_PoolOnConn(conn), node)


def test_the_engine_asks_before_it_cuts_a_diff_story():
    """The order is the fix: the errand gate has to come BEFORE
    `_ensure_diff_story`, or the engine still cuts one."""
    import inspect
    from app.services.git_graph import grind
    src = inspect.getsource(grind._grind_one)
    assert src.index("node_is_errand") < src.index("_ensure_diff_story")
