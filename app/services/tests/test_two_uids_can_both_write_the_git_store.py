"""The workspace is written by two uids, so git must know that.

THE DESIGN THIS PROTECTS, which is deliberate and load-bearing:
`k8s_spec` runs the worker as 65531 and the sandbox as 65532, in one
shared group, so neither container owns the tree outright and each can
write what the other half made. `loop_progress` argues it at length and
ends "it is a two-uid fact rather than a file-mode preference: do not
'tidy' it away".

WHAT WAS MISSING. `podinit.compose` sets `umask(0o002)` so the clone
lands group-writable -- and that covers provisioning and nothing after
it. Every later git command runs under whatever umask its process has,
and the worker's is the default 0022. So a worker-side `story_commit`
creates `.git/objects/<xx>/` at 0755, owned by 65531, and the sandbox
cannot add the next object to it.

MEASURED ON RUN 264, where the executor spent five steps doing surgery
inside git's own object store to get a commit out -- copying a directory
aside, recreating it 775, copying the objects back. Run 262 had done a
cruder version (`chmod -R g+w .git`) and it was written off as
self-healing. It is not self-healing; it is a model repairing the
platform's permission model by hand, differently each time.
"""
from __future__ import annotations

import inspect
import subprocess
import tempfile
from pathlib import Path

import pytest

from kernel.workspace import bindings


def test_both_repository_paths_declare_the_shared_group():
    """Clone AND init. An `init` workspace is written by both
    containers too, and fixing only the branch that was measured is how
    the next one gets found in production."""
    src = inspect.getsource(bindings)
    assert src.count('"core.sharedRepository", "group"') == 2, (
        "one of the two repository-creating paths no longer marks the "
        "store as group-shared, so the uid that did not create an "
        "object directory cannot write into it")


def test_it_is_config_and_not_a_umask():
    """A umask belongs to whichever process happens to run; this is a
    property of the REPOSITORY, so it survives every later command in
    either container. `podinit` already proves the umask route covers
    provisioning and stops there."""
    src = inspect.getsource(bindings)
    assert "os.umask" not in src


@pytest.mark.skipif(not __import__("shutil").which("git"),
                    reason="git is not on PATH")
def test_git_actually_creates_a_group_writable_object_dir():
    """The behaviour, not the setting. `core.sharedRepository=group` is
    only worth setting if git honours it for the object directories a
    commit creates -- which is the exact thing that was shut."""
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", "-C", tmp, *a], capture_output=True, check=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        run("config", "core.sharedRepository", "group")
        Path(tmp, "f.txt").write_text("hello", encoding="utf-8")
        run("add", "f.txt")
        run("commit", "-q", "-m", "one")

        dirs = [p for p in Path(tmp, ".git", "objects").iterdir()
                if p.is_dir() and len(p.name) == 2]
        assert dirs, "the commit created no loose object directory"
        for d in dirs:
            mode = d.stat().st_mode & 0o777
            # 0o020 is group-write. On Windows the mode bits are not
            # meaningful, so this asserts only where they are.
            if hasattr(__import__("os"), "getuid"):
                assert mode & 0o020, f"{d.name} is {oct(mode)}, not group-writable"
