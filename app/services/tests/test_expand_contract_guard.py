"""The expand/contract guard has to be able to fire, and has to not fire.

WHY A GUARD GETS ITS OWN TESTS HERE. The recurring defect in this
repository is not wrong logic, it is correct logic wired to nothing --
`reconcile_tick`, `probe_wedged`, `scrub`, `assert_no_credentials`, the E2
boundary test. A guard is the easiest thing in the world to write in a way
that never reports anything, and it looks identical to a clean repository
right up until the day it was supposed to catch something.

The other half matters just as much. A test in this repo once failed on
the comment DESCRIBING the anti-pattern it was looking for, and a guard
that fires on prose is one people learn to route around rather than fix.
So both directions are asserted: it catches the statement, and it ignores
the sentence about the statement.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
GUARD = ROOT / "scripts" / "check_expand_contract.py"


def _guard():
    spec = importlib.util.spec_from_file_location("_ec_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrations(tmp_path, files: dict[str, str]) -> pathlib.Path:
    d = tmp_path / "migrations"
    d.mkdir()
    io.open(d / "SEQUENCE", "w", encoding="utf-8").write(
        "\n".join(files) + "\n")
    for name, sql in files.items():
        io.open(d / f"{name}.sql", "w", encoding="utf-8").write(sql)
    return d


def test_it_catches_a_dropped_column(tmp_path):
    g = _guard()
    g.ACKNOWLEDGED = set()
    d = _migrations(tmp_path, {"m": "ALTER TABLE t DROP COLUMN old;"})
    _, findings = g.findings_for(d)
    assert len(findings) == 1 and "DROP COLUMN" in findings[0]


@pytest.mark.parametrize("sql,label", [
    ("DROP TABLE t;", "DROP TABLE"),
    ("ALTER TABLE t RENAME COLUMN a TO b;", "RENAME COLUMN"),
    ("ALTER TABLE t ALTER COLUMN c SET NOT NULL;", "SET NOT NULL"),
    ("DROP TYPE mood;", "DROP TYPE"),
])
def test_it_catches_the_other_rollback_hostile_shapes(tmp_path, sql, label):
    g = _guard()
    g.ACKNOWLEDGED = set()
    d = _migrations(tmp_path, {"m": sql})
    _, findings = g.findings_for(d)
    assert findings and label in findings[0], f"{label} not reported for {sql!r}"


def test_it_does_not_fire_on_prose(tmp_path):
    """Comments do not run."""
    g = _guard()
    g.ACKNOWLEDGED = set()
    d = _migrations(tmp_path, {"m": (
        "-- This does NOT DROP TABLE anything, and explains why you should\n"
        "-- not DROP COLUMN or RENAME COLUMN in one deploy.\n"
        "/* Nor in a block comment: DROP TYPE mood; */\n"
        "ALTER TABLE t ADD COLUMN new_thing TEXT;\n")})
    _, findings = g.findings_for(d)
    assert not findings, findings


def test_the_expand_half_is_never_flagged(tmp_path):
    """Adding is always safe to roll back over. If this ever fails, the
    guard has become something that blocks ordinary work."""
    g = _guard()
    g.ACKNOWLEDGED = set()
    d = _migrations(tmp_path, {"m": (
        "CREATE TABLE t (id INT);\n"
        "ALTER TABLE t ADD COLUMN c TEXT;\n"
        "CREATE INDEX idx ON t (c);\n"
        "INSERT INTO t (id) VALUES (1);\n")})
    _, findings = g.findings_for(d)
    assert not findings, findings


def test_the_real_migrations_pass(tmp_path):
    """The baseline is honest: the repository's own history is clean apart
    from the entries in ACKNOWLEDGED, each of which carries its reason
    there."""
    g = _guard()
    _, findings = g.findings_for(ROOT / "migrations")
    assert not findings, (
        "unacknowledged destructive DDL in migrations/:\n  "
        + "\n  ".join(findings))


def test_every_acknowledged_migration_still_exists():
    """An allowlist nobody prunes stops meaning anything -- and this test
    already caught one invented name before the guard ever shipped."""
    g = _guard()
    missing = sorted(n for n in g.ACKNOWLEDGED
                     if not (ROOT / "migrations" / f"{n}.sql").is_file())
    assert not missing, (
        f"{missing} are excused but no longer exist; remove them from "
        f"ACKNOWLEDGED in scripts/check_expand_contract.py")


def test_every_acknowledged_migration_is_actually_destructive():
    """The other direction: an entry that no longer needs excusing is
    noise, and noise is what makes an allowlist unreadable."""
    g = _guard()
    unnecessary = []
    for name in sorted(g.ACKNOWLEDGED):
        path = ROOT / "migrations" / f"{name}.sql"
        if not path.is_file():
            continue
        body = g.strip_sql_comments(
            io.open(path, encoding="utf-8", errors="replace").read())
        if not any(rx.search(body) for rx in g.DESTRUCTIVE.values()):
            unnecessary.append(name)
    assert not unnecessary, (
        f"{unnecessary} are acknowledged but contain no destructive DDL")
