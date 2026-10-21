"""Live-DB dropdown data for the three model-picker UIs.

Cycle-01 of story `5dafee43` swaps three UI creation flows from
hardcoded model lists (`llm_models.SUPPORTED_MODELS`,
`settings.available_models`, and the gateway's `models.list` RPC)
to reads against the v2 Model Registry (`model_providers` +
extended `models` tables shipped by story `414f92d6`).

This module is the composition layer: it uses the v2 primitives
in `management.py` (`list_base_models_with_provider_v2`) + the
synthesizer (`synthesize_all_variants`) and shapes their output into
the two view-models the templates consume. Every entry point below
routes through `_fetch_selectable_base_models`, which filters out
models whose owning provider has been disabled (`availability.py`) —
one shared gate rather than three independent ones, so a disabled
provider disappears from every picker and fails `is_model_registered_v2`
consistently:

  - `build_go_model_tree_v2(pool)` — the two-select base+context
    picker used by `_go_model_picker.html`. Output shape is
    byte-identical to `llm_models.go_model_tree()` so the widget's
    JavaScript and DOM structure need no changes.
  - `list_model_ids_for_dropdown_v2(pool)` — a flat sorted list
    used by the New Template wizard and the template-detail edit
    form.
  - `is_dropdown_empty(x)` — small sentinel-check helper used by
    the templates to decide whether to render the "no models
    registered" empty-state banner.
  - `is_model_registered_v2(pool, model_id)` — server-side
    validation shim used by `create_goal` / `change_agent_model`
    so a newly-registered DB model works end-to-end even if it's
    not in the legacy `SUPPORTED_MODELS` hardcoded list.

No direct SQL, no `model_registry` (v1) reads, no gateway calls.
On any `pool` exception the read helpers log a WARNING and return
their empty-sentinel value so a transient DB blip never crashes a
page render — the caller renders the same empty-state UI as when
zero models are registered.
"""
import logging
from typing import Any

from app.services.model_registry.availability import (
    is_model_selectable,
    model_credential_warning,
)
from app.services.model_registry.management import list_base_models_with_provider_v2
from app.services.model_registry.synthesizer import derive_base_id, synthesize_all_variants


logger = logging.getLogger(__name__)


# GO default: prefer this base when the registry offers it, otherwise the
# first base in id-order. Paired with GO_DEFAULT_CONTEXT, it is what the
# goal-creation picker lands on.
#
# MODULE SCOPE, NOT A FUNCTION LOCAL, and that is the fix as much as the
# value is. It named `gemini-2.5-flash` until 2026-10-19 -- one day after
# the catalogue dropped every pre-3 Gemini -- so the preference matched
# nothing and the picker fell through to `bases[0]`, whatever sorts first
# by id. It still had a default, which is why nobody saw it; it just was
# not a chosen one. Buried inside the function, the value was invisible
# to the reader deleting the models and untestable afterwards. Out here,
# `test_the_preferred_base_is_one_the_catalogue_offers` can hold it and
# `app/llm_models.py` to each other.
_PREFERRED_DEFAULT_BASE = "gemini-3.8-flash"


def _provider_view(row: dict) -> dict:
    """Extract the provider-status sub-view `availability.py` expects
    out of a `list_base_models_with_provider_v2` joined row."""
    return {
        "runtime_status": row.get("provider_runtime_status"),
        "status": row.get("provider_status"),
        "credential_status": row.get("provider_credential_status"),
        "credential_error": row.get("provider_credential_error"),
    }


async def _fetch_selectable_base_models(pool) -> list[dict]:
    """The one place every model-picker surface (`build_go_model_tree_v2`,
    `list_model_ids_for_dropdown_v2`, `is_model_registered_v2` — the
    latter gating `change_agent_model`/`create_goal` in the Agent
    Registry) reads active base models through, so a disabled provider
    is excluded everywhere a model can be picked or assigned, not just
    on the admin page. See `availability.py`'s module docstring for why
    only `status='disabled'` hard-excludes."""
    rows = await list_base_models_with_provider_v2(pool, include_disabled=False)
    return [row for row in rows if is_model_selectable(_provider_view(row))]


def _empty_tree() -> dict:
    """Fresh empty-tree sentinel — new lists/dicts on every call so a
    caller mutating one return value cannot bleed into the next.

    Callers use `is_dropdown_empty` (which checks `bases`) rather than
    sniffing individual keys, so the concrete default values below are
    only consulted by the widget's JS in the truthy-tree branch that
    this sentinel avoids taking.
    """
    return {
        "bases": [],
        "variants": {},
        "default_base": "",
        "default_context": 0,
        "default_id": "",
    }


