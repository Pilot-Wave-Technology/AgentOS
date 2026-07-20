"""Collect-window batching logic (pure functions, no I/O).

- GO threads: explicit window (gather multiple members into one turn).
- SHCA threads: opportunistic (window 0 → drain whatever is already queued).
- A batch runs a turn only if it contains >=1 turn-triggering message
  (user / system_wake). Pure system_inject batches are left queued to ride the
  next turn (so context isn't lost and no empty turn is forced).
"""
from __future__ import annotations

import hashlib

from .config import ChatConfig
from .models import MessageKind, QueuedMessage, ThreadContext


def collect_window_ms(ctx: ThreadContext, cfg: ChatConfig) -> int:
    return cfg.collect_window_go_ms if ctx.is_group else cfg.collect_window_shca_ms


def has_turn_trigger(messages: list[QueuedMessage]) -> bool:
    return any(m.kind.triggers_turn for m in messages)


def first_turn_slice(messages: list[QueuedMessage]) -> list[QueuedMessage]:
    """First-come-first-serve: one triggering message per turn.

    Returns any leading context (system_inject) plus the FIRST turn-triggering
    message (user / system_wake), and stops there. Everything after the first
    trigger is left queued for the next turn — so messages are handled strictly
    in arrival order, one turn each, never folded together.

    Leading injects ride with the first trigger (they're context, not standalone
    turns). A batch with no trigger at all is returned unchanged (it won't run).
    """
    out: list[QueuedMessage] = []
    for m in messages:
        out.append(m)
        if m.kind.triggers_turn:
            break
    return out


def compose_turn_message(messages: list[QueuedMessage]) -> str:
    """Join the batch into one turn input, in order. Each message's `content`
    already carries its own sender attribution (the caller formats GO/SHCA
    envelopes before enqueue), so we just separate them."""
    parts = [m.content for m in messages if m.content and m.content.strip()]
    return "\n\n---\n\n".join(parts)


def batch_idempotency_key(messages: list[QueuedMessage]) -> str:
    """Deterministic key from the batch's member ids. Stable across re-dispatch
    (post-crash) so the reply can be recovered instead of double-run, and used
    as the gateway idempotencyKey."""
    joined = "|".join(sorted(m.id for m in messages))
    return "batch-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
