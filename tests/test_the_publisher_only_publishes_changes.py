"""`scripts/ci/publish_ledger_client.py` decides, unattended, whether a
build produces a new PyPI release. Two ways it can be wrong, and both
are silent.

PUBLISHES WHEN NOTHING CHANGED. A version on PyPI is burned forever —
not even a yank frees the number — so a comparison that reports
"changed" on every build mints an endless run of identical releases
that can never be reclaimed. The version line is the one byte that
differs between any two releases by construction, which is exactly why
it has to be normalised out before hashing. Forget that and the scheme
fails this way on its second build.

PUBLISHES NOTHING WHEN CODE CHANGED. The mirror image, and worse: the
build stays green while consumers keep resolving yesterday's code. That
is the shape of three separate bugs found in this repository in one
day, so it gets a test rather than a comment.

No network here. `content_hash` is pure, and these feed it dictionaries.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

publisher = pytest.importorskip(
    "publish_ledger_client", reason="scripts/ci not in this checkout")


def _pkg(version: str = "0.1.0", body: str = "x = 1\n") -> dict[str, bytes]:
    return {
        "__init__.py": f'__version__ = "{version}"\nfrom .j import x\n'.encode(),
        "j.py": body.encode(),
    }


def test_a_version_bump_alone_is_not_a_change():
    """The property the whole scheme rests on."""
    assert (publisher.content_hash(_pkg("0.1.4"))
            == publisher.content_hash(_pkg("0.1.99")))


def test_a_code_change_is_a_change():
    assert (publisher.content_hash(_pkg(body="x = 1\n"))
            != publisher.content_hash(_pkg(body="x = 2\n")))


def test_adding_a_file_is_a_change():
    a = _pkg()
    b = dict(a, new_module=b"print('hi')\n")
    assert publisher.content_hash(a) != publisher.content_hash(b)


def test_removing_a_file_is_a_change():
    a = _pkg()
    b = {k: v for k, v in a.items() if k != "j.py"}
    assert publisher.content_hash(a) != publisher.content_hash(b)


def test_moving_bytes_between_files_is_a_change():
    """Length-prefixing earns its place here.

    Concatenating contents without boundaries lets `ab` + `c` hash the
    same as `a` + `bc`, so a refactor that only moved code between
    modules would read as unchanged and never ship.
    """
    a = {"one.py": b"ab", "two.py": b"c"}
    b = {"one.py": b"a", "two.py": b"bc"}
    assert publisher.content_hash(a) != publisher.content_hash(b)


def test_compiled_artefacts_are_ignored():
    """A local run leaves __pycache__ behind; an installed wheel has
    none. Counting it would make every build after a local test session
    look changed."""
    a = _pkg()
    b = dict(a, **{"__pycache__/j.cpython-312.pyc": b"\x00\x01",
                   "j.pyc": b"\x00\x02"})
    assert publisher.content_hash(a) == publisher.content_hash(b)


def test_it_is_stable_across_dict_ordering():
    a = _pkg()
    b = dict(reversed(list(a.items())))
    assert publisher.content_hash(a) == publisher.content_hash(b)


def test_the_real_package_hashes_without_error():
    """The normaliser must cope with the actual __init__.py, which is
    not the two-line fixture above."""
    files = publisher.local_files()
    assert files, "the package source was not found"
    assert len(publisher.content_hash(files)) == 64
