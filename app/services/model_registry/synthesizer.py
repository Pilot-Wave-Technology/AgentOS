"""Pure-Python variant synthesis for the hierarchical Model Registry.

Cycle-01 of story `414f92d6` reshapes the app-side registry so a `models`
row carries only its canonical base identity plus an
`available_context_windows` INTEGER[] listing the synthetic sizes the
model supports. There is deliberately no persisted variant table —
synthetic ids like `google-flash-100k/gemini-2.5-flash-100k` are
computed on the fly from the parent base row.

This module owns that computation. It is pure — no I/O, no DB access,
no imports from `management`, `queries`, or `emitter`. Every input
comes from arguments; every output is a plain dict. That lets the
router, the admin-UI grouping helper, unit tests, and any future
GO-model-picker code share one canonical expansion without any wiring
gymnastics.

Wire-key format (mirrors `/opt/agent_os_openclaw/infra/config/models-synthetic.json`):

    <owner>-<provider_shorthand>-<wire_suffix> / <native_model_name>-<wire_suffix>
    ^--- provider_key ---^                       ^--- upstream model name ---^

`provider_shorthand` is hand-picked per base model (Sonnet 4.5 → 'sonnet',
Sonnet 5 → 'sonnet5', Opus 4.6 → 'opus', Opus 4.8 → 'opus48', Flash → 'flash',
Pro → 'pro') and is stored on the `models_hierarchy` extension column. The
helper only reads it — it never derives one, because there is no mechanical
rule from base name to shorthand.
"""
import logging

_log = logging.getLogger(__name__)


def _to_size_suffix(tokens: int) -> tuple[str, str]:
    """Return the (wire, display) size suffix for a token count.

    Powers of 1000 only. Prefer the M form when `tokens` is a whole
    million (so `1_000_000` renders as '1m' / '1M', not '1000k' /
    '1000K'). `bool` is a subclass of `int` in Python; reject it
    explicitly so `_to_size_suffix(True)` cannot slip through as `1`.
    """
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise ValueError(
            f"tokens must be a positive integer, got {tokens!r}"
        )
    if tokens <= 0:
        raise ValueError(
            f"tokens must be a positive integer, got {tokens!r}"
        )
    if tokens % 1_000_000 == 0:
        n = tokens // 1_000_000
        return (f"{n}m", f"{n}M")
    if tokens % 1_000 == 0:
        n = tokens // 1_000
        return (f"{n}k", f"{n}K")
    raise ValueError(
        f"tokens={tokens} is not a power-of-1000 size "
        f"(must be a whole multiple of 1_000)"
    )


def synthesize_variant(
    base_id: str,
    base_label: str,
    provider_shorthand: str,
    context_tokens: int,
    tags: list[str] | None = None,
) -> dict:
    """Expand one (base model, context window) into a wire variant dict.

    Returns a dict with exactly:
        variant_id     e.g. "google-flash-100k/gemini-2.5-flash-100k"
        provider_key   e.g. "google-flash-100k"
        upstream_id    e.g. "google/gemini-2.5-flash" (same as base_id)
        label          e.g. "Gemini 2.5 Flash (100K test)"
        context_window e.g. 100000
        tags           the base model's own tags, passed through
                       unchanged — a context-window variant has the
                       SAME capabilities (reasoning/vision) as its base,
                       it's the same upstream model at a different size.

    Raises ValueError if `provider_shorthand` is empty, `base_id` lacks
    a '/', or `context_tokens` fails `_to_size_suffix`.
    """
    if not provider_shorthand:
        raise ValueError(
            f"provider_shorthand is required to synthesize a variant "
            f"(base_id={base_id!r})"
        )
    if "/" not in base_id:
        raise ValueError(
            f"base_id={base_id!r} must match '<owner>/<model>' "
            f"(missing '/')"
        )
    wire_suffix, display_suffix = _to_size_suffix(context_tokens)

    owner, native_model_name = base_id.split("/", 1)
    provider_key = f"{owner}-{provider_shorthand}-{wire_suffix}"
    variant_id = f"{provider_key}/{native_model_name}-{wire_suffix}"
    label = f"{base_label} ({display_suffix} test)"

    return {
        "variant_id": variant_id,
        "provider_key": provider_key,
        "upstream_id": base_id,
        "label": label,
        "context_window": context_tokens,
        "tags": list(tags or []),
    }


