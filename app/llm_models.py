"""Supported LLM model whitelist + helpers.

Single source of truth for which `agents.model` strings the control
panel offers. The openclaw fork routes on provider prefix natively
(see openclaw_fork_03122026/src/agents/model-selection.ts), so all
we need on this side is: a curated list, defaults per role, and a
validator.

Adding a model: append to SUPPORTED_MODELS. The fork doesn't need
restarting — model strings are validated at next agent.create /
agents.update call.
"""
from typing import Optional, TypedDict


class ModelEntry(TypedDict):
    id:       str   # e.g. "anthropic/claude-sonnet-4-5"
    label:    str   # human-readable, shown in dropdowns
    provider: str   # "anthropic" | "google" | "claude-cli" | …
    tier:     str   # "fast" | "balanced" | "premium" | "agentic"


SUPPORTED_MODELS: list[ModelEntry] = [
    # Anthropic API (native via ANTHROPIC_API_KEY)
    {"id": "anthropic/claude-haiku-4-5",   "label": "Claude Haiku 4.5",     "provider": "anthropic",  "tier": "fast"},
    {"id": "anthropic/claude-sonnet-4-5",  "label": "Claude Sonnet 4.5",    "provider": "anthropic",  "tier": "balanced"},
    {"id": "anthropic/claude-opus-4-7",    "label": "Claude Opus 4.7",      "provider": "anthropic",  "tier": "premium"},

    # Google (Vertex / Gemini API)
    {"id": "google/gemini-2.5-flash",      "label": "Gemini 2.5 Flash",     "provider": "google",     "tier": "fast"},
    {"id": "google/gemini-2.5-pro",        "label": "Gemini 2.5 Pro",       "provider": "google",     "tier": "premium"},

    # ─── Synthetic context-window variants ─────────────────────────────
    # Each `<base>-{100k,200k,500k,1m}` provider is a fake wrapper around
    # the real upstream model with a configured `contextWindow` used by
    # Pi for compaction/percentage math. Defined in
    # agent_os_openclaw/infra/config/models-synthetic.json. The openclaw
    # resolver swaps id -> upstreamId at API call time so the actual
    # provider receives the real model name. Use these to test
    # compaction behavior at fixed token budgets without touching the
    # global contextTokens cap.
    {"id": "google-flash-50k/gemini-2.5-flash-50k",       "label": "Gemini Flash (50K test)",    "provider": "google-flash-50k",      "tier": "test"},
    {"id": "google-flash-100k/gemini-2.5-flash-100k",     "label": "Gemini Flash (100K test)",   "provider": "google-flash-100k",     "tier": "test"},
    {"id": "google-flash-200k/gemini-2.5-flash-200k",     "label": "Gemini Flash (200K test)",   "provider": "google-flash-200k",     "tier": "test"},
    {"id": "google-flash-500k/gemini-2.5-flash-500k",     "label": "Gemini Flash (500K test)",   "provider": "google-flash-500k",     "tier": "test"},
    {"id": "google-flash-1m/gemini-2.5-flash-1m",         "label": "Gemini Flash (1M test)",     "provider": "google-flash-1m",       "tier": "test"},
    {"id": "google-pro-100k/gemini-2.5-pro-100k",         "label": "Gemini Pro (100K test)",     "provider": "google-pro-100k",       "tier": "test"},
    {"id": "google-pro-200k/gemini-2.5-pro-200k",         "label": "Gemini Pro (200K test)",     "provider": "google-pro-200k",       "tier": "test"},
    {"id": "google-pro-500k/gemini-2.5-pro-500k",         "label": "Gemini Pro (500K test)",     "provider": "google-pro-500k",       "tier": "test"},
    {"id": "google-pro-1m/gemini-2.5-pro-1m",             "label": "Gemini Pro (1M test)",       "provider": "google-pro-1m",         "tier": "test"},
    {"id": "anthropic-sonnet-50k/claude-sonnet-4-5-50k",  "label": "Claude Sonnet 4.5 (50K test)",   "provider": "anthropic-sonnet-50k",  "tier": "test"},
    {"id": "anthropic-sonnet-100k/claude-sonnet-4-5-100k","label": "Claude Sonnet 4.5 (100K test)",  "provider": "anthropic-sonnet-100k", "tier": "test"},
    {"id": "anthropic-sonnet-200k/claude-sonnet-4-5-200k","label": "Claude Sonnet 4.5 (200K test)",  "provider": "anthropic-sonnet-200k", "tier": "test"},
    {"id": "anthropic-sonnet-500k/claude-sonnet-4-5-500k","label": "Claude Sonnet 4.5 (500K test)",  "provider": "anthropic-sonnet-500k", "tier": "test"},
    {"id": "anthropic-sonnet-1m/claude-sonnet-4-5-1m",    "label": "Claude Sonnet 4.5 (1M test)",    "provider": "anthropic-sonnet-1m",   "tier": "test"},
    {"id": "anthropic-opus-100k/claude-opus-4-6-100k",    "label": "Claude Opus 4.6 (100K test)",    "provider": "anthropic-opus-100k",   "tier": "test"},
    {"id": "anthropic-opus-200k/claude-opus-4-6-200k",    "label": "Claude Opus 4.6 (200K test)",    "provider": "anthropic-opus-200k",   "tier": "test"},
    {"id": "anthropic-opus-500k/claude-opus-4-6-500k",    "label": "Claude Opus 4.6 (500K test)",    "provider": "anthropic-opus-500k",   "tier": "test"},
    {"id": "anthropic-opus-1m/claude-opus-4-6-1m",        "label": "Claude Opus 4.6 (1M test)",      "provider": "anthropic-opus-1m",     "tier": "test"},
    {"id": "anthropic-opus48-100k/claude-opus-4-8-100k",  "label": "Claude Opus 4.8 (100K test)",    "provider": "anthropic-opus48-100k", "tier": "test"},
    {"id": "anthropic-opus48-200k/claude-opus-4-8-200k",  "label": "Claude Opus 4.8 (200K test)",    "provider": "anthropic-opus48-200k", "tier": "test"},
    {"id": "anthropic-opus48-500k/claude-opus-4-8-500k",  "label": "Claude Opus 4.8 (500K test)",    "provider": "anthropic-opus48-500k", "tier": "test"},
    {"id": "anthropic-sonnet5-50k/claude-sonnet-5-50k",   "label": "Claude Sonnet 5 (50K test)",     "provider": "anthropic-sonnet5-50k", "tier": "test"},
    {"id": "anthropic-sonnet5-100k/claude-sonnet-5-100k", "label": "Claude Sonnet 5 (100K test)",    "provider": "anthropic-sonnet5-100k","tier": "test"},
    {"id": "anthropic-sonnet5-200k/claude-sonnet-5-200k", "label": "Claude Sonnet 5 (200K test)",    "provider": "anthropic-sonnet5-200k","tier": "test"},
    {"id": "anthropic-sonnet5-500k/claude-sonnet-5-500k", "label": "Claude Sonnet 5 (500K test)",    "provider": "anthropic-sonnet5-500k","tier": "test"},

    # NOTE: claude-cli/* and codex-cli/* providers are intentionally
    # NOT exposed for GO or SHCA selection. CLI providers shell out to
    # local binaries with their own session-resume semantics — they're
    # the right fit for IAs (planner-ia, dev-ia, test-ia, code-review-ia)
    # which run as scripted task workers and can stay hardcoded on
    # claude-cli/sonnet. For chat-shaped agents (GO, SHCA), API
    # providers are simpler to reason about (no local binary, no resume
    # state, cleaner model swaps).
]

