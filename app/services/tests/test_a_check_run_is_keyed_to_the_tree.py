"""A7 -- a recorded check run is reusable only when the tree has not moved.

The defect: a long suite runs while the executor works and again when
the same suite is named as a check, in the same cycle, over a tree
nothing touched in between.

The trap in fixing it: HEAD is the obvious key and the wrong one. An
executor that edits three files and does not commit leaves HEAD
identical over a different tree, and a run reused there is a green tick
over code nothing has run. So the fingerprint has to see uncommitted
work, and these tests spend most of their length proving that it does.

Fast on purpose: real git, tiny repositories, no jobs, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_BRIDGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "kernel", "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

treefp = pytest.importorskip("treefp")
jobspill = pytest.importorskip("jobspill")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """One repository with one commit, and git configured enough to use it."""
    root = tmp_path / "work"
    root.mkdir()
    try:
        _git(["init", "-q", "-b", "main", "."], root)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no usable git")
    _git(["config", "user.email", "t@example.invalid"], root)
    _git(["config", "user.name", "T"], root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "one"], root)
    return root


def _head(where):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=where,
                          capture_output=True, text=True).stdout


# -- the fingerprint sees what HEAD does not ---------------------------

def test_an_uncommitted_edit_moves_the_fingerprint(repo):
    """The whole point. HEAD is identical across this and the tree is not."""
    before = treefp.fingerprint(str(repo))
    head_before = _head(repo)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    after = treefp.fingerprint(str(repo))

    assert head_before == _head(repo), "the test did not commit; HEAD must hold"
    assert before and after
    assert before != after, (
        "an uncommitted edit left the fingerprint unchanged -- a check "
        "recorded before it would be reused over code nothing has run")


def test_an_untracked_file_moves_the_fingerprint(repo):
    """A new test file nobody has added yet is still part of what runs."""
    before = treefp.fingerprint(str(repo))
    (repo / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert treefp.fingerprint(str(repo)) != before


def test_nothing_changing_leaves_the_fingerprint_alone(repo):
    """Stability is the other half: a jittery key reuses nothing, ever."""
    assert treefp.fingerprint(str(repo)) == treefp.fingerprint(str(repo))


def test_committing_the_same_content_leaves_the_fingerprint_alone(repo):
    """The tree is the key, not the history.

    Staging and committing what is already on disk changes HEAD and
    changes nothing a check would measure.
    """
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    before = treefp.fingerprint(str(repo))
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "two"], repo)
    assert treefp.fingerprint(str(repo)) == before


def test_the_real_index_is_not_touched(repo):
    """A scratch index, so fingerprinting is not a side effect.

    If this ever staged into the real index, `story_commit` would start
    committing whatever a fingerprint happened to catch.
    """
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    treefp.fingerprint(str(repo))
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True).stdout
    assert staged.strip() == "", f"fingerprinting staged {staged!r}"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "?? b.py" in status, "the file stopped being untracked"


# -- the spill directory is excluded, or A5 defeats A7 -----------------

def test_the_spill_directory_does_not_move_the_fingerprint(repo):
    """A job's own log grows inside the tree while the job runs.

    If that moved the key, every run would be stale the moment it
    finished and nothing would ever be reused.
    """
    jobspill._ensure_excluded(str(repo))
    before = treefp.fingerprint(str(repo))
    spill = repo / jobspill.SPILL_DIR
    spill.mkdir(exist_ok=True)
    (spill / "job-1.log").write_text("line\n" * 500, encoding="utf-8")
    assert treefp.fingerprint(str(repo)) == before


def test_one_name_for_the_spill_directory():
    """Read by the writer, the exclude line and the fingerprint."""
    import inspect
    assert "SPILL_DIR" in inspect.getsource(treefp.git_roots)
    assert "SPILL_DIR" in inspect.getsource(treefp._excludes_file)


# -- several repositories are a set, not a sha -------------------------

def test_every_repository_is_named_in_the_fingerprint(tmp_path):
    """One sha over a multi-repo workspace names a fraction of what ran."""
    root = tmp_path / "ws"
    (root / "repos").mkdir(parents=True)
    made = []
    for name in ("acme__api", "acme__web"):
        where = root / "repos" / name
        where.mkdir()
        try:
            _git(["init", "-q", "-b", "main", "."], where)
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("no usable git")
        _git(["config", "user.email", "t@example.invalid"], where)
        _git(["config", "user.name", "T"], where)
        (where / f"{name}.txt").write_text(name, encoding="utf-8")
        _git(["add", "-A"], where)
        _git(["commit", "-qm", "one"], where)
        made.append(where)

    assert treefp.git_roots(str(root)) == ["repos/acme__api",
                                           "repos/acme__web"]
    before = treefp.fingerprint(str(root))
    assert "repos/acme__api=" in before and "repos/acme__web=" in before

    # A change in the SECOND repository must move the whole key.
    (made[1] / "new.txt").write_text("z", encoding="utf-8")
    assert treefp.fingerprint(str(root)) != before


def test_a_workspace_with_no_repository_fingerprints_to_nothing(tmp_path):
    """Empty means no evidence is usable, never that nothing changed."""
    assert treefp.fingerprint(str(tmp_path)) == ""


# -- the key, and what it deliberately is not --------------------------

def test_the_lookup_is_keyed_on_command_and_tree_and_not_a_criterion():
    import inspect

    from kernel.harness import claim

    src = inspect.getsource(claim.prior_run)
    assert "j.command = $2" in src
    assert "j.fingerprint = $3" in src
    assert "j.fingerprint_end = $3" in src, (
        "a run whose tree moved WHILE it ran measured nothing in particular")
    assert "criterion" not in src.lower(), (
        "keying on the criterion makes the plan-phase run and the "
        "checks-phase run different events, which is the defect")
    assert "j.running = false" in src and "exit_code IS NOT NULL" in src


def test_an_absent_fingerprint_reuses_nothing():
    """The safe direction is always to run the check."""
    import asyncio

    from kernel.harness import claim

    class Boom:
        async def fetchrow(self, *a):
            raise AssertionError("queried with no fingerprint")

    assert asyncio.run(claim.prior_run(Boom(), 1, "pytest", "")) is None
    assert asyncio.run(claim.prior_run(Boom(), 1, "   ", "abc")) is None


def test_the_fingerprint_is_taken_twice_and_not_per_poll():
    """A `git add -A` per poll on a two-hour job is hundreds of them."""
    import asyncio

    from kernel.harness import claim

    taken = []

    class Ex:
        _port = 0
        _host = ""

    async def fake_take(ex):
        taken.append(1)
        return f"fp{len(taken)}"

    ex = Ex()
    original, claim.take = claim.take, fake_take
    try:
        running = {"id": "j1", "running": True}
        first = asyncio.run(claim.stamp(ex, running))
        for _ in range(20):
            asyncio.run(claim.stamp(ex, running))
        last = asyncio.run(claim.stamp(ex, {"id": "j1", "running": False}))
        for _ in range(5):
            asyncio.run(claim.stamp(ex, {"id": "j1", "running": False}))
    finally:
        claim.take = original

    assert len(taken) == 2, f"fingerprinted {len(taken)} times, not twice"
    assert first["fingerprint"] == "fp1"
    assert last["fingerprint"] == "fp1" and last["fingerprint_end"] == "fp2"


def test_a_mid_job_report_never_erases_a_recorded_fingerprint():
    """The panel writes an empty string as no news, not as an erasure."""
    src = open("app/routers/execution_jobs.py", encoding="utf-8").read()
    assert "CASE WHEN EXCLUDED.fingerprint <> " in src
    assert "CASE WHEN EXCLUDED.fingerprint_end <> " in src


def test_the_story_is_resolved_by_the_panel_not_sent_by_the_pod():
    """One SELECT beats a second source of truth on the wire."""
    router = open("app/routers/execution_jobs.py", encoding="utf-8").read()
    mirror = open("kernel/worker/job_mirror.py", encoding="utf-8").read()
    assert "SELECT COALESCE(story_id, " in router
    assert '"story_id":' not in mirror


# -- sync_trunk, the routine invalidator -------------------------------

def test_a_sync_that_moved_the_tree_says_the_checks_will_re_run():
    from kernel.harness.executor_tools import _sync_result

    moved = _sync_result({"repos/a__b": {"state": "merged", "behind": 3}}, "t")
    assert "will run again" in moved.output

    still = _sync_result({"repos/a__b": {"state": "already"}}, "t")
    assert "will run again" not in still.output, (
        "a sync that took nothing changed nothing; the recorded runs stand")


# -- run_check reuses, and says so -------------------------------------

def test_run_check_reports_the_job_a_reused_result_came_from():
    """A pass nobody can locate is worse than one that takes forty minutes."""
    import inspect

    from kernel.harness.tools_worker import checks

    src = inspect.getsource(checks.run_check)
    assert "claim.prior_run" in src
    assert "previous['job_id']" in src or 'previous["job_id"]' in src
    assert "already ran against this exact tree" in src


def test_run_check_asks_before_it_launches():
    """Reuse after starting the job would have cost the forty minutes."""
    import inspect

    from kernel.harness.tools_worker import checks

    src = inspect.getsource(checks.run_check)
    assert src.index("claim.prior_run") < src.index("bash_background"), (
        "the evidence lookup must come before the launch")


def test_an_edit_git_cannot_see_by_stat_still_moves_the_fingerprint(repo):
    """THE BUG CI FOUND, MADE DETERMINISTIC.

    The first version copied `.git/index` to keep git's stat cache, so
    `git add -A` re-hashed only what looked changed. The copy's mtime is
    the moment of the copy, so every file appears older than the index
    and is trusted clean on stat alone -- git's racy-timestamp
    protection never engages -- and an edit that keeps the file's SIZE
    is never re-hashed.

    Locally the mtimes happened to differ and the plain test passed; in
    CI, same second and same size, it did not. So this one does not
    depend on timing: it writes the same number of bytes and then
    RESTORES the original mtime, which is the worst case that filesystem
    resolution can produce, on any filesystem.
    """
    import os

    target = repo / "a.py"
    before_stat = target.stat()
    before = treefp.fingerprint(str(repo))

    target.write_text("x = 9\n", encoding="utf-8")       # same six bytes
    os.utime(target, (before_stat.st_atime, before_stat.st_mtime))

    assert target.stat().st_size == before_stat.st_size
    assert target.stat().st_mtime == before_stat.st_mtime
    assert treefp.fingerprint(str(repo)) != before, (
        "the fingerprint is trusting git's stat cache: an edit of the "
        "same size at the same mtime went unseen, which is a reused "
        "check over code nothing has run")


def test_the_fingerprint_never_reuses_gits_stat_cache():
    """A STRUCTURAL GUARD, because the behavioural one cannot run here.

    The bug was `shutil.copyfile(.git/index, scratch)`: the copy carries
    git's stat cache, the copy's mtime is newer than every file, so
    nothing is racily-clean and a same-size edit is trusted unchanged.

    The test above reproduces that only where the filesystem lets it.
    Git's cache compares ctime as well as mtime, and on Windows ctime
    cannot be restored by `os.utime` -- so the edit always looks dirty,
    the cache is defeated for the wrong reason, and the test passes even
    with the bug present. It was CI, on Linux, that found this.

    A guard that only fires on one platform is a guard that lets the
    next person reintroduce this locally and discover it in a build. So
    this reads the source instead: the fingerprint runs against an EMPTY
    index, always, and pays the hash.
    """
    import inspect

    src = inspect.getsource(treefp.tree_sha)
    code = "\n".join(l for l in src.splitlines()
                     if l.strip() and not l.lstrip().startswith("#"))
    assert "copyfile" not in code, (
        "the index is being copied again -- that carries git's stat "
        "cache and a same-size edit stops moving the fingerprint")
    assert "GIT_INDEX_FILE" in code, "the real index must stay untouched"