def derive_base_id(variant_id: str) -> str | None:
    """Reverse `synthesize_variant()`: recover the base model id from a
    synthetic variant's wire id, e.g.
    'google-flash-100k/gemini-2.5-flash-100k' -> 'google/gemini-2.5-flash'.

    Used to resolve a variant id to the base model's real `models` row —
    a context-window variant has no row of its own (see
    `management.add_context_window_v2`'s docstring), so anything that
    needs a real FK target for one (`dropdowns.resolve_model_id`) must
    resolve to the base instead.

    The size suffix is always the last '-'-delimited segment of the
    provider id, and that exact suffix is what's appended to the native
    model name in `model_name` — stripping it recovers the native name
    without needing to know the provider_shorthand at all (symmetric with
    `emitter._derive_upstream`, which does the same recovery from
    separately-stored provider_id/model_name columns rather than a single
    id string).

    Returns `None` (not an error) if `variant_id` doesn't look like a
    synthesized variant — no '/', no '-' in the provider prefix, or the
    model_name doesn't actually carry that suffix. A plain base id or an
    unsized native/CLI id (e.g. 'claude-cli/opus') correctly returns
    `None` here; the caller is expected to have already tried resolving
    `variant_id` directly before falling back to this.
    """
    if "/" not in variant_id:
        return None
    provider_id, model_name = variant_id.split("/", 1)
    if "-" not in provider_id:
        return None
    owner = provider_id.split("-", 1)[0]
    size_suffix = provider_id.rsplit("-", 1)[1]
    if not model_name.endswith(f"-{size_suffix}"):
        return None
    native_name = model_name[: -(len(size_suffix) + 1)]
    if not native_name:
        return None
    return f"{owner}/{native_name}"


def synthesize_all_variants(model_row: dict) -> list[dict]:
    """Enumerate every synthetic variant for one base-model row.

    Expects `model_row` to carry the keys populated by
    `management.list_base_models_v2`: `id`, `label`, `provider_shorthand`,
    `available_context_windows`, and optionally `tags` (absent/empty
    treated as no tags — callers not yet passing tags, e.g. some test
    fixtures, still work). Returns `[]` for a native-only model (empty or
    missing `available_context_windows`). Otherwise returns one dict per
    element of the windows list in the same order as the input array
    (which the backfill + `add_context_window_v2` keep sorted ascending).

    A row with windows but NO `provider_shorthand` is skipped with a
    WARNING rather than raised on. `synthesize_variant` (the strict
    single-variant primitive) still raises for that case, and
    `add_context_window_v2` refuses to create the situation in the first
    place — but this is the batch path, called in a loop by
    `emitter.build_providers_payload` with no try/except around it. A
    raise here would abort the whole catalogue build, so ONE malformed
    row would silently drop EVERY provider from the gateway payload
    rather than just itself. Degrading to "skip the one bad row, keep
    the other 26 providers, and say so loudly" is the containable
    failure; taking the catalogue down is not.
    """
    windows = model_row.get("available_context_windows") or []
    if not windows:
        return []
    if not model_row.get("provider_shorthand"):
        _log.warning(
            "Skipping context-window variants for model_id=%r: it has %d "
            "window(s) configured but no provider_shorthand, so no variant "
            "id can be built. Set a short name on the model (admin UI → "
            "Edit model) to restore its variants.",
            model_row.get("id"), len(windows),
        )
        return []
    return [
        synthesize_variant(
            base_id=model_row["id"],
            base_label=model_row["label"],
            provider_shorthand=model_row["provider_shorthand"],
            context_tokens=cw,
            tags=model_row.get("tags"),
        )
        for cw in windows
    ]