# Per-role defaults. The GO stays on a small (<=100K) window so its
# orchestration sessions compact early; SHCAs hold longer human
# conversations, so they default to the 200K-window Flash variant.
DEFAULT_GO_MODEL:   str = "google-flash-100k/gemini-2.5-flash-100k"
DEFAULT_SHCA_MODEL: str = "google-flash-200k/gemini-2.5-flash-200k"

# Max context window (tokens) offered when CREATING a GO. The GO-creation
# dropdown is filtered to models at or under this — see models_within_context().
GO_CREATE_MAX_CONTEXT: int = 100_000


def normalize_model_id(model_id: str) -> str:
    """Bridge unprefixed model IDs (gemini-2.5-flash) to the slash-separated
    form OpenClaw requires (google/gemini-2.5-flash).

    The agent registry's dropdown fetches model IDs from the gateway's
    `models.list` RPC, which on some OpenClaw builds returns the bare
    model name without the provider prefix. Saved as-is, the value would
    fail at `agents.create` time (the gateway silently falls back to a
    default, leaving a mismatch between the agents.model column and the
    actually-loaded model).

    Pass any already-prefixed IDs through unchanged. CLI providers
    (claude-cli/*) are always prefixed in the UI's hardcoded list, so
    they're naturally safe.
    """
    if not model_id or "/" in model_id:
        return model_id
    lc = model_id.lower()
    if lc.startswith("gemini-"):
        return f"google/{model_id}"
    if lc.startswith("claude-"):
        return f"anthropic/{model_id}"
    if lc.startswith("gpt-") or lc.startswith("o1-") or lc.startswith("o3-"):
        return f"openai/{model_id}"
    return model_id  # unknown — let downstream validation surface it


def is_supported(model_id: str) -> bool:
    """True iff `model_id` is in the whitelist. Used to validate
    user-submitted form values before they reach the gateway."""
    if not model_id:
        return False
    return any(m["id"] == model_id for m in SUPPORTED_MODELS)


def get_entry(model_id: str) -> Optional[ModelEntry]:
    """Return the full ModelEntry for `model_id`, or None."""
    for m in SUPPORTED_MODELS:
        if m["id"] == model_id:
            return m
    return None


