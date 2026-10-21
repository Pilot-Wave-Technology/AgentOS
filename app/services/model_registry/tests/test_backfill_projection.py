"""Test the projected effect of `models_hierarchy_backfill.sql`.

Runs offline — no Postgres. Reads the migration file as text, then
simulates each of its three steps against a hand-rolled fixture that
mirrors the shape the (since-removed, family-A) `model_registry_seed.sql`
+ `models_seed.sql` seeded at the time this backfill was written. The
fixture is a plain Python dict, independent of that file's continued
existence. The simulator does NOT parse arbitrary SQL — it
reimplements the *documented* algorithm from the plan (INSERT with
ON CONFLICT DO NOTHING; UPDATE guarded by NULL/empty predicate;
CTE-derived shorthand + windows). Its job is to give us a target the
migration should hit, and idempotency proof by running the simulator
twice.

We also spot-check the raw SQL text for the essential guards
(ON CONFLICT DO NOTHING count, the NULL/empty predicate) so a future
edit that removes them fails a test here rather than silently
breaking re-runs on already-seeded databases.

Cycle-01 of story 414f92d6.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKFILL_PATH = _REPO_ROOT / "migrations" / "models_hierarchy_backfill.sql"


def _load_sql() -> str:
    assert _BACKFILL_PATH.exists(), f"missing: {_BACKFILL_PATH}"
    return _BACKFILL_PATH.read_text()


def _load_executable_sql() -> str:
    """`_load_sql()` minus `--` comment lines.

    Needed for any assertion about what the migration REFERENCES: this
    file's header deliberately names columns it no longer touches (to
    record why they were removed), so a naive substring check over the
    raw text can't distinguish "still uses it" from "explains why it
    doesn't"."""
    return "\n".join(
        line for line in _BACKFILL_PATH.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


# ---------------------------------------------------------------------------
# Fixture: model_registry rows the fleet has seeded today.
#
# Fields: id, label, provider, tier, context_window, upstream_id, status.
# Only the rows the backfill's CTE reads (upstream_id IS NOT NULL) matter
# for the UPDATE step; the native rows are here for completeness.
# ---------------------------------------------------------------------------


def _model_registry_fixture() -> list[dict]:
    natives = [
        ("anthropic/claude-haiku-4-5",  "Claude Haiku 4.5",  "anthropic", "fast",     200_000, None),
        ("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "anthropic", "balanced", 200_000, None),
        ("anthropic/claude-opus-4-7",   "Claude Opus 4.7",   "anthropic", "premium",  200_000, None),
        ("google/gemini-2.5-flash",     "Gemini 2.5 Flash",  "google",    "fast",     1_000_000, None),
        ("google/gemini-2.5-pro",       "Gemini 2.5 Pro",    "google",    "premium",  1_000_000, None),
    ]
    synthetics: list[tuple] = []
    for cw in (50_000, 100_000, 200_000, 500_000, 1_000_000):
        synthetics.append((
            f"google-flash-{_wire_suffix(cw)}/gemini-2.5-flash-{_wire_suffix(cw)}",
            f"Gemini Flash ({_display_suffix(cw)} test)",
            f"google-flash-{_wire_suffix(cw)}", "test", cw, "google/gemini-2.5-flash",
        ))
    for cw in (100_000, 200_000, 500_000, 1_000_000):
        synthetics.append((
            f"google-pro-{_wire_suffix(cw)}/gemini-2.5-pro-{_wire_suffix(cw)}",
            f"Gemini Pro ({_display_suffix(cw)} test)",
            f"google-pro-{_wire_suffix(cw)}", "test", cw, "google/gemini-2.5-pro",
        ))
    for cw in (50_000, 100_000, 200_000, 500_000, 1_000_000):
        synthetics.append((
            f"anthropic-sonnet-{_wire_suffix(cw)}/claude-sonnet-4-5-{_wire_suffix(cw)}",
            f"Claude Sonnet 4.5 ({_display_suffix(cw)} test)",
            f"anthropic-sonnet-{_wire_suffix(cw)}", "test", cw, "anthropic/claude-sonnet-4-5",
        ))
    # opus-4-6 / opus-4-8 / sonnet-5 have synthetic children in model_registry
    # but their upstream base ids were NOT seeded into `models` (the
    # removed family-A model_registry_seed.sql treated those upstreams as
    # routing targets, not admin picker rows). The UPDATE step must not create
    # models rows for them; it only touches rows that already exist.
    for cw in (100_000, 200_000, 500_000, 1_000_000):
        synthetics.append((
            f"anthropic-opus-{_wire_suffix(cw)}/claude-opus-4-6-{_wire_suffix(cw)}",
            f"Claude Opus 4.6 ({_display_suffix(cw)} test)",
            f"anthropic-opus-{_wire_suffix(cw)}", "test", cw, "anthropic/claude-opus-4-6",
        ))

    rows: list[dict] = []
    for tup in natives + synthetics:
        rows.append({
            "id": tup[0], "label": tup[1], "provider": tup[2],
            "tier": tup[3], "context_window": tup[4], "upstream_id": tup[5],
            "status": "active",
        })
    return rows


def _wire_suffix(tokens: int) -> str:
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}m"
    return f"{tokens // 1_000}k"


def _display_suffix(tokens: int) -> str:
    return _wire_suffix(tokens).upper()


# ---------------------------------------------------------------------------
# Fixture: models rows (Phase A3 seed shape) — the rows the UPDATE
# step will find (or not find) via `m.id = pb.base_id`.
# ---------------------------------------------------------------------------


def _models_fixture() -> dict[str, dict]:
    """A models-table snapshot seeded exactly as models_seed.sql lands it."""
    rows: dict[str, dict] = {}
    # Native rows.
    for mid, label, provider, tier, cw in [
        ("anthropic/claude-haiku-4-5",  "Claude Haiku 4.5",  "anthropic", "fast",     200_000),
        ("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "anthropic", "balanced", 200_000),
        ("anthropic/claude-opus-4-7",   "Claude Opus 4.7",   "anthropic", "premium",  200_000),
        ("google/gemini-2.5-flash",     "Gemini 2.5 Flash",  "google",    "fast",     1_000_000),
        ("google/gemini-2.5-pro",       "Gemini 2.5 Pro",    "google",    "premium",  1_000_000),
    ]:
        rows[mid] = _mk_models_row(mid, provider, mid.split("/", 1)[1], label, tier, cw)
    # A few synthetic rows exist too (from models_seed.sql), but the backfill
    # UPDATE only touches base ids (matches m.id to pb.base_id which is
    # `upstream_id`). So we don't need to seed every synthetic here; a small
    # sampling is enough to prove the UPDATE ignores them.
    for mid in ("google-flash-100k/gemini-2.5-flash-100k",
                "anthropic-sonnet-500k/claude-sonnet-4-5-500k"):
        rows[mid] = _mk_models_row(
            mid, mid.split("/", 1)[0], mid.split("/", 1)[1], mid, "test", 100_000,
        )
    return rows


def _mk_models_row(mid, provider_id, model_name, label, tier, cw) -> dict:
    return {
        "id": mid, "provider_id": provider_id, "model_name": model_name,
        "wire_id": mid, "label": label, "tier": tier, "context_window": cw,
        "provider_shorthand": None,
        "available_context_windows": [],
        "status": "active",
    }


# ---------------------------------------------------------------------------
# Simulator: apply the three backfill steps to Python dicts.
# ---------------------------------------------------------------------------


def _step1_insert_providers(mp: dict[str, dict]) -> None:
    """Idempotent INSERT of the native provider rows (no `kind` column —
    dropped entirely; every row is implicitly a real upstream)."""
    seed = [
        ("anthropic",  "Anthropic",   "https://api.anthropic.com/v1",
         "ANTHROPIC_API_KEY", "anthropic-messages", "provisioned"),
        ("google",     "Google",      "https://generativelanguage.googleapis.com/v1beta",
         "GEMINI_API_KEY",    "google-generative-ai", "provisioned"),
        ("claude-cli", "Claude CLI",  None, None, "cli", "provisioned"),
    ]
    for pid, label, base_url, api_key_source, api_type, runtime_status in seed:
        # ON CONFLICT (id) DO NOTHING — skip if the id is already present.
        if pid in mp:
            continue
        mp[pid] = {
            "id": pid, "label": label,
            "base_url": base_url, "api_key_source": api_key_source,
            "api_type": api_type, "runtime_status": runtime_status,
            "status": "active",
        }


def _step2_update_models_from_registry(
    models: dict[str, dict], registry: list[dict],
) -> None:
    """UPDATE models with derived shorthand + windows from model_registry.

    Mirrors the CTE in the SQL: for every synthetic child (upstream_id
    IS NOT NULL, status='active'), extract shorthand by dropping the
    first + last hyphen-delimited segment of the child's `provider`
    column, then aggregate windows per upstream base_id.
    """
    per_base: dict[str, dict] = {}  # base_id → {shorthand, windows}
    for r in registry:
        if r["upstream_id"] is None or r["status"] != "active":
            continue
        base_id = r["upstream_id"]
        parts = r["provider"].split("-")
        assert len(parts) >= 3, f"unexpected provider shape: {r['provider']}"
        shorthand = "-".join(parts[1:-1])
        entry = per_base.setdefault(base_id, {"shorthand": shorthand, "windows": set()})
        # Every child agrees on shorthand — MIN(shorthand) would be the SQL's
        # dedup; we just assert consistency here as an extra safety net.
        assert entry["shorthand"] == shorthand, (
            f"drift: base {base_id} has synthetic children with divergent "
            f"shorthands {entry['shorthand']!r} vs {shorthand!r}"
        )
        entry["windows"].add(r["context_window"])

    for base_id, agg in per_base.items():
        row = models.get(base_id)
        if row is None:
            # `LEFT JOIN` semantics: base_id absent from models means no
            # UPDATE. This is the "opus-4-6 / opus-4-8 / sonnet-5" case.
            continue
        # Guard predicate: only touch NULL/empty rows.
        if row["provider_shorthand"] is not None:
            continue
        if len(row["available_context_windows"]) > 0:
            continue
        row["provider_shorthand"] = agg["shorthand"]
        row["available_context_windows"] = sorted(agg["windows"])


def _step3_insert_cli_models(models: dict[str, dict]) -> None:
    """Idempotent INSERT of three CLI model rows."""
    seed = [
        ("claude-cli/opus",   "claude-cli", "opus",    "Claude CLI Opus"),
        ("claude-cli/sonnet", "claude-cli", "sonnet",  "Claude CLI Sonnet"),
        ("codex-cli/gpt-5.4", "codex-cli",  "gpt-5.4", "Codex CLI GPT-5.4"),
    ]
    for mid, provider_id, model_name, label in seed:
        if mid in models:
            continue
        models[mid] = _mk_models_row(mid, provider_id, model_name, label, "agentic", 200_000)


def _apply_backfill(mp, models, registry):
    _step1_insert_providers(mp)
    _step2_update_models_from_registry(models, registry)
    _step3_insert_cli_models(models)


# ---------------------------------------------------------------------------
# SQL text spot-checks (paired with the simulator so a future edit that
# removes a critical guard fails a test rather than silently breaking
# re-runs on already-populated databases).
# ---------------------------------------------------------------------------


def test_backfill_uses_on_conflict_do_nothing_twice():
    """Both INSERT statements must be idempotent."""
    sql = _load_sql().upper()
    assert sql.count("ON CONFLICT (ID) DO NOTHING") == 2


def test_backfill_update_is_guarded_by_null_empty_predicate():
    """The UPDATE step MUST leave already-populated rows alone.

    The guard used to be two-part (`provider_shorthand IS NULL AND
    cardinality(m.available_context_windows) = 0`). The array column was
    removed in favour of the `model_context_windows` junction table, so
    the surviving `provider_shorthand IS NULL` half now carries the whole
    guarantee on its own — which it does: that column's DEFAULT is NULL,
    so the predicate matches exactly the not-yet-backfilled rows and a
    re-run is a pure no-op.
    """
    assert "provider_shorthand IS NULL" in _load_sql()
    # The removed column must not come back — an executable reference to it
    # would fail on any environment created from the current migration
    # files (this is exactly how the pre-fix version broke a fresh deploy).
    assert "available_context_windows" not in _load_executable_sql()


def test_backfill_has_no_delete_drop_alter():
    """Additive only — no destructive DML/DDL."""
    sql = _load_sql().upper()
    for forbidden in ("DELETE FROM", "DROP TABLE", "ALTER TABLE", "TRUNCATE "):
        assert forbidden not in sql, f"forbidden construct in backfill: {forbidden}"


# ---------------------------------------------------------------------------
# Simulator assertions — step 1: provider seed.
# ---------------------------------------------------------------------------


def test_step1_seeds_three_provider_rows_all_provisioned():
    """openai/codex-cli are deliberately deferred — not seeded (see
    models_hierarchy_backfill.sql's header)."""
    mp: dict = {}
    _step1_insert_providers(mp)
    assert set(mp) == {"anthropic", "google", "claude-cli"}
    for pid in mp:
        assert mp[pid]["runtime_status"] == "provisioned"


def test_step1_api_tier_rows_carry_url_key_source_type():
    mp: dict = {}
    _step1_insert_providers(mp)
    assert mp["anthropic"]["base_url"] == "https://api.anthropic.com/v1"
    assert mp["anthropic"]["api_key_source"] == "ANTHROPIC_API_KEY"
    assert mp["anthropic"]["api_type"] == "anthropic-messages"
    assert mp["google"]["base_url"].startswith("https://generativelanguage.googleapis.com")


def test_step1_cli_tier_rows_have_null_url_and_key_source():
    mp: dict = {}
    _step1_insert_providers(mp)
    assert mp["claude-cli"]["base_url"] is None
    assert mp["claude-cli"]["api_key_source"] is None
    assert mp["claude-cli"]["api_type"] == "cli"


# ---------------------------------------------------------------------------
# Simulator assertions — step 2: UPDATE derives shorthand + windows.
# ---------------------------------------------------------------------------


def test_step2_populates_google_flash_with_five_windows():
    models = _models_fixture()
    _step2_update_models_from_registry(models, _model_registry_fixture())
    row = models["google/gemini-2.5-flash"]
    assert row["provider_shorthand"] == "flash"
    assert row["available_context_windows"] == [50_000, 100_000, 200_000, 500_000, 1_000_000]


def test_step2_populates_google_pro_with_four_windows():
    models = _models_fixture()
    _step2_update_models_from_registry(models, _model_registry_fixture())
    row = models["google/gemini-2.5-pro"]
    assert row["provider_shorthand"] == "pro"
    assert row["available_context_windows"] == [100_000, 200_000, 500_000, 1_000_000]


def test_step2_populates_claude_sonnet_4_5_with_five_windows():
    models = _models_fixture()
    _step2_update_models_from_registry(models, _model_registry_fixture())
    row = models["anthropic/claude-sonnet-4-5"]
    assert row["provider_shorthand"] == "sonnet"
    assert row["available_context_windows"] == [50_000, 100_000, 200_000, 500_000, 1_000_000]


def test_step2_leaves_native_only_rows_untouched():
    """Base rows without synthetic children in model_registry (haiku,
    opus-4-7) remain at their DEFAULT state after the UPDATE."""
    models = _models_fixture()
    _step2_update_models_from_registry(models, _model_registry_fixture())
    for mid in ("anthropic/claude-haiku-4-5", "anthropic/claude-opus-4-7"):
        row = models[mid]
        assert row["provider_shorthand"] is None, mid
        assert row["available_context_windows"] == [], mid


def test_step2_does_not_create_missing_base_rows():
    """A base_id that has synthetic children in model_registry but no
    `models` row (e.g. anthropic/claude-opus-4-6 which is a routing
    target only) MUST NOT cause the backfill to CREATE a new row."""
    models = _models_fixture()
    before = set(models)
    _step2_update_models_from_registry(models, _model_registry_fixture())
    assert set(models) == before, (
        "step 2 must UPDATE only, not INSERT — the backfill has no "
        "INSERT-into-models statement for anthropic/claude-opus-4-6"
    )
    assert "anthropic/claude-opus-4-6" not in models


def test_step2_does_not_touch_synthetic_variant_rows():
    """Only base rows get shorthand + windows; the synthetic variant
    rows in `models` (e.g. google-flash-100k/...) must remain untouched.
    """
    models = _models_fixture()
    _step2_update_models_from_registry(models, _model_registry_fixture())
    v = models["google-flash-100k/gemini-2.5-flash-100k"]
    assert v["provider_shorthand"] is None
    assert v["available_context_windows"] == []


# ---------------------------------------------------------------------------
# Simulator assertions — step 3: three CLI seed rows.
# ---------------------------------------------------------------------------


def test_step3_inserts_three_cli_rows_with_null_shorthand_and_empty_windows():
    models = _models_fixture()
    _step3_insert_cli_models(models)
    for mid, provider_id, model_name, label in [
        ("claude-cli/opus",   "claude-cli", "opus",    "Claude CLI Opus"),
        ("claude-cli/sonnet", "claude-cli", "sonnet",  "Claude CLI Sonnet"),
        ("codex-cli/gpt-5.4", "codex-cli",  "gpt-5.4", "Codex CLI GPT-5.4"),
    ]:
        row = models[mid]
        assert row["provider_id"] == provider_id
        assert row["model_name"] == model_name
        assert row["label"] == label
        assert row["tier"] == "agentic"
        assert row["context_window"] == 200_000
        assert row["provider_shorthand"] is None
        assert row["available_context_windows"] == []


# ---------------------------------------------------------------------------
# Idempotency — second pass must be a no-op.
# ---------------------------------------------------------------------------


def test_full_backfill_is_idempotent_over_two_runs():
    mp: dict = {}
    models = _models_fixture()
    registry = _model_registry_fixture()

    _apply_backfill(mp, models, registry)
    snapshot_mp = deepcopy(mp)
    snapshot_models = deepcopy(models)

    # Second pass — everything must be unchanged.
    _apply_backfill(mp, models, registry)
    assert mp == snapshot_mp, "step 1 (providers) not idempotent"
    assert models == snapshot_models, "steps 2+3 (models) not idempotent"


def test_second_pass_does_not_touch_populated_shorthand_field():
    """Even if the UPDATE ran with pb.shorthand != current shorthand, the
    guard `AND m.provider_shorthand IS NULL` should keep the current
    value intact. Verified by pre-populating a distinctive shorthand
    and observing the simulator refuses to overwrite it."""
    models = _models_fixture()
    models["google/gemini-2.5-flash"]["provider_shorthand"] = "custom-op"
    models["google/gemini-2.5-flash"]["available_context_windows"] = [42_000]
    registry = _model_registry_fixture()

    _step2_update_models_from_registry(models, registry)
    assert models["google/gemini-2.5-flash"]["provider_shorthand"] == "custom-op"
    assert models["google/gemini-2.5-flash"]["available_context_windows"] == [42_000]
