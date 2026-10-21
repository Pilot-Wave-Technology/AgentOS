"""The sandbox can edit the tree the init container cloned for it.

MEASURED ON A LIVE POD, which is how this was found -- a story parked
on its first step and the executor asked a person to chmod a directory:

    /workspace        drwxrwsr-x  root  65532   <- writable
    repos/<repo>/     drwxr-sr-x  65531 65532   <- NOT writable
    README.md         -rw-r--r--  65531 65532   <- NOT writable

    sandbox (uid 65532):  new file in repo   REFUSED
                          edit existing      REFUSED
                          new file at root   OK

`fsGroup` on the pod chowns the volume to the shared group and makes it
group-writable AT MOUNT TIME. The compose init container clones
AFTERWARDS, as the worker's uid under the default umask, so every file
it writes is 0644 and every directory 0755. Owner 65531, group 65532,
sandbox 65532: read, no write.

WHY NO ERRAND EVER HIT IT. An errand writes its report at the workspace
ROOT, which fsGroup had already made group-writable. Errand 187 wrote a
7KB report and finished cleanly. A code story cannot edit one file.

AND THE REFUSAL POINTED AT A PERSON. The executor did the right thing
-- it cannot chmod a tree it does not own, so it parked and asked --
and what it asked for is something nobody can do from a chat card. A
loud correct refusal whose CALLER is the defect, which is the commonest
shape of bug in this system.
"""
from __future__ import annotations

import inspect
import os
import stat

import pytest

from kernel.workspace import podinit


def test_the_umask_lets_the_group_write():
    """Covers everything created from compose onward, including a
    checkout that happens after the clone."""
    src = inspect.getsource(podinit.compose)
    assert "umask(0o002)" in src, (
        "the init container clones under the default umask, so every "
        "file lands 0644 and the sandbox cannot edit it")


def test_what_the_clone_already_wrote_is_fixed_too():
    # The umask cannot retroactively help files git created before the
    # process reached our code. Neither half is sufficient alone.
    assert "_make_group_writable" in inspect.getsource(podinit.compose)


def test_it_runs_before_the_completeness_check_refuses():
    """Order matters: a workspace declared incomplete never reaches the
    chmod, and a pod that fails to start is not a pod with a read-only
    tree -- but if the check ever passes on a partial tree we want the
    permissions already right."""
    src = inspect.getsource(podinit.compose)
    assert src.index("_make_group_writable") < src.index("incomplete(root")


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX modes are not meaningful on Windows")
def test_it_actually_makes_a_tree_writable(tmp_path):
    """The behaviour, not the source. A guard that only greps for a
    function name passes for a function that does nothing."""
    repo = tmp_path / "repos" / "acme__thing"
    (repo / "src").mkdir(parents=True)
    plain = repo / "README.md"
    plain.write_text("x", encoding="utf-8")
    script = repo / "run.sh"
    script.write_text("#!/bin/sh", encoding="utf-8")
    os.chmod(plain, 0o644)
    os.chmod(script, 0o755)
    os.chmod(repo / "src", 0o755)

    podinit._make_group_writable(str(tmp_path))

    assert os.stat(plain).st_mode & stat.S_IWGRP, "a file stayed read-only"
    assert os.stat(repo / "src").st_mode & stat.S_IWGRP, "a dir stayed read-only"
    # THE SETGID BIT, so a file the sandbox creates later inherits the
    # shared group rather than its own -- otherwise the worker, which
    # commits, cannot write what the sandbox made and the same problem
    # reappears pointing the other way.
    assert os.stat(repo / "src").st_mode & stat.S_ISGID


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
def test_it_does_not_make_every_file_executable(tmp_path):
    """+x only where the owner has it. Marking a whole tree executable
    changes what it MEANS, and a checked-in script keeps its bit."""
    (tmp_path / "d").mkdir()
    plain = tmp_path / "d" / "notes.md"
    plain.write_text("x", encoding="utf-8")
    os.chmod(plain, 0o644)
    script = tmp_path / "d" / "go.sh"
    script.write_text("#!/bin/sh", encoding="utf-8")
    os.chmod(script, 0o755)

    podinit._make_group_writable(str(tmp_path))

    assert not os.stat(plain).st_mode & stat.S_IXGRP
    assert os.stat(script).st_mode & stat.S_IXGRP


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
def test_one_unchmodable_path_does_not_stop_the_rest(tmp_path, monkeypatch):
    """A degraded workspace beats no workspace: refusing to start the
    pod over a single path we cannot touch would turn a tree with one
    awkward file into no tree at all."""
    (tmp_path / "a").mkdir()
    good = tmp_path / "a" / "ok.txt"
    good.write_text("x", encoding="utf-8")
    os.chmod(good, 0o644)

    real = os.chmod
    calls = {"n": 0}

    def flaky(path, mode, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("nope")
        return real(path, mode, **kw)

    monkeypatch.setattr(os, "chmod", flaky)
    podinit._make_group_writable(str(tmp_path))     # must not raise
    monkeypatch.setattr(os, "chmod", real)
    assert os.stat(good).st_mode & stat.S_IWGRP
