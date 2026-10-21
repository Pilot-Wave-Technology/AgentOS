"""Supported LLM model whitelist + helpers.

Single source of truth for which `agents.model` strings the control
panel offers. The openclaw fork routes on provider prefix natively
(see agentos-gateway/src/agents/model-selection.ts), so all
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
    # NO PRE-3-SERIES GEMINI. The 2.5 flash and pro ids were here until
    # 2026-10-18. An agent already pinned to one keeps running -- its
    # `agents.model` row is untouched and the gateway rewrites the wire
    # id -- but nothing should be able to CHOOSE one, and a catalogue
    # entry is a choice waiting to be made.
    {"id": "google/gemini-3.8-flash",      "label": "Gemini 3.8 Flash",     "provider": "google",     "tier": "fast"},

    # ─── Synthetic context-window variants ─────────────────────────────
    # Each `<base>-{100k,200k,500k,1m}` provider is a fake wrapper around
    # the real upstream model with a configured `contextWindow` used by
    # Pi for compaction/percentage math. Defined in
    # agent_os_openclaw/infra/config/models-synthetic.json. The openclaw
    # resolver swaps id -> upstreamId at API call time so the actual
    # provider receives the real model name. Use these to test
    # compaction behavior at fixed token budgets without touching the
    # global contextTokens cap.
    # THE 3.8 VARIANTS, AND THEY WERE MISSING. `DEFAULT_GO_MODEL` and
    # `DEFAULT_SHCA_MODEL` below have pointed at
    # `google-flash38-500k/gemini-3.8-flash-500k` since 2026-09-24, and
    # that id was in no catalogue entry -- so every new agent ran on a
    # model the dropdown could not show and a validator could not find.
    #
    # NEW ALIASES, not a repointing of `google-flash-*`, which is the
    # convention the 3.7 entry below records: redefining an existing
    # alias silently moves every agent pointing at it.
    {"id": "google-flash38-50k/gemini-3.8-flash-50k",     "label": "Gemini 3.8 Flash (50K test)",   "provider": "google-flash38-50k",    "tier": "test"},
    {"id": "google-flash38-100k/gemini-3.8-flash-100k",   "label": "Gemini 3.8 Flash (100K test)",  "provider": "google-flash38-100k",   "tier": "test"},
    {"id": "google-flash38-200k/gemini-3.8-flash-200k",   "label": "Gemini 3.8 Flash (200K test)",  "provider": "google-flash38-200k",   "tier": "test"},
    {"id": "google-flash38-500k/gemini-3.8-flash-500k",   "label": "Gemini 3.8 Flash (500K)",       "provider": "google-flash38-500k",   "tier": "test"},
    {"id": "google-flash38-1m/gemini-3.8-flash-1m",       "label": "Gemini 3.8 Flash (1M test)",    "provider": "google-flash38-1m",     "tier": "test"},
    # Gemini 3.7 at a fixed 500K. A NEW ALIAS rather than repointing
    # google-flash-500k, which is the convention anthropic-sonnet5-*
    # already follows -- redefining the existing one would silently
    # move every agent pointing at the 2.5 variant onto 3.7.
    {"id": "google-flash37-500k/gemini-3.7-flash-500k", "label": "Gemini 3.7 Flash (500K)",   "provider": "google-flash37-500k",   "tier": "test"},
    # The four `google-pro-*/gemini-2.5-pro-*` variants were removed on
    # 2026-10-18 rather than re-pointed. They existed to test compaction
    # at fixed budgets, which the 3.8 flash variants above now cover, and
    # re-pointing them to a flash model under a name that says "pro"
    # would be a worse lie than deleting them.
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
    {"id": "anthropic-sonnet5-200k/claude-sonnet-5-200k", "label": "Claude Sonnet 5 (200K)",         "provider": "anthropic-sonnet5-200k","tier": "balanced"},
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

# Per-role defaults. The GO defaults to a small (100K) window so its
# orchestration sessions compact early; SHCAs hold longer human
# conversations, so they default to Claude Sonnet 5 at a 200K window. Both
# are defaults only, not caps — any registered context-window variant is
# a valid explicit choice for either role (the old GO_CREATE_MAX_CONTEXT
# hard cap was removed; the registry's variant system means a larger
# selection is deliberate, not something to guard against).
# NOTE: the canonical templates row 'default-shca' overrides this constant at
# provision time (see set_default_shca_model_sonnet5.sql) — keep them in sync.
# BOTH ON GEMINI 3.8 FLASH AT 500K since 2026-09-24, and both are
# defaults for NEW provisioning only -- a live agent keeps the model it
# holds until somebody re-provisions or switches it.
#
# A planner reads goal_state + code_state + plan previews in one turn,
# so the window is the point; 500K is also what the goal-setup picker
# now preselects, so the form and this fallback agree.
#
# THE TEMPLATE ROWS SAY THE SAME (`default_models_flash38.sql`) and must
# keep saying it: resolution is explicit choice > `templates.model` >
# this constant, so either one left behind is silently dead for half the
# surfaces.
#
# The GO default moved off `gemini-3.7-flash-500k` for a second reason:
# the picker no longer offers that model at all (`go_model_choices.sql`),
# and a default the menu cannot show is a goal landing on something
# nobody chose.
DEFAULT_GO_MODEL:   str = "google-flash38-500k/gemini-3.8-flash-500k"
DEFAULT_SHCA_MODEL: str = "google-flash38-500k/gemini-3.8-flash-500k"


def normalize_model_id(model_id: str) -> str:
    """Bridge unprefixed model IDs (gemini-3.8-flash) to the slash-separated
    form OpenClaw requires (google/gemini-3.8-flash).

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
    "google/gemini-3.8-flash":     1_000_000,
    # THE 2.5 ENTRIES STAY, and that is not an oversight. This is a
    # LOOKUP, not a choice: an agent pinned to 2.5 before 2026-10-18
    # keeps running, and dropping its row here would not move it off the
    # model -- it would just lose its context window and compact against
    # a default. Nothing can SELECT these any more (they are gone from
    # MODELS and from `available_models`); they only answer for what is
    # already pinned.
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


# Default context-window size pre-selected in the GO model picker. The
# picker's size dropdown offers every window the chosen base model
# supports — this is only the default, not a cap.
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
