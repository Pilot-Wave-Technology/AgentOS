"""Model-registry bootstrap + skew guard.

Phase A3 (story b134ed98). Additive seam between the on-disk
`app.llm_models.SUPPORTED_MODELS` list and the runtime `models` table
populated by the Phase A3 migrations.

Two responsibilities:
1. `load_models(pool)` — pull the active model set from the DB, falling
   back to SUPPORTED_MODELS whenever the DB result is empty or the
   query raises. Callers can rely on it under any condition; it NEVER
   raises.
2. `check_skew(pool, on_disk)` — diff the DB result against the on-disk
   list by wire_id. Reports rows present in only one side, and rows
   present in both but with divergent metadata. Emits one INFO log per
   non-empty bucket for observability.

Consumers of `app.llm_models` are deliberately NOT rewritten in this
cycle — SUPPORTED_MODELS remains their source of truth. This module is
the observation seam that lets operators see drift as it accumulates
across environments; the actual switch-over to a DB-backed model set
is a later phase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.llm_models import SUPPORTED_MODELS

_log = logging.getLogger(__name__)

# Fields compared when detecting metadata drift between the on-disk
# SUPPORTED_MODELS list and the runtime `models` table. Kept to the
# fields that appear on BOTH sides — SUPPORTED_MODELS entries don't
# carry `context_window` directly (that's derived by
# app.llm_models.context_window_for), so we don't diff it here either.
# The on-disk `ModelEntry` shape (llm_models.py:16-20) has exactly
# these keys.
_METADATA_FIELDS: tuple[str, ...] = ("label", "provider", "tier")


_LOAD_MODELS_SQL = """
SELECT id,
       wire_id,
       label,
       provider_id       AS provider,
       tier,
       context_window,
       status
  FROM models
 WHERE status = 'active'
 ORDER BY wire_id
"""


async def load_models(pool: asyncpg.Pool) -> list[dict]:
    """Load active models from the runtime `models` table.

    Returns dicts shape-compatible with `app.llm_models.ModelEntry`
    (keys `id`, `label`, `provider`, `tier`) plus the Phase A3 columns
    (`wire_id`, `context_window`, `status`). `max_output_tokens`/
    `gateway_params` were dropped from `models` 2026-08-06 (confirmed
    dead — never read anywhere) and removed from this query
    accordingly; this function never exposed them to any caller anyway.

    Fallback behaviour:
      - Empty result → returns SUPPORTED_MODELS and logs a WARNING.
        Expected on a fresh feature-branch DB where the Phase A3
        migrations have not been applied; unexpected in production.
      - Any exception → returns SUPPORTED_MODELS and logs an ERROR
        with `exc_info`. NEVER raises.
    """
    try:
        rows = await pool.fetch(_LOAD_MODELS_SQL)
    except Exception:
        _log.error(
            "llm_models_bootstrap.load_models: DB query failed; "
            "falling back to SUPPORTED_MODELS",
            exc_info=True,
        )
        return list(SUPPORTED_MODELS)

    if not rows:
        _log.warning(
            "llm_models_bootstrap.load_models: `models` table returned "
            "no active rows; falling back to SUPPORTED_MODELS. This is "
            "expected on a DB whose Phase A3 migrations have not yet "
            "been applied; unexpected in production."
        )
        return list(SUPPORTED_MODELS)

    return [dict(r) for r in rows]


@dataclass
class SkewReport:
    """Difference between the DB `models` table and on-disk SUPPORTED_MODELS.

    Attributes:
        only_in_db:    wire_ids present in the DB but not in on_disk.
        only_on_disk:  wire_ids present in on_disk but not in the DB.
        metadata_diff: wire_ids present in both, with mismatched values
                       in one of `_METADATA_FIELDS`. Each entry is
                       `(wire_id, diff_dict)` where diff_dict maps
                       field name to `{"db": value, "on_disk": value}`.
    """
    only_in_db: list[str] = field(default_factory=list)
    only_on_disk: list[str] = field(default_factory=list)
    metadata_diff: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


async def check_skew(pool: asyncpg.Pool, on_disk: list[dict]) -> SkewReport:
    """Compare the DB `models` table against the on-disk SUPPORTED_MODELS.

    Diff is keyed by wire_id (which equals `ModelEntry.id` for every
    canonical entry). Emits one INFO log line per non-empty bucket so
    operators can see drift as it accumulates. Never raises: DB errors
    are swallowed inside `load_models`, and any diff-computation error
    would still return an empty report (`load_models` on the failure
    path returns SUPPORTED_MODELS, which equals `on_disk` in practice —
    so the report is empty).
    """
    db_rows = await load_models(pool)

    # Prefer `wire_id` on the DB side (definitive), fall back to `id`
    # for the SUPPORTED_MODELS fallback path where entries only carry
    # `id`. The two are equal for every canonical row today.
    db_by_key: dict[str, dict] = {
        (r.get("wire_id") or r.get("id")): r for r in db_rows
    }
    on_disk_by_key: dict[str, dict] = {r.get("id"): r for r in on_disk}

    only_in_db = sorted(set(db_by_key) - set(on_disk_by_key))
    only_on_disk = sorted(set(on_disk_by_key) - set(db_by_key))

    metadata_diff: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(set(db_by_key) & set(on_disk_by_key)):
        db_row = db_by_key[key]
        disk_row = on_disk_by_key[key]
        diff: dict[str, dict[str, Any]] = {}
        for f in _METADATA_FIELDS:
            db_val = db_row.get(f)
            disk_val = disk_row.get(f)
            if db_val != disk_val:
                diff[f] = {"db": db_val, "on_disk": disk_val}
        if diff:
            metadata_diff.append((key, diff))

    if only_in_db:
        _log.info(
            "model registry skew: only_in_db=%d entries=%s",
            len(only_in_db), only_in_db,
        )
    if only_on_disk:
        _log.info(
            "model registry skew: only_on_disk=%d entries=%s",
            len(only_on_disk), only_on_disk,
        )
    if metadata_diff:
        _log.info(
            "model registry skew: metadata_diff=%d entries=%s",
            len(metadata_diff), [wid for wid, _ in metadata_diff],
        )

    return SkewReport(
        only_in_db=only_in_db,
        only_on_disk=only_on_disk,
        metadata_diff=metadata_diff,
    )
