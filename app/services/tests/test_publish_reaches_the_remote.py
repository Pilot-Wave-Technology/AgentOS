"""I5 · a story's commits leave the workspace, and DONE says so honestly.

THE DEFECT SHAPE THIS GUARDS. `story_commit` commits and `GitDriver.publish`
pushes, and for one release nothing joined them: the first live story ended
with seven commits in a sandbox volume — [ahead 7] — while the engine merged
the empty branch it had cut and announced a landing. `publish_execution` was
written to close that, and NOTHING IN THE SUITE ASSERTED IT WAS CALLED. Delete
the one line in `finish_pass.py` that calls it and every test still passed,
which is the same "no caller" state the fix was written to leave behind.

So the first test here drives the real chain — adjudicator tail → publish →
per-mount driver — with only the git driver itself replaced. It fails if any
link is removed, which a test that exercises `CompositeDriver.publish()`
directly cannot do.

THE SECOND DEFECT, found while wiring the guard. `CompositeDriver.publish`
silently SKIPPED a writable mount whose directory was absent, so a workspace
the adjudicator could not see — `replicas: 2` is deliberate and the tree is
local to one conductor — produced zero refs, "nothing to push", and DONE. A
false delivery, reported as success, by the very function that exists to make
false delivery impossible.
"""
from __future__ import annotations

import io
import os
import pathlib
import shutil
import subprocess

import pytest

from kernel.workspace import composite
from kernel.workspace.composite import MOUNT_MISSING
from kernel.workspace.deliver import publish_execution
from kernel.workspace.driver import CheckpointRef

ROOT = pathlib.Path(__file__).resolve().parents[3]
MOUNT = "repos/acme__calc"


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _row(at: str, writable: bool = True) -> dict:
    return {"at": at, "binding": "git", "source": "https://x.invalid/r.git",
            "ref": "feat/GOAL-story", "writable": writable, "kind": "repo",
            "resource_id": "r1"}


class _Conn:
    """The reads `publish_execution` makes, and every write it provokes.

    `events` is shared with the fake driver so ORDER is assertable: the
    push has to happen before the DONE write, not merely alongside it.
    """

    def __init__(self, root: str, mounts: list, events: list,
                 lanes: tuple = (), kind: str = "story"):
        self.root, self._mounts, self._lanes, self._kind = (
            root, mounts, lanes, kind)
        self.events = events

    async def fetchval(self, sql, *args):
        if "workspace_path" in sql:
            return self.root
        if "workspace_source" in sql:
            return None
        if "SELECT kind" in sql:
            return self._kind
        return None

    async def fetch(self, sql, *args):
        if "execution_mounts" in sql:
            return self._mounts
        if "goal_resources" in sql:
            return [{"branch_name": b} for b in self._lanes]
        return []

    async def execute(self, sql, *args):
        self.events.append(("execute", " ".join(sql.split())))
        return "UPDATE 1"


class _Driver:
    """Stands in for GitDriver so no network is needed. Records the push."""

    def __init__(self, events: list, boom: Exception | None = None):
        self.events, self.boom = events, boom

    def publish(self, path: str) -> CheckpointRef:
        if self.boom:
            raise self.boom
        self.events.append(("push", os.path.normpath(path)))
        return CheckpointRef(kind="git", ref="c0ffee1", detail="pushed")


def _wire(monkeypatch, events, boom=None):
    monkeypatch.setattr(composite, "driver_for",
                        lambda name: _Driver(events, boom))


def _tree(tmp_path, *mounts) -> str:
    root = tmp_path / "exec-1"
    for m in mounts:
        (root / m).mkdir(parents=True)
    root.mkdir(exist_ok=True)
    return str(root)


async def _finish(conn, eid: int = 1):
    from kernel.verifier.finish_pass import finish_passed_claim

    return await finish_passed_claim(
        conn, eid, 3, 7, {"id": eid, "generation": 3, "kind": "story"})


# ── the guard the original defect needed ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_passing_claim_pushes_before_it_grants_done(tmp_path,
                                                            monkeypatch):
    """THE TEST THAT WOULD HAVE CAUGHT [ahead 7]. It calls nothing under
    `kernel/workspace/` directly: it enters at the adjudicator's tail, the
    way a real pass does, and asserts a push came out of the far end."""
    monkeypatch.delenv("AGENTOS_HUMAN_DONE", raising=False)
    events: list = []
    _wire(monkeypatch, events)
    root = _tree(tmp_path, MOUNT)
    conn = _Conn(root, [_row(MOUNT)], events)

    out = await _finish(conn)

    assert ("push", os.path.normpath(os.path.join(root, MOUNT))) in events, (
        "a claim passed and nothing pushed — the commits are in the "
        "workspace and the engine's branch is empty")
    assert out["action"] == "done"
    done = next(i for i, e in enumerate(events)
                if e[0] == "execute" and "state = 'DONE'" in e[1])
    assert events.index(("push", os.path.normpath(os.path.join(root, MOUNT)))) \
        < done, "DONE was written before the push; an interrupted push " \
                "would leave a delivered story that is not delivered"


def test_the_chain_from_the_verdict_to_the_push_has_no_missing_link():
    """Named link by link, because the behavioural test above can be
    satisfied by a future refactor that mocks one layer out. Each of these
    is a call that was, at some point, the one nobody had made."""
    assert "finish_passed_claim" in _read("kernel/verifier/adjudicator.py")
    fin = _read("kernel/verifier/finish_pass.py")
    assert "publish_execution(conn, eid)" in fin, (
        "the pass path no longer publishes before it finishes the run")
    assert "CompositeDriver().publish" in _read("kernel/workspace/deliver.py")


