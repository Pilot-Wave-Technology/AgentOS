"""P2 (plan 11.4) — the records a FAILED env setup leaves behind.

`ensure_setup` is the step that installs a story's dependencies, and
plan section 5 is about what happens when the environment a check runs
against is not the environment the work was done in. Three of its four
writes happen only when something went wrong, and two of them were
missing the guard the success path has:

  * `run_setups`'s no-manifest path declines to run the scout on a pod,
    with a paragraph saying why -- the scout WRITES, and a pod's
    workspace is not on the conductor's filesystem. The failure path
    called the same scout with no guard at all, and then wrote
    SETUP_FAILED.md with a bare `open(..., "w")` beside it.
  * the done-marker was written with `open(marker, "w")`, which
    TRUNCATES BEFORE IT WRITES. The OSError was swallowed, so a write
    that failed left a zero-byte marker -- and `already_done` asks
    `os.path.exists`. `kernel/verifier/volume.py:stale()` states the
    same rule from the other side: an absent stamp is the safe
    direction, a half-written one is not.

WHY A CONDUCTOR-SIDE WRITE IS WORSE THAN A FAILED ONE. `tmp_path` here
stands in for the case that actually corrupts something: a directory
that EXISTS on the conductor at the pod workspace's path, left by an
earlier local run. The note and the draft then land in a real tree that
belongs to somebody else's execution, where the scout's own `_gather`
reads them back as that workspace's own files.

These tests drive `ensure_setup`, not `run_setups`. Plan section 6
warns that the obvious fix -- copy the success path's `if remote:`
onto the failure path -- is a `NameError`, because `remote` is a local
of `run_setups`; a test that only covered `run_setups` would pass
against that fix while the failure path crashed instead of explaining
itself. They also touch only names `envsetup` had before this change,
so they can be run against the old module to check they fail there.
"""

from __future__ import annotations

import builtins
import glob
import os

import pytest

from kernel.bridge.client import BRIDGE_HOST
from kernel.worker import envsetup

POD = "10.244.1.7"
CONTAINER = "agentos-exec-211"
MOUNT = "repos/acme__thing"
OUTPUT = (f"[{MOUNT}:requirements.txt] exit=1 "
          f"No matching distribution found for nosuch==9")


class _Conn:
    """Enough of a connection to capture what was written and how."""

    def __init__(self):
        self.sql: list = []

    async def execute(self, sql, *args):
        self.sql.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    async def fetch(self, *a):
        return []

    def stalls(self) -> list[str]:
        return [sql for sql, _ in self.sql if "stall_reason" in sql]


def _row(tmp_path, host: str) -> dict:
    return {"id": 211, "workspace_path": str(tmp_path),
            "container_id": CONTAINER, "bridge_port": 9100,
            "workspace_host": host}


def _left_behind(tmp_path) -> list[str]:
    return sorted(os.path.basename(p)
                  for p in glob.glob(str(tmp_path / "*"))
                  + glob.glob(str(tmp_path / ".*")))


@pytest.fixture()
def drafted(monkeypatch):
    """The install stubbed out, failing; the scout recording its calls.

    The scout is patched at `kernel.worker.scout`, where BOTH the old
    inline loop and the guarded one import it from, so this fixture does
    not encode which module makes the call.
    """
    async def _run(conn, eid, workspace, port, host=BRIDGE_HOST):
        return {"setups": [f"{MOUNT}:requirements.txt"], "state": "done",
                "ok": False, "output": OUTPUT}

    calls: list = []

    async def _suggest(workspace, at, reason):
        calls.append((workspace, at))
        return ""

    monkeypatch.setattr(envsetup, "run_setups", _run)
    monkeypatch.setattr("kernel.worker.scout.suggest_setup", _suggest)
    return calls


def _a_pod_with_no_marker(monkeypatch):
    """A pod workspace that has not run its setups, and no bridge.

    Both spellings of the marker write are stubbed: it lived in
    `envsetup._mark_done` before this change and in `envsetup.mark_done`
    (imported from `envrecord`) after it. Without this the test would
    open a TCP connection to a pod address and wait for it.
    """
    monkeypatch.setattr("kernel.workspace.podfs_client.read_file",
                        lambda *a, **k: (b"", "not found"))

    async def _skip(*a, **k):
        return None

    for name in ("mark_done", "_mark_done"):
        monkeypatch.setattr(envsetup, name, _skip, raising=False)


