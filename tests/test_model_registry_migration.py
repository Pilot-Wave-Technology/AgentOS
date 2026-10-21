"""Unit tests for the Phase A3 model-registry data migration.

Story b134ed98 / cycle-02. Covers the seven test buckets required by
the plan's T4 task:

  1. Resolution        — alias/canonical lookup produces expected wire_id.
  2. Canonicalisation  — SET model = model_id is a no-op on canonical
                         inputs and idempotent on re-apply.
  3. Skew classifier   — only_in_db / only_on_disk / metadata_diff.
  4. Bootstrap fallback- load_models returns SUPPORTED_MODELS on empty
                         DB and on DB exception, with the right log level.
  5. Alias-coverage    — every raw string enumerated in the Phase A0
                         recon report appears in model_aliases_seed.
  6. Idempotence       — resolve+rewrite applied twice produces no diff.
  7. Provider-id       — every models_seed.sql row's provider_id equals
     consistency         its SUPPORTED_MODELS[i]['provider'] AND is
                         either in model_provider_registry_seed.sql or
                         in the native-provider allowlist. This is the
                         FK-substitute assertion under Option A.

No live database is required. The SQL seed files are parsed with
regexes into in-memory fixtures; the resolver logic is mirrored in
pure Python (`_resolve`, `_canonicalise`) and exercised against those
fixtures. Bootstrap-fallback tests replace asyncpg.Pool.fetch with a
tiny stub that either returns [] or raises.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from app.llm_models import SUPPORTED_MODELS
from app.llm_models_bootstrap import (
    SkewReport,
    check_skew,
    load_models,
)
from app.services.model_registry.management import NATIVE_PROVIDERS
from app.services.tests.shipped_tree import IS_DISTRIBUTION

# ── Repo paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"
_RECON_REPORT = _REPO_ROOT / "documentation" / "model_registry_inventory.md"


# ── Expected alias mapping (hand-transcribed from Phase A0 recon) ────
#
# Each entry corresponds to a raw string enumerated in
# repo/documentation/model_registry_inventory.md that the T2 backfill
# must resolve to a canonical `provider/model` wire_id. Keeping this
# table in the test module lets tests fail loudly when the seed drifts
# from the recon report; a follow-up test asserts the recon report
# still literally mentions every raw string listed here.
_EXPECTED_ALIAS_MAP: dict[str, str] = {
    # §2.6 claude_phases.model — bare Claude CLI shortnames.
    "sonnet":            "anthropic/claude-sonnet-4-5",
    "haiku":             "anthropic/claude-haiku-4-5",
    # §4.5 normalize_model_id — bare unprefixed native names.
    "gemini-2.5-flash":  "google/gemini-2.5-flash",
    "gemini-2.5-pro":    "google/gemini-2.5-pro",
    "claude-haiku-4-5":  "anthropic/claude-haiku-4-5",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
    "claude-opus-4-7":   "anthropic/claude-opus-4-7",
    # cycle-02 plan spec: defensive legacy short-form.
    "sonnet-4":          "anthropic/claude-sonnet-4-5",
}

# Native provider prefixes intentionally excluded from
# model_provider_registry per Phase A1 design. The provider-id
# consistency test accepts these as valid provider_id values on top
# of whatever the synthetic-provider seed contains. The canonical set
# is imported from `app.services.model_registry.management` — this
# test consumes the same source of truth every other consumer uses.


# ── SQL parsing helpers ──────────────────────────────────────────────

def _read_migration(name: str) -> str:
    """Return the text of migrations/<name>.sql."""
    return (_MIGRATIONS_DIR / f"{name}.sql").read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- ...` line comments so INSERT-tuple regexes ignore
    example code inside header docstrings."""
    return re.sub(r"--[^\n]*", "", sql)


_MODEL_TUPLE_RE = re.compile(
    r"""\(
        \s*'([^']+)'\s*,       # id
        \s*'([^']+)'\s*,       # provider_id
        \s*'([^']+)'\s*,       # model_name
        \s*'([^']+)'\s*,       # label
        \s*'([^']+)'\s*,       # tier
        \s*(\d+)\s*            # context_window
        \)""",
    re.VERBOSE | re.DOTALL,
)