def _size_label(tokens: int) -> str:
    """'100000' -> '100K'; '1000000' -> '1M'.

    Duplicated from `llm_models._variant_size_label` so this module
    stays independent of `llm_models` (which is the legacy fallback
    we are moving off).
    """
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}M"
    return f"{tokens // 1_000}K"


def _prefer_synthetic_on_tie(row_variants: list[dict], native_id: str) -> list[dict]:
    """Collapse same-size entries, keeping the synthetic variant over the native one.

    A model whose `model_context_windows` contains its own native size produces TWO
    entries at that size: the native one (bare id, e.g. `anthropic/claude-sonnet-4-5`)
    and the synthesized one (`anthropic-sonnet-200k/claude-sonnet-4-5-200k`). Four
    models are in this state today: claude-opus-4-6, claude-sonnet-4-5,
    gemini-2.5-flash and gemini-2.5-pro.

    Left alone that is actively harmful, not merely untidy:

      * the size dropdown renders two options with identical labels, and
      * `_go_model_picker.html` resolves a size with `.find(v => v.tokens === tokens)`
        — first match wins. The native entry is appended before the synthetics and the
        sort is stable, so the BASE always won and the synthetic variant was
        unreachable through the UI.

    The practical damage was silent: agents stored against a variant
    (platform-steward, platform-shca, the research-* templates) would be flipped to the
    base model by merely opening the picker and re-saving, with no visible change.

    Owner's decision (2026-08-16): a *size* selection means the VARIANT. The base model
    entry represents the original model at the provider's own native window, so where a
    synthesized variant exists for a size, that variant is what the size refers to.

    Order is preserved and only exact `tokens` collisions are touched, so a model with
    no duplicate sizes comes back byte-identical.
    """
    seen: dict[int, int] = {}          # tokens -> index in `out`
    out: list[dict] = []
    for entry in row_variants:
        tokens = entry["tokens"]
        if tokens not in seen:
            seen[tokens] = len(out)
            out.append(entry)
            continue
        # Collision: keep whichever entry is NOT the bare native id.
        if out[seen[tokens]]["id"] == native_id:
            out[seen[tokens]] = entry
    return out


# The largest window a GOAL may be set up on. Not a limit on the model
# and not a limit on the catalogue -- see the note at the filter below.
GO_MAX_CONTEXT = 500_000