@pytest.mark.asyncio
async def test_a_pod_setup_failure_writes_nothing_on_the_conductor(
        tmp_path, monkeypatch, drafted):
    """THE BUG. Two writes, neither of which belongs on this machine.

    Fails against the old module, which writes SETUP_FAILED.md into
    `tmp_path` and calls the scout, which writes SETUP_SUGGESTED.md
    beside it -- into a tree that is not the execution's workspace.
    """
    _a_pod_with_no_marker(monkeypatch)
    conn = _Conn()

    action = await envsetup.ensure_setup(conn, _row(tmp_path, POD))

    assert _left_behind(tmp_path) == [], (
        "a pod execution's failure note was written to the conductor's "
        "own filesystem, where the next reader of that path believes it")
    assert drafted == [], (
        "the scout ran against a tree that is not this workspace -- and "
        "it spends a model call before it discovers it cannot write")
    # AND THE FAILURE IS STILL REPORTED. Guarding the writes must not
    # make a pod's setup failure silent: both channels that reach a
    # person and a model without touching a filesystem still carry it.
    assert action["action"] == "setup_failed"
    assert "No matching distribution" in action["detail"]
    assert conn.stalls(), "nothing said why the environment is missing"


@pytest.mark.asyncio
async def test_a_local_setup_failure_still_writes_both(tmp_path, monkeypatch,
                                                       drafted):
    """Non-vacuity. On Docker the workspace IS on this filesystem, and
    the note and the draft are how the model finds out what to fix --
    a guard that removed them everywhere would pass the test above."""
    conn = _Conn()

    await envsetup.ensure_setup(conn, _row(tmp_path, BRIDGE_HOST))

    body = (tmp_path / "SETUP_FAILED.md").read_text(encoding="utf-8")
    assert body.startswith("# Workspace setup failed")
    assert "No matching distribution" in body
    assert body.rstrip().endswith("```"), "the note is truncated"
    assert drafted == [(str(tmp_path), MOUNT)]


@pytest.mark.asyncio
async def test_a_marker_that_could_not_be_written_is_absent_not_empty(
        tmp_path, monkeypatch):
    """THE HALF-WRITTEN STAMP, which is the unsafe direction.

    Simulates the ending `open(path, "w")` has no answer for: the
    truncate succeeds and the write does not. Fails against the old
    module, which leaves a zero-byte `.agentos-setup-*` behind and
    swallows the error -- after which `already_done` reports that this
    sandbox has had its setups run, for the life of the container, and
    the leg gets an environment nobody installed.
    """
    real_open = builtins.open

    def _disk_full(path, *a, **kw):
        if str(path).startswith(str(tmp_path)):
            real_open(path, "w").close()          # the truncate lands...
            raise OSError(28, "No space left on device")   # ...the write
        return real_open(path, *a, **kw)

    async def _run(conn, eid, workspace, port, host=BRIDGE_HOST):
        return {"setups": [f"{MOUNT}:requirements.txt"], "state": "done",
                "ok": True, "output": "installed 41 packages"}

    monkeypatch.setattr(envsetup, "run_setups", _run)
    monkeypatch.setattr(builtins, "open", _disk_full)

    action = await envsetup.ensure_setup(_Conn(), _row(tmp_path, BRIDGE_HOST))

    assert action["action"] == "setup"
    assert glob.glob(str(tmp_path / ".agentos-setup-*")) == [], (
        "a marker the writer could not finish is still a marker to "
        "`os.path.exists`, and no temp file may be left claiming to be "
        "one either")
    assert not await envsetup._already_done(str(tmp_path), CONTAINER,
                                            BRIDGE_HOST, 211), (
        "the next pass skips the setups on the strength of a file that "
        "was never written")


@pytest.mark.asyncio
async def test_the_stall_note_cannot_land_on_a_run_that_ended(
        tmp_path, monkeypatch, drafted):
    """The fourth write, and the only one that is a row.

    A pass can watch an install for SETUP_WAIT_S; a run cancelled in
    that window is already terminal, and an unguarded UPDATE paints
    "It is stalled" (answerer/record.py, assistant/progress.py) onto
    work that ended, with no edge left that would ever clear it. Every
    other `stall_reason` writer in the kernel scopes its UPDATE to a
    state -- `conductor/admit.py` to PENDING, `harness/intake.py` to
    RUNNING. Fails against the old module, whose UPDATE matched on id
    alone.
    """
    conn = _Conn()

    await envsetup.ensure_setup(conn, _row(tmp_path, BRIDGE_HOST))

    assert conn.stalls(), "the failure was not reported at all"
    assert "state = 'RUNNING'" in conn.stalls()[0], (
        "the stall note is written by execution id alone, so it can "
        "land after the run it describes has ended")