def _parse_models_seed() -> list[dict]:
    """Extract seeded model rows from models_seed.sql.

    Returns a list of dicts with keys `id`, `provider_id`,
    `model_name`, `label`, `tier`, `context_window`, and the derived
    `wire_id` (provider_id || '/' || model_name)."""
    sql = _strip_sql_comments(_read_migration("models_seed"))
    rows: list[dict] = []
    for m in _MODEL_TUPLE_RE.finditer(sql):
        model_id, provider_id, model_name, label, tier, cw = m.groups()
        rows.append({
            "id": model_id,
            "provider_id": provider_id,
            "model_name": model_name,
            "label": label,
            "tier": tier,
            "context_window": int(cw),
            "wire_id": f"{provider_id}/{model_name}",
        })
    return rows


_ALIAS_TUPLE_RE = re.compile(
    r"""\(
        \s*'([^']*)'\s*,       # alias (may be non-empty for our seed)
        \s*'([^']+)'\s*,       # model_id (target wire_id)
        \s*'([^']+)'\s*        # source
        \)""",
    re.VERBOSE,
)


def _parse_model_aliases_seed() -> dict[str, str]:
    """Extract {alias → model_id (wire_id)} from model_aliases_seed.sql."""
    sql = _strip_sql_comments(_read_migration("model_aliases_seed"))
    return {m.group(1): m.group(2) for m in _ALIAS_TUPLE_RE.finditer(sql)}


# The synthetic-provider seed used to be parsed here. `model_provider_
# registry` was Family A -- superseded, zero live callers -- and e1a323cc
# removed the table and its seed migration outright. The parser outlived
# the file it read and raised FileNotFoundError at fixture setup, which
# pytest reports as an ERROR rather than a failure, so the test below
# never ran again. NATIVE_PROVIDERS is now the whole allowlist, and the
# database agrees: every models.provider_id is one of its three entries.


# ── Pure-Python mirror of the T2 backfill SQL ────────────────────────

def _resolve(
    model_str: str,
    seed_models: list[dict],
    seed_aliases: dict[str, str],
) -> str | None:
    """Mirror of the T2 backfill's Step 1 COALESCE.

      COALESCE(
        (SELECT wire_id  FROM models        WHERE wire_id = t.model),
        (SELECT model_id FROM model_aliases WHERE alias   = t.model)
      )

    Direct wire_id match wins over alias-table lookup."""
    wire_ids = {m["wire_id"] for m in seed_models}
    if model_str in wire_ids:
        return model_str
    return seed_aliases.get(model_str)


def _canonicalise(row_model: str, resolved_id: str | None) -> str:
    """Mirror of the T2 backfill's Step 2:

      UPDATE t SET model = model_id
       WHERE model_id IS NOT NULL AND model <> model_id;

    Returns the new value of the `model` column for one row."""
    if resolved_id is None:
        return row_model
    if row_model != resolved_id:
        return resolved_id
    return row_model


# ── Test fixtures ────────────────────────────────────────────────────

def _shipped_models_seed() -> list[dict]:
    """The distribution's model seed: `seed/models.json`, loaded by
    `0002_baseline_seed.py` in place of the squashed models_seed.sql.
    Same row shape, with `wire_id` derived the way the column is."""
    rows = json.loads((_REPO_ROOT / "seed" / "models.json")
                      .read_text(encoding="utf-8"))
    for r in rows:
        r["wire_id"] = f"{r['provider_id']}/{r['model_name']}"
    return rows


@pytest.fixture(scope="module")
def seed_models() -> list[dict]:
    if IS_DISTRIBUTION:
        # A curated set, far smaller than the frozen seed; it must still
        # be non-empty and every row well-formed.
        rows = _shipped_models_seed()
        assert len(rows) >= 2, f"shipped seed has only {len(rows)} rows"
        assert all(r["id"] == r["wire_id"] for r in rows), rows
        return rows
    rows = _parse_models_seed()
    # Sanity: parser found at least the five native rows + several
    # synthetic rows. Guards against a regex silently failing.
    assert len(rows) >= 10, f"parser returned only {len(rows)} rows"
    return rows


