"""Which tree is this suite running in -- the repository, or the published
distribution -- and what does the distribution's schema say?

WHY THIS EXISTS. The same suite runs in two places. In this repository a
schema change is an individual migration (`migrations/<name>.sql`, listed
in SEQUENCE), and a test may read that file to prove the change exists.
The published distribution squashes every migration into one baseline,
`migrations/0001_baseline_schema.sql`; the individual files are not there.

A test that checks a PRODUCT FACT -- this column exists, this constraint
allows that value, this role may read that table -- must hold in both. So
it checks the migration here and the baseline there, through this module:

    from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_schema

    if IS_DISTRIBUTION:
        assert table_has_column("stories", "short_title")
    else:
        ...the original assertion on the migration text...

A test that checks only how an internal file is WRITTEN (a migration's
SQL style, our environment build files) is not rewritten; the
distribution lists it in release_deselect.txt with the file it needs.

Not a test module (no `test_` prefix), so pytest does not collect it.
"""
from __future__ import annotations

import functools
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "migrations"
BASELINE = MIGRATIONS / "0001_baseline_schema.sql"

# The distribution ships the squashed baseline; this repository does not.
IS_DISTRIBUTION = BASELINE.exists()


@functools.lru_cache(maxsize=1)
def baseline_schema() -> str:
    """The distribution's whole schema (pg_dump --schema-only)."""
    return BASELINE.read_text(encoding="utf-8")


def migration_text(name: str) -> str | None:
    """`migrations/<name>.sql` if this tree has it, else None."""
    p = MIGRATIONS / f"{name}.sql"
    return p.read_text(encoding="utf-8") if p.exists() else None


def create_table(table: str) -> str:
    """The baseline's CREATE TABLE statement for `schema.table` (public by
    default). Raises AssertionError naming the table if it is absent."""
    qualified = table if "." in table else f"public.{table}"
    m = re.search(r"CREATE TABLE " + re.escape(qualified) + r" \((.*?)\n\);",
                  baseline_schema(), re.DOTALL)
    assert m, f"{qualified} is not in the baseline schema"
    return m.group(1)


def table_has_column(table: str, column: str) -> bool:
    body = create_table(table)
    return re.search(r"^\s+" + re.escape(column) + r"\s", body, re.MULTILINE) is not None


def baseline_mentions(pattern: str) -> bool:
    """True if a regex matches anywhere in the baseline schema -- for an
    index, a constraint, a function or a trigger by name."""
    return re.search(pattern, baseline_schema(), re.DOTALL) is not None


@functools.lru_cache(maxsize=1)
def shipped_grants() -> str:
    """Every grant the distribution's migrations make (0003 onward)."""
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(MIGRATIONS.glob("000[3-9]_*.sql")))


# ── the seed tree: what 0002 loads into a fresh install ─────────────────
#
# Data migrations (a template's prompt, its model, a skill, a model row)
# are not in the schema dump. The distribution ships them as files under
# `seed/`, which `0002_baseline_seed.py` inserts verbatim -- so a fact a
# migration WROTE is checked there.

SEED = ROOT / "seed"


def seed_text(rel: str) -> str:
    """`seed/<rel>` as text. Raises AssertionError naming it if absent."""
    p = SEED / rel
    assert p.is_file(), f"seed/{rel} is not in the distribution"
    return p.read_text(encoding="utf-8")


def seed_template(template_id: str) -> dict:
    """A seeded template's meta.json (model, tools_allow, ...)."""
    import json
    return json.loads(seed_text(f"templates/{template_id}/meta.json"))


def seed_model(model_id: str) -> dict:
    """A row of seed/models.json by id. Raises AssertionError if absent."""
    import json
    rows = [m for m in json.loads(seed_text("models.json")) if m["id"] == model_id]
    assert rows, f"{model_id} is not in seed/models.json"
    return rows[0]


def shipped_privileges(obj: str, role: str) -> set:
    """The privileges the distribution grants `role` on `obj` (a
    schema-qualified table or sequence), e.g. {"SELECT", "INSERT"}.
    Empty if nothing is granted."""
    privs = set()
    for m in re.finditer(r"^GRANT (.+?) ON (?:TABLE |SEQUENCE )?(\S+) TO (\w+);",
                         shipped_grants(), re.MULTILINE):
        if m.group(2) == obj and m.group(3) == role:
            privs |= {p.strip() for p in m.group(1).split(",")}
    return privs