# ── per-mount, and only the ones it may touch ────────────────────────────

@pytest.mark.asyncio
async def test_a_read_only_mount_is_never_pushed(tmp_path, monkeypatch):
    """A doc mounted as input has no remote and no business carrying a
    commit, let alone a push."""
    events: list = []
    _wire(monkeypatch, events)
    root = _tree(tmp_path, MOUNT, "docs")
    conn = _Conn(root, [_row(MOUNT), _row("docs", writable=False)], events)

    ok, _ = await publish_execution(conn, 1)

    assert ok
    assert [e for e in events if e[0] == "push"] == [
        ("push", os.path.normpath(os.path.join(root, MOUNT)))]


@pytest.mark.asyncio
async def test_a_mount_with_nothing_to_push_is_not_an_error(tmp_path,
                                                            monkeypatch):
    """`GitDriver.publish` answers "no remote" for a corpus origin, and E0
    runs entirely on those. Treating a quiet mount as a failure would stall
    every local story on a rule it obeyed."""
    events: list = []
    monkeypatch.setattr(
        composite, "driver_for",
        lambda name: type("D", (), {"publish": staticmethod(
            lambda p: CheckpointRef(kind="git", ref="", detail="no remote"))})())
    root = _tree(tmp_path, MOUNT)

    ok, detail = await publish_execution(_Conn(root, [_row(MOUNT)], events), 1)

    assert ok
    assert "no remote" in detail


# ── the silent skip ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_writable_mount_that_is_not_on_disk_is_not_delivered(
        tmp_path, monkeypatch):
    """THE SECOND [ahead 7]. The mount directory is absent — the shape a
    second conductor replica, or a volume reclaimed before the verdict,
    presents — and the old code skipped it and reported "nothing to push"."""
    events: list = []
    _wire(monkeypatch, events)
    root = _tree(tmp_path)                       # the mount subdir is absent

    ok, detail = await publish_execution(_Conn(root, [_row(MOUNT)], events), 1)

    assert ok is False, (
        "a mount that was never looked at was reported as delivered")
    assert MOUNT in detail, "the refusal does not name the mount"
    assert not [e for e in events if e[0] == "push"]


def test_the_two_operations_report_an_absent_mount_the_same_way():
    """`checkpoint` recorded it and `publish` dropped it. One spelling, in
    one place, so the check in `deliver.py` cannot drift off it."""
    src = _read("kernel/workspace/composite.py")
    assert src.count("MOUNT_MISSING") >= 3
    assert "MOUNT_MISSING" in _read("kernel/workspace/deliver.py")


@pytest.mark.asyncio
async def test_an_undelivered_pass_returns_the_story_to_running(tmp_path,
                                                                monkeypatch):
    """Loudly: RUNNING, a `stall_reason` a person reads, and a steer the
    executor reads. Anything quieter is the false DONE again."""
    monkeypatch.delenv("AGENTOS_HUMAN_DONE", raising=False)
    events: list = []
    _wire(monkeypatch, events)
    conn = _Conn(_tree(tmp_path), [_row(MOUNT)], events)

    out = await _finish(conn)

    assert out["action"] == "publish_failed"
    sqls = [e[1] for e in events if e[0] == "execute"]
    # ACROSS THE STATEMENTS, NOT WITHIN ONE. This used to be a single
    # UPDATE and the assertion read it as such. The reason and the
    # return-to-RUNNING are now written separately, because a run that
    # PARKS on a forge refusal must get the reason (so the card posts)
    # without the leg and without being sent back to RUNNING -- see
    # kernel/worker/publish_block.py. Both facts this test is about are
    # unchanged; only how many statements carry them.
    assert any("state = 'RUNNING'" in s for s in sqls),         "an undelivered pass must not be left VERIFYING"
    assert any("stall_reason" in s for s in sqls),         "an undelivered pass must leave a reason a person can read"
    assert not any("state = 'DONE'" in s for s in sqls)


@pytest.mark.asyncio
async def test_a_push_that_raises_never_reaches_done(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTOS_HUMAN_DONE", raising=False)
    events: list = []
    _wire(monkeypatch, events, boom=RuntimeError("remote rejected"))
    conn = _Conn(_tree(tmp_path, MOUNT), [_row(MOUNT)], events)

    out = await _finish(conn)

    assert out["action"] == "publish_failed"
    assert "remote rejected" in out["detail"]


# ── and against a real remote ────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_the_commits_actually_reach_the_remote(tmp_path):
    """The property I5 states, against real git rather than a fake driver:
    after a completion, `ls-remote` shows the story's commit. Nothing here
    is mocked but the database."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("seed", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")

    root = tmp_path / "exec-9"
    (root / "repos").mkdir(parents=True)
    work = root / MOUNT
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "feature.txt").write_text("the story's work", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "leg 1: the work")
    sha = _git(work, "rev-parse", "HEAD")

    ok, detail = await publish_execution(_Conn(str(root), [_row(MOUNT)], []), 9)

    assert ok, detail
    assert sha in _git(tmp_path, "ls-remote", str(origin)), (
        "the commit is not on the remote — this is [ahead 7]")
    assert MOUNT_MISSING not in detail