async def build_go_model_tree_v2(
    pool, *, default_context: int = 500_000,
) -> dict:
    """DB-backed replacement for `llm_models.go_model_tree()`.

    Output shape (byte-identical to the v1 helper — key order and
    scalar types match so `_go_model_picker.html`'s JS keeps
    working unchanged):

        {
          "bases":    [{"key": <base_key>, "label": <label>}, ...],
          "variants": {<base_key>: [{"tokens": int, "label": "100K", "id": <variant_id>}, ...]},
          "default_base":    <base_key>,
          "default_context": <tokens>,
          "default_id":      <full synthetic id for (default_base, default_context)>,
        }

    Filter: only ACTIVE base rows (`status='active'`) are included.
    Every base contributes its own native `context_window` as a
    selectable entry (id = the bare base id, e.g.
    `anthropic/claude-opus-5`) — GO can run on the model's real window,
    not just a synthesized test-sized one. Bases with a
    `provider_shorthand` AND a non-empty `available_context_windows[]`
    additionally contribute one entry per synthesized context-window
    variant (id = the synthetic id, e.g.
    `anthropic-opus5-100k/claude-opus-5-100k`) — these exist to test
    compaction/percentage math at a fixed, artificially small token
    budget (see `synthesizer.py`), not to replace the native option.

    `default_base` is the first base in list-order (which is
    `list_base_models_v2` order, i.e. `ORDER BY id`). `default_id`
    resolves to the entry whose `tokens == default_context` if one
    exists, otherwise the smallest window for the default base.

    On any pool exception, returns a fresh empty-tree sentinel and
    logs at WARNING. Callers cannot distinguish "empty DB" from
    "read failed" and don't need to — both surface the same
    "No models available — configure in Model Registry" UI.
    """
    try:
        rows = await _fetch_selectable_base_models(pool)
    except Exception as exc:
        logger.warning(
            "dropdowns.build_go_model_tree_v2: DB read failed — %s", exc,
        )
        return _empty_tree()

    # ── which models a GOAL may be set up on ─────────────────────────
    #
    # A TAG AND NOT A LIST IN CODE: `models` is the live catalogue the
    # gateway pulls, so which of them a goal is offered is data like
    # everything else about a model. A tuple here would need a deploy to
    # add one, and would be a third place that has to agree with the
    # other two.
    #
    # IT NARROWS THE PICKER AND NOTHING ELSE. Every other base stays
    # `active` and stays assignable through the agent registry, the
    # template wizard and `change_agent_model`, and every GO already
    # running on one keeps running. Retiring a model is a different
    # decision from steering new goals.
    #
    # NO TAGGED MODEL MEANS NO FILTER, deliberately. If the migration
    # has not landed, or somebody removes the last tag, the honest
    # failure is the old dropdown -- not an empty one that blocks goal
    # creation with nothing on screen saying why.
    tagged = [r for r in rows if "go-selectable" in (r.get("tags") or [])]
    if tagged:
        rows = tagged
    else:
        logger.warning(
            "dropdowns.build_go_model_tree_v2: no model carries "
            "'go-selectable'; offering every active base")

    bases: list[dict] = []
    variants: dict[str, list[dict]] = {}

    for row in rows:
        shorthand = row.get("provider_shorthand")
        windows = row.get("available_context_windows") or []

        # The native entry: the base model at its own real context
        # window, selectable by its bare id — always offered.
        row_variants = [
            {
                "tokens": row["context_window"],
                "label": _size_label(row["context_window"]),
                "id": row["id"],
            },
        ]

        if shorthand and windows:
            try:
                expansions = synthesize_all_variants(row)
            except Exception as exc:
                # `synthesize_variant` raises ValueError on malformed
                # base_id / tokens; skip the synthesized variants for
                # this row rather than tanking the whole picker — the
                # native entry above is still offered.
                logger.warning(
                    "dropdowns.build_go_model_tree_v2: synth failed for "
                    "id=%r — %s", row.get("id"), exc,
                )
                expansions = []

            row_variants.extend(
                {
                    "tokens": exp["context_window"],
                    "label": _size_label(exp["context_window"]),
                    "id": exp["variant_id"],
                }
                for exp in expansions
            )

        # THE CEILING IS ON THE PICKER, NOT ON THE CATALOGUE. A 1M
        # variant stays synthesizable and stays assignable -- four
        # Council RA templates run on `gemini-3.8-flash-1m` today, and
        # dropping the rung from `model_context_windows` would leave
        # them naming something the synthesizer no longer produces. This
        # only declines to OFFER it when somebody sets up a goal.
        #
        # Applied after the native entry is built, so a base whose own
        # window is larger than the cap is bounded too rather than
        # slipping through on the "always offered" path.
        row_variants = [v for v in row_variants
                        if v["tokens"] <= GO_MAX_CONTEXT]
        if not row_variants:
            continue

        # Base key matches the v1 shape: the native model name
        # (right-hand side of the base id). v1 computed
        # `base_key = model_part[:sm.start()]` — stripping the
        # size suffix off a variant's model_part. v2 base rows
        # carry no size suffix, so `split("/", 1)[1]` gives the
        # same string.
        _, native_name = row["id"].split("/", 1)

        bases.append({"key": native_name, "label": row["label"]})
        row_variants.sort(key=lambda v: v["tokens"])
        variants[native_name] = _prefer_synthetic_on_tie(row_variants, row["id"])

    if not bases:
        return _empty_tree()

    default_base = next(
        (b["key"] for b in bases if b["key"] == _PREFERRED_DEFAULT_BASE),
        bases[0]["key"],
    )
    dv = variants.get(default_base, [])
    hit = (
        next((v for v in dv if v["tokens"] == default_context), None)
        or (dv[0] if dv else None)
    )

    return {
        "bases": bases,
        "variants": variants,
        "default_base": default_base,
        "default_context": default_context,
        "default_id": hit["id"] if hit else "",
    }