@pytest.fixture(scope="module")
def seed_aliases() -> dict[str, str]:
    if IS_DISTRIBUTION:
        # The distribution's seed loader fills no model_aliases rows, so
        # resolution there is direct wire_id match only. Proven, not assumed.
        loader = (_MIGRATIONS_DIR / "0002_baseline_seed.py").read_text(
            encoding="utf-8")
        assert "INSERT INTO model_aliases" not in loader
        return {}
    aliases = _parse_model_aliases_seed()
    assert aliases, "alias-seed parser returned no rows"
    return aliases


class _FakePool:
    """Minimal asyncpg.Pool stub. Its `fetch` returns predetermined
    rows, or raises a predetermined exception if given one."""

    def __init__(self, rows_or_exc):
        self._rows_or_exc = rows_or_exc

    async def fetch(self, *args, **kwargs):
        if isinstance(self._rows_or_exc, BaseException):
            raise self._rows_or_exc
        return list(self._rows_or_exc)


# ══════════════════════════════════════════════════════════════════════
# Bucket 1 — Resolution
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "raw,expected_wire_id",
    list(_EXPECTED_ALIAS_MAP.items()),
    ids=list(_EXPECTED_ALIAS_MAP),
)
def test_alias_resolves_to_expected_wire_id(
    raw, expected_wire_id, seed_models, seed_aliases,
):
    """Every recon-report raw string resolves to its canonical wire_id."""
    resolved = _resolve(raw, seed_models, seed_aliases)
    assert resolved == expected_wire_id, (
        f"alias {raw!r}: expected {expected_wire_id!r}, got {resolved!r}"
    )


def test_canonical_strings_resolve_to_themselves(seed_models, seed_aliases):
    """A raw string that already equals a wire_id resolves to itself
    via the direct-match branch of COALESCE."""
    for m in seed_models:
        wid = m["wire_id"]
        assert _resolve(wid, seed_models, seed_aliases) == wid


def test_direct_match_wins_over_alias(seed_models):
    """If a raw string somehow existed in both models.wire_id and
    model_aliases.alias, the direct wire_id match must win (mirrors
    the COALESCE ordering in the T2 SQL)."""
    # Craft a fake alias that points at a DIFFERENT wire_id than the
    # raw string's own wire_id, and confirm the resolver prefers the
    # direct match.
    any_two = [m["wire_id"] for m in seed_models][:2]
    assert len(any_two) == 2, "need at least two seeded models"
    wid_a, wid_b = any_two
    misleading_aliases = {wid_a: wid_b}  # would send wid_a → wid_b
    assert _resolve(wid_a, seed_models, misleading_aliases) == wid_a


def test_unresolvable_returns_none(seed_models, seed_aliases):
    """A raw string that is neither a wire_id nor an alias resolves
    to None — the T2 backfill leaves model_id NULL for these rows and
    RAISEs NOTICE at migration time."""
    assert _resolve("", seed_models, seed_aliases) is None
    assert _resolve("no-such-model", seed_models, seed_aliases) is None


# ══════════════════════════════════════════════════════════════════════
# Bucket 2 — Canonicalisation transform
# ══════════════════════════════════════════════════════════════════════

def test_canonicalisation_noop_on_canonical(seed_models):
    """Applying the rewrite to an already-canonical row is a no-op."""
    for m in seed_models:
        wid = m["wire_id"]
        assert _canonicalise(wid, wid) == wid


def test_canonicalisation_rewrites_alias_to_canonical():
    """A row whose legacy model differs from its resolved wire_id is
    rewritten to the wire_id."""
    assert _canonicalise("sonnet", "anthropic/claude-sonnet-4-5") == (
        "anthropic/claude-sonnet-4-5"
    )


def test_canonicalisation_idempotent_on_reapply():
    """Re-applying the rewrite after canonicalisation is a no-op."""
    first_pass = _canonicalise("sonnet", "anthropic/claude-sonnet-4-5")
    second_pass = _canonicalise(first_pass, "anthropic/claude-sonnet-4-5")
    assert first_pass == second_pass == "anthropic/claude-sonnet-4-5"


def test_canonicalisation_unresolved_stays(seed_models):
    """Rows with resolved_id=None (unresolvable raw strings) keep
    their original model value untouched."""
    assert _canonicalise("", None) == ""
    assert _canonicalise("some-legacy", None) == "some-legacy"


# ══════════════════════════════════════════════════════════════════════
# Bucket 3 — Skew classifier
# ══════════════════════════════════════════════════════════════════════

