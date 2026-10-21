"""A sandbox file tool cannot address anything outside the workspace.

WHAT THIS IS GUARDING, in the order it was learned.

`_resolve` was `path if isabs(path) else join(workspace, path)` --
no containment at all. Over 22,500 file-tool calls on dev and next,
models pointed a tool outside the workspace 30 times: reading
`/opt/agentos-bridge/shim.py`, grepping `/opt`, listing the bridge's
own source. Every one landed somewhere small, so nothing broke.

Then execution 101 passed `path: ".."`. That resolves to `/`, `grep`
walked the container, and it read synthetic files by LINE -- some of
which have no newline, so one "line" grew until the 9GiB sandbox was
OOM-killed. The bridge shim lives in that container, so every later
tool call returned errno 99, and the model concluded the bridge needed
restarting and asked a person to do it. One unlucky path, and the pod
was gone.

THE ESCAPES WERE NOT ALL INNOCENT EITHER. exec 407 on next read
`/opt/agentos-bridge/client.py` during the run already on record for
hunting credentials when blocked. Containment closes that too.

Run against the real modules rather than a stand-in: they are flat
imports because the sandbox puts `kernel/bridge` on `sys.path`, and a
test that mocked that away would not be testing the thing that ships.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3] / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

tools_fs = pytest.importorskip("tools_fs")
tools_core = pytest.importorskip("tools_core")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("hello needle world\n")
    return str(tmp_path)


# ── what must be refused ─────────────────────────────────────────────

def test_the_call_that_killed_execution_101(ws):
    """`path: ".."` -- the exact arguments, from the step journal."""
    out = tools_fs.tool_grep({"path": "..", "pattern": "needle"}, ws)
    assert out["error_code"] == "E_OUTSIDE_SCOPE"
    assert out["exit_code"] == 1


def test_an_absolute_path_is_not_a_way_round(ws):
    """It was taken verbatim: `isabs(path)` short-circuited the join."""
    for tool, args in (
            (tools_fs.tool_grep, {"path": "/etc", "pattern": "x"}),
            (tools_fs.tool_glob, {"path": "/etc", "pattern": "*"}),
            (tools_fs.tool_list_dir, {"path": "/etc"}),
    ):
        assert tool(args, ws)["error_code"] == "E_OUTSIDE_SCOPE"


def test_the_platform_source_is_not_readable(ws):
    """exec 407's move, and exec 49's, and exec 35's. A sandbox has no
    business reading the shim that polices it."""
    tools_core.WORKSPACE = ws
    assert tools_core._tool_read(
        {"path": "/opt/agentos-bridge/shim.py"})["error_code"] == "E_OUTSIDE_SCOPE"
    assert tools_core._tool_write(
        {"path": "/opt/agentos-bridge/shim.py",
         "content": "x"})["error_code"] == "E_OUTSIDE_SCOPE"


def test_a_symlink_out_is_caught(ws):
    """Checked after resolution, not before. A `.venv` is full of links
    and a check on the spelling would be checking the wrong string."""
    link = os.path.join(ws, "escape")
    try:
        os.symlink(tempfile.gettempdir(), link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make the symlink")
    assert tools_fs.tool_list_dir(
        {"path": "escape"}, ws)["error_code"] == "E_OUTSIDE_SCOPE"


# ── what must keep working ───────────────────────────────────────────

def test_a_relative_path_inside_is_untouched(ws):
    out = tools_fs.tool_grep({"path": "sub", "pattern": "needle"}, ws)
    assert out["exit_code"] == 0
    assert "sub/a.py:1:hello needle world" in out["output"]


def test_an_absolute_path_INSIDE_the_workspace_still_works(ws):
    """Models write `/workspace/.agentos-out/job-xyz.log` and mean it --
    exec 426 on next did exactly that. The test is where the path
    LANDS, not how it was spelled, and refusing this would break
    something both common and correct."""
    tools_core.WORKSPACE = ws
    out = tools_core._tool_read({"path": os.path.join(ws, "sub", "a.py")})
    assert out["exit_code"] == 0
    assert "needle" in out["output"]


# ── and the reads are bounded, which containment alone does not give ──

def test_a_file_with_no_newline_does_not_eat_the_sandbox(ws):
    """The mechanism behind the OOM, reproduced inside the workspace
    where containment cannot help. Line iteration has no bound and a
    file with no newline is one line."""
    (pathlib.Path(ws) / "blob.bin").write_text("x" * (9 * 1024 * 1024))
    out = tools_fs.tool_grep({"path": "blob.bin", "pattern": "needle"}, ws)
    assert out["exit_code"] == 0
    assert out["error"] is None


def test_a_long_read_says_it_was_clipped(ws):
    """Everything else about `read` tells the model what it did not get.
    A byte cap that stayed quiet would read as the end of the file."""
    tools_core.WORKSPACE = ws
    big = pathlib.Path(ws) / "long.txt"
    big.write_text("line\n" * 2_000_000)          # ~10MB, over the cap
    out = tools_core._tool_read({"path": "long.txt", "limit": 5})
    assert out["exit_code"] == 0
    assert "MB of this file was read" in out["output"]


def test_the_walk_is_bounded_not_just_the_matches(ws):
    """`MAX_MATCHES` capped hits and bounded nothing about the walk: a
    pattern matching little walked everything under the root."""
    import inspect
    src = inspect.getsource(tools_fs.tool_grep)
    assert "MAX_FILES" in src
    assert "files_scanned" in src
