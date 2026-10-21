"""pytest plugin: leave out the tests listed in release_deselect.txt.

Loaded from pytest.ini (`-p release_deselect`), so it applies however the
published suite is run -- the build, scripts/ci/run_suite.sh, or a plain
`pytest`. The list says why each test is not run here; read it first.

DESELECTED, NOT SKIPPED. A skip is a test that could not run; these are
tests about files this distribution does not ship, and counting them as
skips would spend the runner's skip budget on something that is not a
skip. pytest reports them as "deselected", by number.

A LISTED ID THAT NO LONGER EXISTS IS AN ERROR when the whole suite runs.
A rename upstream would otherwise leave a stale line here and silently
run nothing -- or worse, a renamed copy of a test that should have stayed
out. On a partial run (a path on the command line) it cannot tell, so it
only checks what it collected.
"""
from __future__ import annotations

import pathlib

import pytest

LIST = pathlib.Path(__file__).resolve().parent / "release_deselect.txt"


def _listed() -> set[str]:
    if not LIST.exists():
        return set()
    return {line.strip() for line in LIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def pytest_collection_modifyitems(session, config, items):
    listed = _listed()
    if not listed:
        return
    keep, drop = [], []
    for item in items:
        (drop if item.nodeid in listed else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep

    # Stale entries, only on a full run: no explicit paths were given.
    if not config.args or config.args == list(config.getini("testpaths")):
        stale = listed - {i.nodeid for i in drop}
        if stale:
            raise pytest.UsageError(
                "release_deselect.txt lists tests that do not exist (renamed "
                "or removed upstream?) -- fix the list:\n  "
                + "\n  ".join(sorted(stale)))
