"""Guard the cycle-01 backfill migration against drift on two dimensions:

  1. The set of native-provider ids seeded into `model_providers` must
     equal `management.NATIVE_PROVIDERS` exactly.
  2. The `runtime_status` per row must match the design: all three
     seeded natives (anthropic, google, claude-cli) are 'provisioned'.

`openai` and `codex-cli` are deliberately NOT seeded/registered for
now — deferred, see `NATIVE_PROVIDERS`'s docstring in management.py and
`migrations/models_hierarchy_backfill.sql`'s header.

If a future story adds a new native provider to `NATIVE_PROVIDERS`
without extending the backfill INSERT (or vice versa), or if the
runtime_status assignment shifts without corresponding scope.md +
migration comment updates, this test fails loud rather than letting
the deploy silently drift.

Cycle-01 of story 414f92d6.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.model_registry import management as m
from app.services.tests.shipped_tree import IS_DISTRIBUTION, seed_text


# ---------------------------------------------------------------------------
# Fixture: locate + read the backfill migration.
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKFILL_PATH = _REPO_ROOT / "migrations" / "models_hierarchy_backfill.sql"


def _load_backfill_sql() -> str:
    assert _BACKFILL_PATH.exists(), (
        f"backfill migration missing at {_BACKFILL_PATH}"
    )
    return _BACKFILL_PATH.read_text()


# ---------------------------------------------------------------------------
# Parser: extract the INSERT INTO model_providers ... VALUES tuples.
#
# Deliberately narrow — we only need the (id, ..., runtime_status) pair
# from each row. Any row that fails to match is surfaced as a parse
# error so a schema drift (e.g. an added column) shows up as a test
# failure rather than a silent miss.
# ---------------------------------------------------------------------------


def _extract_provider_tuples(sql: str) -> dict[str, str]:
    """Return {provider_id: runtime_status} for every seeded row.

    Handles both API-tier rows (6 fields with base_url etc.) and CLI-tier
    rows (base_url/api_key_source NULL). The regex accepts either quoted
    strings or bare NULL in the middle fields and pins the first-position
    id and last-position runtime_status. (No `kind` field — that column
    was dropped entirely; every row is implicitly a real upstream now.)
    """
    # Isolate the INSERT INTO model_providers block ... ON CONFLICT.
    m_ins = re.search(
        r"INSERT\s+INTO\s+model_providers\s*\([^)]*\)\s*VALUES\s*(.*?)ON\s+CONFLICT",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert m_ins, "could not find INSERT INTO model_providers ... VALUES ... ON CONFLICT"
    values_block = m_ins.group(1)

    # Each row tuple is (...) up to the matching close-paren; the pattern
    # after the '(' contains 6 comma-separated fields terminating with
    # runtime_status. Middle fields may be single-quoted strings or NULL.
    field = r"(?:'(?:[^']|'')*'|NULL)"
    row_re = re.compile(
        r"\(\s*"
        r"'(?P<id>[^']+)'\s*,\s*"       # 1. id
        + field + r"\s*,\s*"             # 2. label
        + field + r"\s*,\s*"             # 3. base_url
        + field + r"\s*,\s*"             # 4. api_key_source
        + field + r"\s*,\s*"             # 5. api_type
        r"'(?P<rs>[^']+)'"               # 6. runtime_status
        r"\s*\)",
        re.DOTALL,
    )
    rows = {m.group("id"): m.group("rs") for m in row_re.finditer(values_block)}
    return rows


def _seeded_rows() -> dict[str, str]:
    """{provider_id: runtime_status} as a fresh install gets it.

    The distribution does not ship the backfill migration: its providers
    come from `seed/model_providers.json`, which `0002_baseline_seed.py`
    inserts verbatim -- so that file is the seed this guard pins there."""
    if IS_DISTRIBUTION:
        import json
        return {p["id"]: p.get("runtime_status", "provisioned")
                for p in json.loads(seed_text("model_providers.json"))}
    return _extract_provider_tuples(_load_backfill_sql())


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_backfill_migration_file_exists():
    if IS_DISTRIBUTION:
        assert _seeded_rows(), "seed/model_providers.json seeds no provider"
    else:
        _load_backfill_sql()


def test_seeded_ids_equal_native_providers_frozenset():
    """The provider-id set in the seed matches management.NATIVE_PROVIDERS."""
    rows = _seeded_rows()
    assert set(rows.keys()) == set(m.NATIVE_PROVIDERS), (
        f"drift: backfill seeds {sorted(rows)!r} but "
        f"NATIVE_PROVIDERS is {sorted(m.NATIVE_PROVIDERS)!r}"
    )


def test_seeded_row_count_is_exactly_three():
    """Structural sanity — 3 native providers, no more, no less.

    `openai`/`codex-cli` are deliberately deferred (not seeded) — see
    module docstring."""
    rows = _seeded_rows()
    assert len(rows) == 3, (
        f"expected 3 seeded provider rows, saw {len(rows)}: {sorted(rows)!r}"
    )


def test_all_seeded_providers_are_provisioned():
    """anthropic, google, claude-cli all have runtime infra wired."""
    rows = _seeded_rows()
    for pid in ("anthropic", "google", "claude-cli"):
        assert rows[pid] == "provisioned", (
            f"expected {pid}='provisioned', saw {rows[pid]!r}"
        )


def test_full_runtime_status_shape_pinned():
    """Combined assertion — one hop per (id, runtime_status) pair."""
    rows = _seeded_rows()
    expected = {
        "anthropic":  "provisioned",
        "google":     "provisioned",
        "claude-cli": "provisioned",
    }
    assert rows == expected, f"drift: {rows!r} != {expected!r}"