def label_for(model_id: str) -> str:
    """Display label, falls back to the id itself if unknown."""
    entry = get_entry(model_id)
    return entry["label"] if entry else (model_id or "—")


def provider_for(model_id: str) -> str:
    """Provider prefix (used for tier-aware UI hints)."""
    entry = get_entry(model_id)
    return entry["provider"] if entry else ""


def default_for(role: str) -> str:
    """Default model for a given role string (`go` | `shca`).
    Unknown role falls back to the SHCA default."""
    if role == "go":
        return DEFAULT_GO_MODEL
    return DEFAULT_SHCA_MODEL


# Native-model context windows (synthetic variants derive theirs from the id).
_NATIVE_CONTEXT_WINDOWS: dict[str, int] = {
    "anthropic/claude-haiku-4-5":  200_000,
    "anthropic/claude-sonnet-4-5": 200_000,
    "anthropic/claude-opus-4-7":   200_000,
    "google/gemini-2.5-flash":     1_000_000,
    "google/gemini-2.5-pro":       1_000_000,
}


def context_window_for(model_id: str) -> int:
    """Approximate context window (tokens) for a model id.

    Synthetic `*-50k/100k/200k/500k/1m` ids derive the window from their
    suffix; native models use the explicit map above. Unknown ids are
    treated as large (1M) so a max-window filter EXCLUDES them rather than
    wrongly offering them."""
    if not model_id:
        return 1_000_000
    if model_id in _NATIVE_CONTEXT_WINDOWS:
        return _NATIVE_CONTEXT_WINDOWS[model_id]
    import re
    m = re.search(r"-(\d+)(k|m)\b", model_id.lower())
    if m:
        return int(m.group(1)) * (1_000_000 if m.group(2) == "m" else 1_000)
    return 1_000_000


def models_within_context(max_tokens: int) -> list[ModelEntry]:
    """SUPPORTED_MODELS whose context window is <= max_tokens, preserving
    list order. Used to cap the GO-creation dropdown (gemini + anthropic
    alike) to small-window models."""
    return [m for m in SUPPORTED_MODELS if context_window_for(m["id"]) <= max_tokens]


# Default context-window size pre-selected in the GO model picker. The picker's
# size dropdown still offers every window the chosen base model supports — this
# is the default, not a cap (contrast the legacy GO_CREATE_MAX_CONTEXT filter).
GO_DEFAULT_CONTEXT: int = 100_000


def _variant_size_label(tokens: int) -> str:
    """'100000' -> '100K', '1000000' -> '1M'."""
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}M"
    return f"{tokens // 1_000}K"


def go_model_tree(default_context: int = GO_DEFAULT_CONTEXT) -> dict:
    """Group the synthetic context-window variants into base models + sizes for
    the GO two-dropdown picker (LLM model + context window).

    Returns:
        {
          "bases":    [{"key": <base model id>, "label": <base label>}, ...],  # ordered
          "variants": {<base key>: [{"tokens": int, "label": "100K", "id": <full id>}, ...]},
          "default_base":    <base key>,
          "default_context": <tokens>,
          "default_id":      <full synthetic id for (default_base, default_context)>,
        }

    Only entries whose id carries a -NNk/-NNm size suffix (the synthetic
    variants) are included; native/unsized entries are omitted — a GO always
    runs on a controlled context-window variant. The full synthetic id is what
    the form submits (via a hidden field), so route handlers are unchanged.
    """
    import re

    size_re = re.compile(r"-(\d+)(k|m)$", re.I)
    label_re = re.compile(r"\s*\(\d+\s*[km]\s*test\)\s*$", re.I)

    bases: list[dict] = []
    variants: dict[str, list[dict]] = {}
    for m in SUPPORTED_MODELS:
        model_part = m["id"].split("/")[-1]
        sm = size_re.search(model_part)
        if not sm:
            continue  # native / unsized — not part of the GO picker
        base_key = model_part[: sm.start()]
        tokens = int(sm.group(1)) * (1_000_000 if sm.group(2).lower() == "m" else 1_000)
        if base_key not in variants:
            variants[base_key] = []
            label = label_re.sub("", m["label"]).strip() or base_key
            bases.append({"key": base_key, "label": label})
        variants[base_key].append(
            {"tokens": tokens, "label": _variant_size_label(tokens), "id": m["id"]}
        )
    for entries in variants.values():
        entries.sort(key=lambda v: v["tokens"])

    default_base = bases[0]["key"] if bases else ""
    dm = size_re.search(DEFAULT_GO_MODEL.split("/")[-1])
    if dm:
        default_base = DEFAULT_GO_MODEL.split("/")[-1][: dm.start()]

    default_id = ""
    dv = variants.get(default_base, [])
    hit = next((v for v in dv if v["tokens"] == default_context), None) or (dv[0] if dv else None)
    if hit:
        default_id = hit["id"]

    return {
        "bases": bases,
        "variants": variants,
        "default_base": default_base,
        "default_context": default_context,
        "default_id": default_id,
    }
