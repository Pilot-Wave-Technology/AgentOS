"""A captured `.venv` must survive the extraction filter. Execution 209.

The capture finally worked end to end -- 45,864,960 bytes, twenty
packages installed, the marker parsed -- and the criterion Job then
refused to unpack it:

    fetch: could not unpack 1715409f6806 into /tmp:
    AbsoluteLinkError: 'verify/repos/<repo>/.venv/bin/python3' is a link
    to an absolute path

`kernel/artifacts/fetch.py` extracts with `filter="data"`, which refuses
absolute symlinks, and `python -m venv` ALWAYS writes `bin/python3` as a
link to the absolute path of its base interpreter. Those two facts
cannot both hold: no captured venv was ever extractable, so no story
with a manifest could reach a criterion.

THE FILTER IS NOT THE THING TO CHANGE. It refuses these for a good
reason -- the tar is ours but it is built FROM A REPOSITORY, and an
absolute link followed by a write through it is the classic escape from
an extraction root. Relaxing it would buy one case by weakening the
boundary against hostile content, on the one tar that carries hostile
content by design.

So the capture dereferences instead: the link is stored as the file it
points at. Same bytes the criterion needs, no absolute links in the
archive, filter untouched. It is also more portable -- a link to
`/usr/local/bin/python3` only resolves if the criterion's image carries
that interpreter at that path, and the CONTRACT picks that image.
"""
from __future__ import annotations

import io
import os
import tarfile

import pytest

from kernel.artifacts.capture import archive


def _venv_like(tmp_path):
    """A tree shaped like the one that failed: a venv beside a repo."""
    root = tmp_path / "verify"
    (root / "repo" / ".venv" / "bin").mkdir(parents=True)
    (root / "repo" / "api").mkdir()
    (root / "repo" / "api" / "main.py").write_text("app = 1\n")

    # The real interpreter, somewhere outside the captured tree -- which
    # is exactly where a venv's link points.
    real = tmp_path / "usr" / "local" / "bin"
    real.mkdir(parents=True)
    (real / "python3").write_bytes(b"#!/binary\n\x7fELF-ish")

    os.symlink(str(real / "python3"),
               str(root / "repo" / ".venv" / "bin" / "python3"))
    return root


def _members(blob):
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        return {m.name: m for m in tar.getmembers()}


# ── the regression ─────────────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_the_archive_carries_no_absolute_symlinks(tmp_path):
    """THE ONE THAT WOULD HAVE CAUGHT IT."""
    members = _members(archive(str(_venv_like(tmp_path))))
    offenders = [n for n, m in members.items()
                 if m.issym() and os.path.isabs(m.linkname)]
    assert offenders == [], f"data_filter will refuse these: {offenders}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_the_interpreter_arrives_as_a_real_file(tmp_path):
    """Dropping the link would extract cleanly and leave a venv with no
    interpreter -- a pass that cannot run wearing the shape of one that
    can."""
    members = _members(archive(str(_venv_like(tmp_path))))
    name = "verify/repo/.venv/bin/python3"
    assert name in members, "the interpreter vanished from the capture"
    assert members[name].isfile()
    assert members[name].size > 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_it_actually_extracts_under_the_data_filter(tmp_path):
    """The end-to-end claim, asserted against the real filter rather
    than against my reading of it."""
    blob = archive(str(_venv_like(tmp_path)))
    dest = tmp_path / "out"
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(str(dest), filter="data")      # must not raise
    landed = dest / "verify" / "repo" / ".venv" / "bin" / "python3"
    assert landed.is_file()
    assert landed.read_bytes().startswith(b"#!/binary")


# ── and what must not change ───────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_ordinary_files_are_untouched(tmp_path):
    members = _members(archive(str(_venv_like(tmp_path))))
    assert "verify/repo/api/main.py" in members


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_relative_link_inside_the_tree_stays_a_link(tmp_path):
    """`data` accepts these, and they are how a repository legitimately
    expresses "the same file twice". Dereferencing them would inflate
    every archive for no reason."""
    root = _venv_like(tmp_path)
    os.symlink("main.py", str(root / "repo" / "api" / "alias.py"))
    members = _members(archive(str(root)))
    assert members["verify/repo/api/alias.py"].issym()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_dangling_absolute_link_is_dropped_not_carried(tmp_path):
    """It was already broken in the container that made it; carrying it
    forward only moves the failure into the criterion."""
    root = _venv_like(tmp_path)
    os.symlink("/nonexistent/nowhere",
               str(root / "repo" / ".venv" / "bin" / "ghost"))
    members = _members(archive(str(root)))
    assert "verify/repo/.venv/bin/ghost" not in members


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_the_tar_is_still_rooted_at_the_basename(tmp_path):
    """Everything downstream reads these names -- `only_new` subtracts
    the slice's entries and `rebase_deps_tar` re-roots them, and both
    would silently produce nothing against a differently rooted tar."""
    members = _members(archive(str(_venv_like(tmp_path))))
    assert all(n == "verify" or n.startswith("verify/") for n in members)
