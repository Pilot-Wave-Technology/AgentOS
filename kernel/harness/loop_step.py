"""The durable record of one step, and the scrubbing that guards it.

THE JOURNAL IS THE ONLY THING THAT OUTLIVES THE LEG. The conversation
dies at the boundary, the sandbox dies with the pod, and everything the
next leg knows mechanically -- the read-set, the spend, the orientation
floor, whether the run has stalled -- is re-derived from these rows. So
this file is not bookkeeping around the interesting part; for every leg
after the first it IS the interesting part.

FOUR THINGS HERE HAVE EACH COST A DAY, and none of them should be
"simplified":

  the idempotency key      carries execution, generation, depth AND
                           step. Every dimension in it is one that
                           actually varies, and each was added after a
                           collision silently dropped a row -- the
                           insert uses ON CONFLICT DO NOTHING, so a
                           missing dimension fails OPEN. A child runs at
                           its parent's execution and generation, so
                           without `depth` the child's step 0 destroyed
                           the parent's.
  the cost breakdown       records the provider's own total and the
                           characters we can prove we sent, so
                           `ci/check_token_reconciliation.sh` can check
                           the journal against the provider instead of
                           trusting it. Two accounting defects shipped
                           in one day and neither was visible without
                           going back to the API.
  `_as_args`               normalises the model's `args` ONCE, here. A
                           provider can put a JSON string where an
                           object belongs, and every downstream `.get`
                           then raises AttributeError and takes the leg
                           with it.
  `_scrub_json`/`_scrub_text`  Ec4d. A token landing in a journal row
                           outlives the lease that carried it, every
                           rotation, and the workspace it was minted
                           for. Applied to the RECORD and never to the
                           value the tool receives.

`provider_empty` IS ITS OWN TOOL NAME and must stay one. A turn where
the model spoke and called nothing, and a turn where the provider
returned nothing at all, are different facts -- G3's step counts
attributed the second to the model for twelve legs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from kernel.harness.stall import signature as stall_signature


@dataclass(frozen=True)
class Step:
    """One journalled step, as the loop needs it afterwards.

    `record` is what the predicates read; `journal_id` is what the
    artifact path updates `result_ref` on; `tool_name` and `tool_args`
    are the NORMALISED call, handed back so the dispatch below uses the
    same values that were recorded rather than re-reading the raw field.
    """

    journal_id: int
    tool_name: str
    tool_args: dict
    record: dict


def _as_args(tool_call) -> dict:
    """The `args` of a tool call, always a dict.

    The model decides this field's shape and is the least trustworthy
    source of input the harness has. A string here produced `'str'
    object has no attribute 'get'` from whichever reader touched it
    first, which killed the leg rather than being reported back as a bad
    call the model could correct.

    Returning {} rather than raising is deliberate: the tool then fails
    its own argument validation and says WHICH argument it wanted, which
    is a better message than anything this function could write.
    """
    if not tool_call:
        return {}
    args = tool_call.get("args") if isinstance(tool_call, dict) else None
    return args if isinstance(args, dict) else {}


async def record_step(conn, response, *, execution_id: int, generation: int,
                      depth: int, step_index: int,
                      current_messages: list[dict], adapter) -> Step:
    """Write this step to the journal and return what the loop needs back.

    `current_messages` and `adapter` are here only to be MEASURED --
    `sentChars` and the model id -- and neither is mutated. They are the
    two facts about the request that no provider reports back.
    """
    # ── Journal the step ─────────────────────────────────────────────
    cost_dict = None
    if response.usage:
        cost_dict = {
            "input": response.usage.input,
            "output": response.usage.output,
            "cacheRead": response.usage.cacheRead,
            "cacheWrite": response.usage.cacheWrite,
        }
        if response.usage.thinking is not None:
            cost_dict["thinking"] = response.usage.thinking
        # THE PROVIDER'S OWN TOTAL, so the journal can be checked
        # against it afterwards instead of trusted.
        #
        # Two accounting defects shipped in one day and both were
        # invisible in the journal: cached tokens double-counted so a
        # cached step read 80% more expensive than an uncached one,
        # and `input` mistaken for everything received so LR3 called
        # every cached call a truncation. Neither could be seen
        # without going back to the provider. Recording the total
        # makes `ci/check_token_reconciliation.sh` a thing anyone can
        # run over real runs.
        if response.usage.total is not None:
            cost_dict["providerTotal"] = response.usage.total
        # WHAT WE DEMONSTRABLY SENT, so a provider that reports no
        # total can still be audited.
        #
        # Anthropic reports no total at all, so the reconciliation
        # above has nothing to check and honestly says so — every
        # Anthropic step lands in "no provider total" and the run
        # reports NOTHING CHECKED. That is better than a false green,
        # but it leaves one of two providers unverified, and
        # synthesising a total from Anthropic's own parts would only
        # check our arithmetic against our arithmetic.
        #
        # This is independent of the normaliser: it comes from the
        # request the harness built, not from anything the provider
        # said. If a cache field were dropped the way `cacheRead`
        # once was, measured context would collapse far below the
        # bytes we can prove we sent. One-directional by nature —
        # context legitimately EXCEEDS this, because system prompt
        # and tools are not counted here — so only a large shortfall
        # is evidence, and that is exactly the failure being hunted.
        cost_dict["sentChars"] = sum(
            len(str(m.get("content", ""))) for m in current_messages)
        cost_dict["model"] = getattr(adapter, "_model", None)

    seq = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM step_journal "
        "WHERE execution_id = $1",
        execution_id,
    )

    # `provider_empty` is its own journal entry. A turn where the
    # model spoke and called nothing, and a turn where the provider
    # returned nothing at all, are different facts — and G3's step
    # counts attributed the second to the model.
    if response.tool_call:
        tool_name = response.tool_call["tool"]
    elif response.stop_reason == "provider_empty":
        tool_name = "provider_empty"
    else:
        tool_name = "no_tool"
    # NORMALISED ONCE, HERE. A provider can put anything in `args` --
    # a JSON string where an object belongs is the common one -- and
    # every downstream `.get` then raises AttributeError and takes
    # the leg with it. Coercing at the boundary means one place
    # decides what a malformed call is, instead of each reader
    # discovering it separately.
    tool_args = _as_args(response.tool_call)

    # Generation in the key so legs at different generations never collide.
    # Before this fix, step_index restarted at 0 each leg, and
    # ON CONFLICT DO NOTHING silently dropped every row after leg 1.
    #
    # Asymmetry that let this survive ES2/ES3: snapshot() uses a plain
    # INSERT (collision raises loudly), but the journal used ON CONFLICT
    # DO NOTHING with a discarded RETURNING id (collision vanished
    # silently). One path fails closed, the other fails open.
    # depth is IN the key: a child runs at the same execution and
    # generation as its parent, so without it the child's step 0 collides
    # with the parent's and ON CONFLICT drops one of them silently —
    # the same shape as the generation-less bug that survived ES2/ES3.
    idem_key = (f"loop-{execution_id}-gen-{generation}-"
                f"d{depth}-step-{step_index}")

    journal_id = await conn.fetchval(
        """
        INSERT INTO step_journal
            (execution_id, seq, generation, idempotency_key, tool, args,
             cost, model_text, depth)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
        ON CONFLICT (execution_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        execution_id,
        seq,
        generation,
        idem_key,
        tool_name,
        # Ec4d — SCRUBBED ON THE WAY IN. `credentials.scrub` was
        # written as "a last line, not the design" and wired to
        # nothing, so the last line did not exist. The journal is the
        # durable record: a token landing here outlives the lease
        # that carried it, every rotation, and the workspace it was
        # minted for. Arguments are one way one arrives -- a model
        # that read the lease and passed it to `bash` puts it here.
        _scrub_json(tool_args),
        json.dumps(cost_dict) if cost_dict else None,
        # Evidence, not control flow. Recorded on every step, including
        # `no_tool` turns — which is how a turn where the model spoke but
        # called nothing stops looking identical to a turn where nothing
        # happened. Never read by a predicate or the progress clock (LR5).
        #
        # Scrubbed for the same reason as `args` above, and this is
        # the likelier half: what the model SAID is where an echoed
        # token lands, and Stage G watched agents echo tokens through
        # `exec` and `bash` output.
        _scrub_text(response.text),
        depth,
    )

    if journal_id is None:
        raise RuntimeError(
            f"Journal insert failed: idempotency collision on key {idem_key!r}. "
            f"This should not happen with generation in the key — "
            f"execution_id={execution_id}, generation={generation}, step={step_index}."
        )

    # AN EMPTY STEP COSTS TOKENS AND IS NOT A STEP OF WORK.
    #
    # Recorded, so its input tokens count against the token budget —
    # they were spent. Marked, so it does NOT count against the step
    # budget: a run that exhausts its steps on responses the provider
    # never sent has been failed for somebody else's outage, and the
    # comparison then measures who got unlucky rather than whether
    # the tools helped.
    #
    # The `step_budget + 10` overshoot guard still bounds the loop,
    # so a request that is empty for ever cannot spin.
    return Step(
        journal_id=journal_id,
        tool_name=tool_name,
        tool_args=tool_args,
        # H7: what this step DID, normalised. Computed
        # here rather than in the clock so the clock
        # never re-derives it from a mutated record.
        # From `response.tool_call`, which is what is
        # in scope HERE. `tc_args` is unpacked further
        # down the loop and using it read as fine
        # until the first run.
        # tool_args, NOT a fresh read of the raw
        # field: re-reading it here bypassed the
        # normalisation done above and handed
        # signature() a string. Returning it on `Step`
        # is what now makes a second read impossible
        # rather than merely wrong.
        record={"cost": cost_dict, "seq": seq,
                "empty": response.stop_reason == "provider_empty",
                "signature": stall_signature(tool_name, tool_args)
                if tool_name else None},
    )


def _scrub_json(value) -> str:
    """`json.dumps`, with anything credential-shaped replaced.

    Applied to the JOURNAL copy, never to the live value: the tool still
    receives exactly what the model sent. Scrubbing the value itself
    would change behaviour in order to protect a record, which is the
    wrong way round.
    """
    import json as _json

    from kernel.workspace.credentials import scrub

    return scrub(_json.dumps(value))


def _scrub_text(value):
    """The same, for free text. `None` stays `None` -- the column is
    nullable and an empty string is a different fact from no text."""
    from kernel.workspace.credentials import scrub

    return None if value is None else scrub(value)