async def list_model_ids_for_dropdown_v2(pool) -> list[dict]:
    """Flat sorted list of every active BASE model — no context-window
    variants.

        [{"id": <full id>, "label": <human label>, "kind": "base"}, ...]

    Ordered by `id` ascending.

    Used by the New Template wizard (`authoring/wizard.html`) and the
    template-detail edit form (`templates/detail.html`). Deliberately
    excludes synthesized variants: sized context-window entries
    (`tier='test'`, e.g. "Gemini Flash (100K test)") were only ever
    designed as a controlled cost/size cap for GO
    (`build_go_model_tree_v2`) — offering ~20+ near-duplicate entries
    here for IA/RA/SHCA templates, which don't need a capped context
    window, was pure noise. `kind` is kept in the output shape (always
    `"base"` now) so callers/templates don't need updating.

    On any pool exception, returns `[]` and logs at WARNING.
    """
    try:
        rows = await _fetch_selectable_base_models(pool)
    except Exception as exc:
        logger.warning(
            "dropdowns.list_model_ids_for_dropdown_v2: DB read failed — %s",
            exc,
        )
        return []

    out = [
        {
            "id": row["id"],
            "label": row["label"],
            "kind": "base",
            # None when there's nothing to warn about — additive field,
            # existing template rendering can ignore it unchanged.
            "credential_warning": model_credential_warning(_provider_view(row)),
        }
        for row in rows
    ]
    out.sort(key=lambda r: r["id"])
    return out


def is_dropdown_empty(tree_or_list: Any) -> bool:
    """Return True if the given tree/list should render the empty-state banner.

    Accepts either the tree dict from `build_go_model_tree_v2` (checks
    `bases` truthiness) or the flat list from
    `list_model_ids_for_dropdown_v2` (checks length). Anything else
    is treated as empty so templates never crash on a shape mismatch.
    """
    if isinstance(tree_or_list, dict):
        return not tree_or_list.get("bases")
    if isinstance(tree_or_list, (list, tuple)):
        return len(tree_or_list) == 0
    return True


async def is_model_registered_v2(pool, model_id: str) -> bool:
    """True if `model_id` matches an active base OR synthesized variant
    whose owning provider is not disabled.

    Server-side validation shim used by `create_goal` and
    `change_agent_model` (the Agent Registry's model-change path) so a
    DB-registered model that is not in `llm_models.SUPPORTED_MODELS`
    still passes their `is_supported` gate. Callers combine this with
    `is_supported` (OR): pass either check and the submission is
    accepted. Goes through `_fetch_selectable_base_models`, the same
    provider-disabled filter the two dropdown builders use — a model
    belonging to a disabled provider is rejected here too, not just
    hidden from the pickers.

    Returns False on pool exception (logged at WARNING). A False
    result on a real DB blip means the caller's `is_supported`
    fallback is the only remaining line of defence — that's fine
    because that fallback is the pre-cycle-01 behaviour we are
    replacing incrementally.
    """
    if not model_id:
        return False
    try:
        rows = await _fetch_selectable_base_models(pool)
    except Exception as exc:
        logger.warning(
            "dropdowns.is_model_registered_v2: DB read failed — %s", exc,
        )
        return False

    for row in rows:
        if row["id"] == model_id:
            return True
        try:
            expansions = synthesize_all_variants(row)
        except Exception as exc:
            logger.warning(
                "dropdowns.is_model_registered_v2: synth failed for "
                "id=%r — %s", row.get("id"), exc,
            )
            continue
        for exp in expansions:
            if exp["variant_id"] == model_id:
                return True
    return False


async def resolve_model_id(pool_or_conn, model: str | None) -> str | None:
    """Resolve a model string to the `models.wire_id` value its FK columns
    expect (`agents.model_id`, `agent_configs.model_id`,
    `templates.model_id`, etc. all reference `models(wire_id)`).

    A context-window variant (e.g.
    'google-flash-100k/gemini-2.5-flash-100k') has no `models` row of its
    own — it's the same base model at a different size (see
    `management.add_context_window_v2`'s docstring). So this tries a
    direct match first (covers base ids and unsized native/CLI ids like
    'claude-cli/opus'), and if that misses, strips the variant's size
    suffix via `synthesizer.derive_base_id` and matches the recovered
    base id instead — `model_id` then points at the shared base row
    rather than being left `NULL` just because a specific context-window
    size was selected. The exact size is still fully recoverable from
    `model` (the legacy free-text column, untouched by this resolution)
    if anything downstream needs it.

    Returns `None` only when `model` doesn't resolve at all — genuinely
    unregistered. These FK columns are nullable precisely for that case:
    `model` remains the value actually used to run the agent regardless
    of whether `model_id` resolves.

    Accepts either a pool or an open connection (`conn`), since callers
    use this both inside and outside a transaction.
    """
    if not model:
        return None
    direct = await pool_or_conn.fetchval(
        "SELECT wire_id FROM models WHERE wire_id = $1", model,
    )
    if direct:
        return direct
    base_id = derive_base_id(model)
    if base_id is None:
        return None
    return await pool_or_conn.fetchval(
        "SELECT wire_id FROM models WHERE wire_id = $1", base_id,
    )