def _row(wire_id: str, **extra) -> dict:
    """Build a DB-shaped row for the skew classifier tests."""
    base = {
        "id": wire_id,
        "wire_id": wire_id,
        "label": f"label-{wire_id}",
        "provider": wire_id.split("/", 1)[0] if "/" in wire_id else wire_id,
        "tier": "test",
    }
    base.update(extra)
    return base


def _entry(wire_id: str, **extra) -> dict:
    """Build a SUPPORTED_MODELS-shaped entry for the on-disk side."""
    base = {
        "id": wire_id,
        "label": f"label-{wire_id}",
        "provider": wire_id.split("/", 1)[0] if "/" in wire_id else wire_id,
        "tier": "test",
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_skew_reports_only_in_db():
    """Wire_ids present in the DB but absent from on_disk go into only_in_db."""
    db_rows = [_row("a/x"), _row("b/y"), _row("c/z")]
    on_disk = [_entry("a/x"), _entry("b/y")]
    pool = _FakePool(db_rows)
    report = await check_skew(pool, on_disk)
    assert report.only_in_db == ["c/z"]
    assert report.only_on_disk == []
    assert report.metadata_diff == []


@pytest.mark.asyncio
async def test_skew_reports_only_on_disk():
    """Wire_ids present in on_disk but absent from the DB go into only_on_disk."""
    db_rows = [_row("a/x"), _row("b/y")]
    on_disk = [_entry("a/x"), _entry("b/y"), _entry("c/z")]
    pool = _FakePool(db_rows)
    report = await check_skew(pool, on_disk)
    assert report.only_on_disk == ["c/z"]
    assert report.only_in_db == []
    assert report.metadata_diff == []


@pytest.mark.asyncio
async def test_skew_reports_metadata_diff():
    """Wire_ids present in both with a differing comparable field
    surface in metadata_diff with a per-field {db, on_disk} record."""
    db_rows = [_row("a/x", label="db-label"), _row("b/y")]
    on_disk = [_entry("a/x", label="disk-label"), _entry("b/y")]
    pool = _FakePool(db_rows)
    report = await check_skew(pool, on_disk)
    assert report.only_in_db == []
    assert report.only_on_disk == []
    assert len(report.metadata_diff) == 1
    wid, diff = report.metadata_diff[0]
    assert wid == "a/x"
    assert diff == {"label": {"db": "db-label", "on_disk": "disk-label"}}


@pytest.mark.asyncio
async def test_skew_empty_when_aligned():
    """Identical DB and on_disk sets produce an empty report in every bucket."""
    rows = [_row("a/x"), _row("b/y")]
    entries = [_entry("a/x"), _entry("b/y")]
    pool = _FakePool(rows)
    report = await check_skew(pool, entries)
    assert isinstance(report, SkewReport)
    assert report.only_in_db == []
    assert report.only_on_disk == []
    assert report.metadata_diff == []


# ══════════════════════════════════════════════════════════════════════
# Bucket 4 — Bootstrap fallback
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_load_models_empty_returns_supported_and_warns(caplog):
    """An empty DB result triggers the WARNING-log fallback path;
    load_models returns SUPPORTED_MODELS unchanged."""
    caplog.set_level(logging.WARNING, logger="app.llm_models_bootstrap")
    pool = _FakePool([])
    result = await load_models(pool)
    assert result == list(SUPPORTED_MODELS)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING log record"
    assert any("no active rows" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_load_models_exception_returns_supported_and_errors(caplog):
    """A DB exception triggers the ERROR-log fallback path with exc_info;
    load_models still returns SUPPORTED_MODELS and never raises."""
    caplog.set_level(logging.ERROR, logger="app.llm_models_bootstrap")
    pool = _FakePool(RuntimeError("simulated DB down"))
    result = await load_models(pool)
    assert result == list(SUPPORTED_MODELS)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected at least one ERROR log record"
    assert any("DB query failed" in r.getMessage() for r in errors)
    # exc_info must be attached so operators see the underlying trace.
    assert any(r.exc_info is not None for r in errors)


@pytest.mark.asyncio
async def test_check_skew_on_db_exception_is_empty_report(caplog):
    """When load_models falls back to SUPPORTED_MODELS on a DB error,
    check_skew's diff is (SUPPORTED_MODELS vs SUPPORTED_MODELS) = empty."""
    caplog.set_level(logging.ERROR, logger="app.llm_models_bootstrap")
    pool = _FakePool(RuntimeError("simulated"))
    report = await check_skew(pool, list(SUPPORTED_MODELS))
    assert report.only_in_db == []
    assert report.only_on_disk == []
    assert report.metadata_diff == []


# ══════════════════════════════════════════════════════════════════════
# Bucket 5 — Alias-coverage regression guard
# ══════════════════════════════════════════════════════════════════════

def test_alias_seed_covers_every_expected_raw_string(seed_aliases):
    """Every alias in the hand-transcribed recon table appears in the
    seed file with the expected canonical target."""
    missing = {
        alias: target
        for alias, target in _EXPECTED_ALIAS_MAP.items()
        if seed_aliases.get(alias) != target
    }
    assert not missing, (
        f"model_aliases_seed drift vs recon table: {missing!r}\n"
        f"seed contents: {seed_aliases!r}"
    )


def test_alias_seed_has_no_unexpected_entries(seed_aliases):
    """Reverse guard: the seed contains no aliases the test table
    doesn't know about. Prevents silent drift where the seed grows
    but this test's expected-map does not."""
    unexpected = set(seed_aliases) - set(_EXPECTED_ALIAS_MAP)
    assert not unexpected, (
        f"model_aliases_seed has aliases not in the test's expected map: "
        f"{sorted(unexpected)!r}. If these are intentional, add them to "
        f"_EXPECTED_ALIAS_MAP with the expected wire_id."
    )


def test_expected_aliases_are_documented_in_recon_report():
    """Every raw string in the expected-map appears literally in the
    recon report or is a valid derivation from a documented rule. This
    protects against transcription errors between the seed and the
    Phase A0 inventory."""
    recon_text = _RECON_REPORT.read_text(encoding="utf-8")
    # 'sonnet' and 'haiku' are named literally in §2.6.
    for literal in ("sonnet", "haiku"):
        assert literal in recon_text, (
            f"recon report no longer mentions {literal!r}"
        )
    # The unprefixed-name rules (§4.5) are described by regex, not by
    # every literal string. It is enough that normalize_model_id's
    # prefix rules for gemini-, claude- are documented.
    for rule_marker in ("normalize_model_id", "gemini-", "claude-"):
        assert rule_marker in recon_text, (
            f"recon report no longer describes rule {rule_marker!r}"
        )


def test_every_alias_target_is_a_seeded_wire_id(seed_aliases, seed_models):
    """FK integrity is enforced by the SQL layer at migration time;
    this test catches it at seed-authoring time so a bad alias would
    fail here before ever reaching Postgres."""
    known_wire_ids = {m["wire_id"] for m in seed_models}
    orphans = {
        alias: target
        for alias, target in seed_aliases.items()
        if target not in known_wire_ids
    }
    assert not orphans, f"alias target(s) not in models_seed: {orphans!r}"


# ══════════════════════════════════════════════════════════════════════
# Bucket 6 — Idempotence of the resolver + rewrite
# ══════════════════════════════════════════════════════════════════════

def test_backfill_is_idempotent_on_reapply(seed_models, seed_aliases):
    """Running the full resolve+rewrite twice against the same fixture
    set produces no change on the second pass — mirrors the WHERE
    clauses in the T2 SQL (model_id IS NULL / model <> model_id)."""
    # Sample fixture rows: one already-canonical, one alias, one
    # unresolvable. Emulates the shape of a real user-table row.
    fixture_rows: list[dict] = [
        {"model": "anthropic/claude-sonnet-4-5", "model_id": None},
        {"model": "sonnet",                      "model_id": None},
        {"model": "",                            "model_id": None},
    ]

    def _apply_backfill(rows: list[dict]) -> None:
        # Step 1: resolve where model_id IS NULL.
        for row in rows:
            if row["model_id"] is None:
                row["model_id"] = _resolve(row["model"], seed_models, seed_aliases)
        # Step 2: canonicalise legacy model column where model_id IS
        # NOT NULL and model <> model_id.
        for row in rows:
            row["model"] = _canonicalise(row["model"], row["model_id"])

    _apply_backfill(fixture_rows)
    snapshot_after_first = [dict(r) for r in fixture_rows]

    _apply_backfill(fixture_rows)  # re-apply
    snapshot_after_second = [dict(r) for r in fixture_rows]

    assert snapshot_after_second == snapshot_after_first, (
        f"backfill is NOT idempotent: first={snapshot_after_first!r} "
        f"second={snapshot_after_second!r}"
    )

    # Sanity: alias row resolved and was rewritten; unresolvable stayed.
    canonical_sonnet = "anthropic/claude-sonnet-4-5"
    assert fixture_rows[0]["model"] == canonical_sonnet
    assert fixture_rows[0]["model_id"] == canonical_sonnet
    assert fixture_rows[1]["model"] == canonical_sonnet
    assert fixture_rows[1]["model_id"] == canonical_sonnet
    assert fixture_rows[2]["model"] == ""
    assert fixture_rows[2]["model_id"] is None


# ══════════════════════════════════════════════════════════════════════
# Bucket 7 — Provider-id consistency (Option A FK substitute)
# ══════════════════════════════════════════════════════════════════════

# RETIRED FROM THE CATALOGUE, STILL IN THE FROZEN SEED.
#
# The mirror of POST_FREEZE_MODELS below. The pre-3-series Gemini ids
# left `SUPPORTED_MODELS` on 2026-10-18 so that nothing can SELECT one
# -- no dropdown, no new template, no new agent. Their rows stay in
# `models_seed.sql` because that migration is applied and editing it
# changes nothing, and because an agent pinned to one before that date
# keeps running: the row is what its `model_id` FK still points at.
#
# So the seed legitimately holds more than the catalogue offers, and
# these three guards have to say which direction they police. Adding an
# id here is a decision to stop OFFERING a model, never a decision to
# break one already in use.
RETIRED_FROM_CATALOGUE = {
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "google-flash-50k/gemini-2.5-flash-50k",
    "google-flash-100k/gemini-2.5-flash-100k",
    "google-flash-200k/gemini-2.5-flash-200k",
    "google-flash-500k/gemini-2.5-flash-500k",
    "google-flash-1m/gemini-2.5-flash-1m",
    "google-pro-100k/gemini-2.5-pro-100k",
    "google-pro-200k/gemini-2.5-pro-200k",
    "google-pro-500k/gemini-2.5-pro-500k",
    "google-pro-1m/gemini-2.5-pro-1m",
}


def test_every_seed_provider_id_matches_supported_models(seed_models):
    """Each row seeded by models_seed.sql carries the same provider_id
    as the corresponding entry in SUPPORTED_MODELS. This is the
    seed-vs-code drift guard called out in the cycle-02 plan."""
    supported_by_id: dict[str, dict] = {m["id"]: m for m in SUPPORTED_MODELS}
    mismatches: list[str] = []
    for row in seed_models:
        if row["id"] in RETIRED_FROM_CATALOGUE:
            continue
        supported = supported_by_id.get(row["id"])
        if supported is None:
            mismatches.append(
                f"{row['id']} seeded but not in SUPPORTED_MODELS"
            )
            continue
        if row["provider_id"] != supported["provider"]:
            mismatches.append(
                f"{row['id']}: seed provider_id={row['provider_id']!r} "
                f"vs SUPPORTED_MODELS provider={supported['provider']!r}"
            )
    assert not mismatches, "provider drift:\n  " + "\n  ".join(mismatches)


def test_every_seed_row_is_covered_by_supported_models(seed_models):
    """Every seeded row corresponds to a SUPPORTED_MODELS entry — no
    orphan rows sneaking into the seed."""
    supported_ids = {m["id"] for m in SUPPORTED_MODELS}
    orphans = [r["id"] for r in seed_models
               if r["id"] not in supported_ids
               and r["id"] not in RETIRED_FROM_CATALOGUE]
    assert not orphans, f"seeded rows not in SUPPORTED_MODELS: {orphans!r}"


# ADDED AFTER THE SEED WAS FROZEN, and they cannot be added to it.
#
# `models_seed.sql` is an applied migration: apply.py records what it ran
# and never re-runs it, so editing the file changes nothing anywhere. The
# usual answer -- a new migration -- is wrong here too, because the rows
# it would add use the synthetic `<owner>-<shorthand>-<size>` provider
# scheme that `models_hierarchy_backfill.sql` deliberately retired. A new
# migration would resurrect the dead shape to satisfy a test.
#
# So the correspondence this guard checks is between two artefacts that
# have both stopped moving, and an entry added to app/llm_models.py after
# the backfill can only ever be listed here. It stays a guard for
# everything else: anything NEW that goes missing still fails.
#
# What this does NOT mean is that the model is unreachable -- the live
# registry serves it natively as google/gemini-3.7-flash, which is why
# nothing broke when DEFAULT_GO_MODEL was pointed at it.
#
# The 3.8 entries joined it on 2026-10-18 for the same reason, and one
# more: `DEFAULT_GO_MODEL` and `DEFAULT_SHCA_MODEL` had pointed at
# `google-flash38-500k/gemini-3.8-flash-500k` since 2026-09-24 while no
# catalogue entry existed for it at all — so every new agent ran on a
# model the dropdown could not show. Listing the variants here is the
# same bargain the 3.7 entry struck: the registry serves them natively,
# and the frozen seed is not the place that decides what exists.
POST_FREEZE_MODELS = {
    "google-flash37-500k/gemini-3.7-flash-500k",
    "google/gemini-3.8-flash",
    "google-flash38-50k/gemini-3.8-flash-50k",
    "google-flash38-100k/gemini-3.8-flash-100k",
    "google-flash38-200k/gemini-3.8-flash-200k",
    "google-flash38-500k/gemini-3.8-flash-500k",
    "google-flash38-1m/gemini-3.8-flash-1m",
}


def test_every_supported_model_is_seeded(seed_models):
    """Every SUPPORTED_MODELS entry has a seeded row — no on-disk
    entries missing from the DB seed."""
    seeded_ids = {r["id"] for r in seed_models}
    missing = [m["id"] for m in SUPPORTED_MODELS
               if m["id"] not in seeded_ids
               and m["id"] not in POST_FREEZE_MODELS]
    assert not missing, (
        f"SUPPORTED_MODELS entries missing from models_seed: {missing!r}"
    )


def test_the_post_freeze_allowance_does_not_outlive_its_entries():
    """An allowance nobody prunes becomes a list of forgotten drift."""
    known = {m["id"] for m in SUPPORTED_MODELS}
    stale = sorted(POST_FREEZE_MODELS - known)
    assert not stale, (
        f"{stale} are excused from the seed check but no longer exist in "
        f"SUPPORTED_MODELS -- remove them from POST_FREEZE_MODELS")


def test_every_provider_id_is_recognised(seed_models):
    """Under Option A there is no FK on models.provider_id — this
    test is the substitute: every seeded provider_id must be a
    recognised native provider.

    The other half of the allowlist used to be parsed from
    `model_provider_registry_seed.sql`. e1a323cc retired that table as
    dead schema and deleted the seed, and the parser outlived the file --
    raising FileNotFoundError at fixture setup, which pytest reports as
    an ERROR and not a failure, so this stopped running rather than
    stopped passing.

    SUPPORTED_MODELS replaces it, and is the better source anyway: the
    synthetic `<owner>-<shorthand>-<size>` providers in the frozen seed
    are on-disk names, not database ones, and app/llm_models.py is where
    they are declared. `models_hierarchy_backfill.sql` rewrote the rows
    onto the three native providers IN THE DATABASE; the seed file it
    superseded still reads the old way, which is why the allowlist needs
    both."""
    # AND THE RETIRED ONES. Their providers are on-disk names that the
    # frozen seed still carries; dropping the models from the catalogue
    # took the provider names with them, which would make every retired
    # row's provider_id read as unrecognised.
    known = (NATIVE_PROVIDERS
             | {m["provider"] for m in SUPPORTED_MODELS}
             | {rid.split("/")[0] for rid in RETIRED_FROM_CATALOGUE})
    unknown = sorted({
        row["provider_id"]
        for row in seed_models
        if row["provider_id"] not in known
    })
    assert not unknown, (
        f"provider_id(s) not in synthetic seed and not in native "
        f"allowlist {sorted(NATIVE_PROVIDERS)!r}: {unknown!r}"
    )
