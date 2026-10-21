"""How big a leg is, and how many a run may take: a goal's choice.

THE SHAPE IS `size_choice` / `models.choice`: a per-goal setting a person
makes on the goal's settings page, FROZEN onto the run at dispatch so
editing the goal changes the NEXT run, never a running one. A run whose
leg size changed mid-flight would make its own journal unreadable --
half its legs cut at one size, half at another.

FROZEN ON THE EXECUTION, because that is what the pod can read. The leg
role has SELECT on `kernel.executions` and nothing on `goals`, so the
conductor (which can read both) copies the choice across once.

None MEANS "NOBODY CHOSE". Only the fields a person actually set are
frozen, so a goal that sets only the leg count leaves the leg size to
the model and the profile, which is what they would have been anyway.

THE REVIEW PHASE IS NOT GOVERNED BY THIS. A review has its own budget
(three legs of 600K, `profiles/story-coding-review`), and a goal that
asked for short work legs did not ask for a review that cannot finish.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The floor `contract_config.MIN_CONTEXT_TOKENS` enforces for the same
# reason: below it a leg reads, runs out and writes nothing.
MIN_LEG_TOKENS = 20_000
# The widest window any supported model offers. A leg larger than its
# model's window is not a bigger leg -- the provider errors instead of
# the harness ending the leg cleanly -- so the pod clamps further, per
# model, at use.
MAX_LEG_TOKENS = 1_000_000
# Room for the reply: `peak_context` does not count output, so a leg
# needs its budget PLUS the model's `max_output`. 65,536 is the largest
# `max_output` any supported model declares.
REPLY_HEADROOM = 65_536
MAX_LEGS = 200

_GOAL_SQL = ("SELECT leg_context_tokens, max_legs FROM goals "
             "WHERE id = $1")


def _int_or_none(raw, name: str) -> int | None:
    text = ("" if raw is None else str(raw)).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def valid(leg_tokens, max_legs) -> tuple[int | None, int | None]:
    """The two settings, normalised; raises with the rule in the message.

    Blank means "the default for the model and the work". `max_legs` may
    be -1 for unbounded; a leg's size may not, because a leg with no
    size never ends.
    """
    tokens = _int_or_none(leg_tokens, "Tokens per leg")
    legs = _int_or_none(max_legs, "Max legs")
    if tokens is not None and not MIN_LEG_TOKENS <= tokens <= MAX_LEG_TOKENS:
        raise ValueError(
            f"Tokens per leg must be between {MIN_LEG_TOKENS:,} and "
            f"{MAX_LEG_TOKENS:,}. Below {MIN_LEG_TOKENS:,} a leg runs out "
            f"before it can write anything.")
    if legs is not None and legs != -1 and not 1 <= legs <= MAX_LEGS:
        raise ValueError(
            f"Max legs must be between 1 and {MAX_LEGS}, or -1 for no limit.")
    return tokens, legs


async def chosen_for_goal(conn, goal_id) -> dict:
    """What a person set on this goal, as frozen-ready fields. {} if nothing.

    NEVER RAISES ON A BAD STORED VALUE: validation belongs at the write,
    and a stored value that no longer validates must not stop the run
    dispatching. It logs and leaves that field to the defaults.
    """
    if not goal_id:
        return {}
    row = await conn.fetchrow(_GOAL_SQL, goal_id)
    if row is None:
        return {}
    out: dict = {}
    try:
        tokens, legs = valid(row["leg_context_tokens"], row["max_legs"])
    except ValueError:
        logger.warning("goal %s has a leg setting that no longer validates "
                       "(%r, %r); the defaults apply", goal_id,
                       row["leg_context_tokens"], row["max_legs"])
        return {}
    if tokens is not None:
        out["context_tokens"] = tokens
    if legs is not None:
        out["max_legs"] = legs
    return out


async def freeze(conn, execution_id: int, choice: dict) -> None:
    """Pin the goal's leg settings onto this run, once, at dispatch.

    `WHERE leg_budget IS NULL` so a second dispatch pass cannot rewrite a
    running run's budget. An empty choice writes nothing: the column
    means "a person set this", not "this is what ran".
    """
    if not choice:
        return
    await conn.execute(
        "UPDATE executions SET leg_budget = $2::jsonb "
        "WHERE id = $1 AND leg_budget IS NULL",
        execution_id, json.dumps(choice))


async def frozen(conn, execution_id: int) -> dict:
    """The pinned settings for this run, read in the pod. {} if none."""
    raw = await conn.fetchval(
        "SELECT leg_budget FROM executions WHERE id = $1", execution_id)
    if not raw:
        return {}
    value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {k: v for k, v in value.items()
            if k in ("context_tokens", "max_legs") and isinstance(v, int)}


def tokens_for(choice: dict, max_context: int) -> int | None:
    """The leg size to run, clamped to what this model can hold."""
    tokens = choice.get("context_tokens")
    if not tokens:
        return None
    ceiling = max(MIN_LEG_TOKENS, max_context - REPLY_HEADROOM)
    if tokens > ceiling:
        logger.info("leg size %s exceeds what a %s-token model can hold "
                    "with room for its reply; using %s", tokens,
                    max_context, ceiling)
        return ceiling
    return tokens
