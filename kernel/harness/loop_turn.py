"""One turn at the provider: what we send, and what an empty answer means.

THE ONLY PLACE THE LOOP TALKS TO A MODEL, and it is a file of its own
because the two rules here are both easy to break from somewhere else.

RULE ONE, THE PROMPT STABILITY CONTRACT. The request is built in one
statement so that the ordering argument in it cannot be undone by a
caller appending "just one more field". Section 8.3 prices a broken
cacheable prefix at $314 against $60 on the same story.

RULE TWO, AN EMPTY RESPONSE IS A FACT ABOUT THE PROVIDER. It arrives
looking exactly like a model that declined to act, and for the whole of
G3 it was recorded as one -- a story spent twelve legs reading as a
model sitting on its hands. The loop must be able to tell the two apart,
so the distinction is drawn HERE, once, and travels as
`stop_reason == "provider_empty"`.

Do not "simplify" the retry away, and do not raise the count back up
without new evidence -- the constant's own comment records the probe
that set it to zero.
"""

from __future__ import annotations

import logging

from kernel.harness.models.types import CompletionRequest

logger = logging.getLogger(__name__)

# How many times an EMPTY provider response is retried. ZERO, and the
# previous value of 2 was wrong on evidence I already had.
#
# The claim was that retrying recovers it. What actually recovers it is
# the NEXT STEP, whose messages differ; an identical request returns
# nothing every time — probed three times on the same bytes, empty three
# times. So a retry is one more round trip and ~2,500 input tokens to
# learn what the first call said.
#
# Measured cost of getting this wrong: a corpus run with 17 empty steps
# was making 51 API calls to produce nothing.
EMPTY_RESPONSE_RETRIES = 0


async def take_turn(adapter, current_messages: list[dict], tools: list[dict],
                    *, execution_id, generation, step_index):
    """Ask the model once, and say plainly when it answered with nothing.

    `execution_id`, `generation` and `step_index` are carried only so the
    two log lines below can name the step. They were worth threading:
    without them the warning said a provider returned nothing and not
    WHICH request, and the pairs that trigger it are deterministic per
    (prompt, tool-surface) -- so the step number is the only handle
    anyone has on reproducing it.
    """
    # ── Prompt stability contract (§8.3) ─────────────────────────────
    # Stable prefix first: messages (system prompt, conversation
    # history), then tool schemas. Anything that varies per-step
    # placed before them destroys the cacheable prefix and turns
    # every step into a cache miss. Input replay is ~80% of a large
    # story's spend (§8.3) — the difference between $60 and $314 on
    # the same story.
    #
    # THERE IS NO LONGER A VOLATILE TAIL. `budget_hint` used to sit
    # last for exactly this reason; removing it leaves the whole
    # request stable, which is strictly better for caching than
    # carefully ordering a volatile field nothing read.
    # ─────────────────────────────────────────────────────────────────
    request = CompletionRequest(
        messages=current_messages,       # stable prefix — cached
        tools=tools,                     # stable — cached
        # Nothing about the budget goes to the model. The harness
        # keeps its own books; the predicates end the leg. Sending a
        # figure the model cannot act on only made the request's
        # tail volatile, which is the one part a cache wants stable.
    )
    response = await adapter.complete(request)

    # AN EMPTY PROVIDER RESPONSE IS RETRIED, NOT SPENT.
    #
    # Gemini returns zero output tokens and no content for certain
    # (prompt, tool-surface) pairs. It is deterministic per pair and
    # NOT a clean function of anything we could isolate — 17 tools
    # worked, 18 failed, 19 worked. Whatever it is, it is the
    # provider's, and it arrived at the harness as `end_turn` with no
    # tool call, so the loop journalled `no_tool` and a story spent
    # TWELVE legs looking like a model sitting on its hands.
    #
    # Retried with the request unchanged, because that is what
    # actually recovers it, and bounded because a pair that always
    # returns nothing must not become an infinite loop. What is NOT
    # done here is inventing a fallback tool call or a text turn:
    # the model said nothing, and pretending otherwise is how a
    # harness starts making up work.
    attempts = 0
    while (response.stop_reason == "provider_empty"
           and attempts < EMPTY_RESPONSE_RETRIES):
        attempts += 1
        logger.warning(
            "execution %s gen %s step %s: provider returned nothing; "
            "retry %d of %d", execution_id, generation, step_index,
            attempts, EMPTY_RESPONSE_RETRIES)
        response = await adapter.complete(request)
    if response.stop_reason == "provider_empty":
        # NOT a break. The condition is intermittent within a leg —
        # observed recovering by step 40 of the same generation — so
        # ending the leg would throw away work over a hiccup. The
        # step is journalled as `provider_empty` instead, which is
        # the whole point: for the whole of G3 this was
        # indistinguishable from `no_tool`, and a run that spent
        # twelve legs on it read as a model sitting on its hands.
        logger.error(
            "execution %s gen %s step %s: provider returned nothing "
            "%d times for an unchanged request. Journalled as "
            "provider_empty — a provider or tool-surface problem, "
            "not the model declining to act.",
            execution_id, generation, step_index,
            EMPTY_RESPONSE_RETRIES + 1)

    return response
