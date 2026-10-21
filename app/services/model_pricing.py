"""Model pricing, surfaced on the Model Registry admin page.

Pricing rows are append-only (`migrations/model_pricing.sql`) — changing a
rate means inserting a new row with a later `effective_from`, never
UPDATE/DELETE. `dashboard_metrics.py`'s cost query still joins purely on
the free-text `(provider, model)` columns; `model_id` (added by
`migrations/model_pricing_model_id.sql`) is an additional, admin-UI-facing
soft link to `models.id` and changes nothing about that cost-query join.

Every row is grouped here into either:
  * "linked"   — `model_id` resolves to a real `models` row. These render
    on that model's card in the registry page, with an "Add rate" action
    that always sets `model_id` (so new rows are never ambiguous).
  * "unlinked" — legacy rows whose `(provider, model)` text never matched
    a real model (e.g. `claude-cli/haiku` — no base row ever existed;
    `google/gemini-2.5-flash-exp`/`-preview` — workaround rows for the
    cost-CTE's suffix-stripping regex, see
    `migrations/model_pricing_gemini_preview.sql`). These are read-only
    here — surfaced so their history stays visible, not force-matched.
"""
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_PRICE_COLS = """
    id, model_id, provider, model, provider_label, notes,
    input_rate, output_rate, cache_read_rate, cache_write_rate,
    thinking_rate, effective_from, created_at
"""


async def list_pricing_grouped(pool) -> dict[str, list[dict]]:
    """Every `model_pricing` row, newest-first, grouped into `current`
    (the latest row) + `history` (the rest), split into `linked` (has a
    `model_id`) and `unlinked` (does not).

    Linked rows group by `model_id`, NOT by the raw `(provider, model)`
    text pair — several legacy `(provider, model)` strings can resolve to
    the same model via `model_aliases` (e.g. `google/gemini-2.5-flash`,
    `google/gemini-2.5-flash-exp`, and `google/gemini-2.5-flash-preview`
    all resolve to `model_id = 'google/gemini-2.5-flash'`, see
    `migrations/model_aliases_seed_pricing.sql`). Grouping by the text
    pair instead would show the same model three times in the admin UI
    with identical rates, rather than one row with all three variants'
    entries folded into its history. Unlinked rows have no shared
    identity to group by, so they keep the (provider, model) grouping.

    Returns `{"linked": [...], "unlinked": [...]}`; each entry is
    `{model_id, provider, model, current: row, history: [rows]}` — for a
    linked entry, `provider`/`model` are the CURRENT (latest) row's own
    text, and `history` may contain rows whose (provider, model) text
    differs from `current`'s (that's expected — same model, a
    differently-named snapshot).

    A history row identical in `(effective_from, *rates)` to a row
    already kept (current, or an earlier-kept history row) is dropped —
    it isn't a real distinct price point, just the exact same rate
    recorded a second time under a different legacy `(provider, model)`
    name at the same instant (the Gemini `-exp`/`-preview` seed rows are
    byte-identical duplicates of their canonical row's rate, see
    `migrations/model_pricing_gemini_preview.sql`). Grouping by model_id
    would otherwise surface these as fake "history," implying a rate
    change that never happened.
    """
    rows = await pool.fetch(f"""
        SELECT {_PRICE_COLS}
        FROM   model_pricing
        ORDER  BY effective_from DESC
    """)

    def price_point(row) -> tuple:
        return (
            row["effective_from"], row["input_rate"], row["output_rate"],
            row["cache_read_rate"], row["cache_write_rate"], row["thinking_rate"],
        )

    groups: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for row in rows:
        key = row["model_id"] or (row["provider"], row["model"])
        if key not in groups:
            groups[key] = {
                "model_id": row["model_id"],
                "provider": row["provider"],
                "model": row["model"],
                "current": row,
                "history": [],
                "_seen_points": {price_point(row)},
            }
            order.append(key)
        else:
            point = price_point(row)
            if point in groups[key]["_seen_points"]:
                continue
            groups[key]["_seen_points"].add(point)
            groups[key]["history"].append(row)
    for g in groups.values():
        del g["_seen_points"]

    linked = [groups[k] for k in order if groups[k]["model_id"]]
    unlinked = [groups[k] for k in order if not groups[k]["model_id"]]
    logger.debug(
        "list_pricing_grouped: %d linked model(s), %d unlinked", len(linked), len(unlinked),
    )
    return {"linked": linked, "unlinked": unlinked}


async def count_unpriced_active_models(pool) -> int:
    """How many active models have zero `model_pricing` rows — read from
    `v_model_pricing_coverage` (migrations/model_pricing_coverage_view.sql).

    Used by the model-registry self-heal loop (`main.py`) to log a
    WARNING when non-zero, so the next model added with no rate is
    visible instead of silently reporting $0.00 — the same failure
    class `model_pricing_gemini_preview.sql`'s incident writeup
    describes. Not surfaced as a hard block anywhere: adding a model
    before its price is a legitimate order of operations.
    """
    return await pool.fetchval(
        "SELECT count(*) FROM v_model_pricing_coverage WHERE NOT priced",
    )


class PricingValidationError(Exception):
    """Raised for a bad `create_price_version` payload — message is
    already user-facing (re-rendered inline like `ManagementError`)."""


async def create_price_version(
    pool,
    model_id: str,
    *,
    input_rate: float,
    output_rate: float,
    cache_read_rate: float = 0,
    cache_write_rate: float = 0,
    thinking_rate: float | None = None,
    provider_label: str | None = None,
    notes: str | None = None,
    effective_from: datetime | None = None,
) -> None:
    """Insert a new pricing version for `model_id`, resolving its
    `provider`/`model` text from the `models` row itself — the admin never
    free-types these, closing the gap that produced mismatched rows like
    the `-exp`/`-preview` workarounds in the first place.
    """
    model_row = await pool.fetchrow(
        "SELECT provider_id, model_name FROM models WHERE id = $1", model_id,
    )
    if not model_row:
        raise PricingValidationError(f"Unknown model {model_id!r}")

    await pool.execute("""
        INSERT INTO model_pricing
            (model_id, provider, model, provider_label, notes,
             input_rate, output_rate, cache_read_rate, cache_write_rate,
             thinking_rate, effective_from)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,COALESCE($11, now()))
    """,
        model_id, model_row["provider_id"], model_row["model_name"],
        provider_label, notes,
        input_rate, output_rate, cache_read_rate, cache_write_rate,
        thinking_rate, effective_from,
    )
    logger.info(
        "model pricing added: model_id=%s in=%.4f out=%.4f cr=%.4f cw=%.4f thinking=%s",
        model_id, input_rate, output_rate, cache_read_rate, cache_write_rate,
        f"{thinking_rate:.4f}" if thinking_rate is not None else "output_rate",
    )
