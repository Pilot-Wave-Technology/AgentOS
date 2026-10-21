"""Kernel LLM spend into `token_calls`, the one ledger the cost views read.

WHY THE KERNEL WRITES HERE AT ALL. Every cost panel on the dashboard reads
`token_calls`. The executor's spend lived in `kernel.step_journal.cost` and
the verifier's lived nowhere, so both were invisible to every one of them --
and since the assistant and GO rows WERE there, the page looked complete
while reporting the two cheapest spenders. One ledger is the fix; a second
cost table would just be a second thing to reconcile.

IDEMPOTENT BY KEY, NOT BY CARE. Every row carries a `dedupe_key` and the
unique index refuses the second copy. The executor's rows are drained from a
table they already live in, so the drain WILL run twice -- after a crash,
after a backfill, on overlapping ticks -- and a cost report that creeps
upward on its own is one people stop trusting. Correctness is the schema's
job here, not the caller's.

WHAT IS AND IS NOT COUNTED. `input` and `cacheRead` are DISJOINT: the Gemini
adapter subtracts `cached_content_token_count` from `prompt_token_count`
before it reports, and Anthropic's fields are additive already. So they are
written to separate columns and the cost views multiply each by its own
rate. Passing Gemini's raw prompt count through was measured once at 80%
over on a cached step, which is the error that makes caching look like a
regression.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The model string the kernel records is a bare id (`gemini-3.7-flash`).
# `token_calls.provider` is a separate column and every cost view groups on
# it, so a row with the wrong provider joins to no pricing and costs zero --
# which reads as free rather than as broken.
_PROVIDERS = (
    ("gemini", "google"),
    ("claude", "anthropic"),
    ("gpt", "openai"),
)


def provider_for(model: str | None) -> str:
    lowered = (model or "").lower()
    for needle, provider in _PROVIDERS:
        if needle in lowered:
            return provider
    return "unknown"


def usage_of(cost) -> dict | None:
    """The four token counts from a `step_journal.cost` payload, or None.

    None when there is nothing billable. A step that used no tokens is not
    a free row to insert -- it is a row that adds nothing to a sum and a
    line on a report that has to be scrolled past.

    TEXT IS ACCEPTED, not just a dict. `asyncpg` hands `jsonb` back as a
    STRING unless a codec is registered on the connection, and the kernel's
    connections do not register one. Typing this parameter `dict` and
    trusting it cost a backfill that selected all 2,299 rows, rejected every
    one, and reported "0 written" as though there had been nothing to do --
    a silent zero, which is the failure this whole change exists to remove.
    """
    if isinstance(cost, (str, bytes)):
        try:
            cost = json.loads(cost)
        except (TypeError, ValueError):
            return None
    if not isinstance(cost, dict):
        return None
    got = {
        "input": int(cost.get("input") or 0),
        "output": int(cost.get("output") or 0),
        "cache_read": int(cost.get("cacheRead") or 0),
        "cache_write": int(cost.get("cacheWrite") or 0),
        "thinking": int(cost.get("thinking") or 0),
    }
    if not any(got[k] for k in ("input", "output", "cache_read", "cache_write")):
        return None
    got["model"] = cost.get("model")
    return got


_INSERT = """
    INSERT INTO token_calls
        (recorded_at, goal_id, story_id, execution_id, role, agent_id,
         session_key, run_id, provider, model,
         input_tokens, output_tokens, thinking_tokens,
         cache_read_tokens, cache_write_tokens, record_type, dedupe_key)
    VALUES (COALESCE($1, now()), $2, $3, $4, $5, $6,
            $7, $8, $9, $10, $11, $12, $13, $14, $15, 'call', $16)
    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
"""


async def record(conn, *, execution_id: int, role: str, dedupe_key: str,
                 usage: dict, goal_id: str | None, story_id: str | None,
                 at=None) -> bool:
    """One ledger row. True when it was new, False when already counted.

    The caller is told which so a drain can report how much it actually
    moved -- a drain that says "500 rows" every tick when it is really
    inserting none is a drain nobody would notice had broken.
    """
    # A STEP CAN REPORT TOKENS AND NO MODEL, and `token_calls.model` is NOT
    # NULL, so the choice is to name it or to lose it. Naming it keeps the
    # ledger complete and makes the unpriced portion VISIBLE -- 'unknown'
    # joins to no pricing row, so it lands in the coverage view as spend
    # nobody can value. Dropping it would have removed 31 rows from the
    # accounts to keep the report tidy, which is the opposite of adding up.
    model = usage.get("model") or "unknown"
    # `agent_id` is what the cost panels label a row with, and for kernel
    # spend the useful label is the story, not a gateway agent that does not
    # exist. `run_id` carries the execution so the per-run drill-down works
    # off the column the other producers already use for it.
    agent_id = f"{role}-{story_id}" if story_id else f"{role}-{execution_id}"
    try:
        got = await conn.execute(
            _INSERT, at, goal_id, story_id, execution_id, role, agent_id,
            f"kernel:{role}:{execution_id}", str(execution_id),
            provider_for(model), model,
            usage["input"], usage["output"], usage.get("thinking") or 0,
            usage["cache_read"], usage["cache_write"], dedupe_key)
    except Exception as exc:                                # noqa: BLE001
        # LOUD, and the reason is specific. The kernel runs as
        # `agentos_worker`, which held no privilege on this table until the
        # accompanying migration; the failure is `permission denied` inside
        # a tick that swallows exceptions, so the only symptom would be a
        # cost page that stays empty while every log reads clean.
        logger.warning("usage row rejected (execution=%s role=%s): %s",
                       execution_id, role, exc)
        return False
    return got.endswith(" 1")


async def run_scope(conn, execution_id: int) -> tuple[str | None, str | None]:
    """(goal_id, story_id) for a run — the attribution, read once per drain."""
    row = await conn.fetchrow(
        "SELECT goal_id, story_id FROM executions WHERE id = $1", execution_id)
    if row is None:
        return None, None
    return row["goal_id"], row["story_id"]
