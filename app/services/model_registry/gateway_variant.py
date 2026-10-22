"""The id the gateway knows a base model by, for model choices it resolves itself.

The catalog registers a model with the gateway ONLY as its context-window
variants (`google-flash38-1m/gemini-3.8-flash-1m`, ...): the providers payload
is built from `synthesize_all_variants`, and a base id such as
`google/gemini-3.8-flash` is not a provider model there. Agents are always
configured with a variant id, so they resolve. A setting the gateway looks up
by id in its own registry -- the compaction summarizer -- must be served the
same way, or the lookup fails and compaction silently does nothing: every
async compaction no-ops with "configured summarizer model
google/gemini-3.8-flash not found in registry". That is what the base id did
before this was served as a variant.

THE LARGEST WINDOW, deliberately. A summarizer is handed the older part of an
agent's history -- up to most of an agent's window -- and one that cannot hold
it fails exactly when the session needs it. For Gemini 3.8 Flash that is the
1M variant.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.services.model_registry.queries import fetch_active_base_models
from app.services.model_registry.synthesizer import synthesize_all_variants


def largest_variant_id(model_id: str, base_rows: Iterable[Any]) -> Optional[str]:
    """The largest-window variant id of `model_id`, or None.

    None when the model is not an active base model or has no context-window
    variants (a native-only model) -- the caller then serves the id unchanged.
    """
    for row in base_rows:
        if row["id"] != model_id:
            continue
        variants = synthesize_all_variants(row)
        if not variants:
            return None
        return max(variants, key=lambda v: v["context_window"])["variant_id"]
    return None


async def gateway_model_id(pool, model_id: str) -> str:
    """`model_id` as the gateway's registry knows it.

    A base id becomes its largest-window variant; anything else (already a
    variant, or a model without variants) is returned as given.
    """
    base_rows = await fetch_active_base_models(pool)
    return largest_variant_id(model_id, base_rows) or model_id
