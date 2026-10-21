"""Anthropic adapter — E5b increment of Stage E.

Real SDK: anthropic (Python).  Already in requirements.txt (anthropic>=0.40.0).
reasoning = "reported" — thinking tokens are in
output_tokens_details.thinking_tokens, normalised by normalize_usage().
budget_basis: measured (thinking is a separate number, count is complete).

LR1: declares reasoning="reported", budget_basis=measured.
LR2: context overflow -> stop_reason='context_overflow', never retried.
LR3: truncation detection — compares sent prompt size against reported input.
LR4: parallel tool calls split into separate CompletionResponse objects.
LR6: no internal retries — errors propagate.
LR8: no bare except / except Exception above the SDK call.
"""

from __future__ import annotations

import logging
from typing import Optional

import anthropic

from kernel.harness.usage import normalize_usage, NormalizedUsage
from kernel.harness.models import breaker as _breaker
from kernel.harness.models.retry import call_with_retry
from kernel.harness.models.config import cache_policy
from kernel.harness.models import registry as _registry
from kernel.harness.models.types import (
    AdapterCapabilities,
    CompletionRequest,
    CompletionResponse,
    ModelAdapter,
)
from kernel.tools.dialect import normalise_tool_name

logger = logging.getLogger(__name__)

# THE CEILING ON ONE REPLY, thinking included.
#
# It was 4,096 on the reasoning that "a reply longer than that is a model
# writing prose instead of calling something". That stopped being true
# when thinking became always-on: Sonnet 5.5 (effort high by default) and
# Opus 5.5 count their thinking against max_tokens, and one `write` of a
# 370-line file is ~4K tokens on its own. A reply cut there drops the tool
# call it was making, and the step is spent. Gemini's adapter sets no
# ceiling at all (65,536).
#
# 16,000 AND NOT 65,536, because this adapter does not stream: the SDK
# refuses a non-streaming call whose max_tokens implies more than ten
# minutes (above ~21,333 -- `_calculate_nonstreaming_timeout`), and a
# reply that long belongs on a streamed request anyway. Raising it past
# that means streaming first.
#
# A ceiling, not a spend: only generated tokens are billed.
REPLY_CEILING = 16_000
REPLY_FLOOR = 1_024


def _reply_ceiling(requested) -> int:
    """How many tokens the reply may use.

    NOT derived from the leg budget any more. It used to be, and that
    was wrong twice over: a leg near its budget still needs a full-size
    tool call, and after context and spend were split the figure it read
    went to zero within a couple of steps — clamping every reply to the
    floor for the rest of the run, invisibly, because a short reply looks
    like a terse model.

    Bounded below so a caller cannot ask for a fragment, and above at
    REPLY_CEILING (see there). None, or anything not an int, takes the
    ceiling.
    """
    if not isinstance(requested, int) or isinstance(requested, bool):
        return REPLY_CEILING
    return min(max(requested, REPLY_FLOOR), REPLY_CEILING)


class AnthropicAdapter:
    """Anthropic model adapter — reasoning="reported", budget_basis=measured."""

    def __init__(self, model: str | None = None):
        from kernel.harness.models.config import for_adapter

        from kernel.harness.models.config import api_key

        cfg = for_adapter("anthropic", model)
        self._model = cfg.model
        self.retry = cfg.retry
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key("anthropic"),
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """FROM THE MODEL. Opus, Sonnet and Haiku are not the same seam."""
        from kernel.harness.models.registry import resolve

        spec = resolve(self._model)
        return AdapterCapabilities(
            max_context=spec.max_context,
            native_tools=spec.native_tools,
            reasoning=spec.reasoning,
            thinking_in_output=spec.thinking_in_output,
            prompt_caching=True,
            parallel_tool_calls=True,
            temperature_zero=spec.temperature_zero,
            leg_token_budget=spec.leg_token_budget,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Call Anthropic API.  One tool call + usage + stop reason out.

        LR2: context overflow is caught and returned as stop_reason='context_overflow'.
        LR6: no internal retries — errors propagate to the harness as steps.
        LR8: no bare except above the SDK call.
        """
        # Build messages — Anthropic expects system separately, and uses
        # native tool_use/tool_result content blocks (not the neutral format).
        #
        # provider_state rule: if provider_state["adapter"] == "anthropic",
        # re-emit raw unchanged (preserves thinking signatures etc.);
        # otherwise build from the projection.
        system_msg = ""
        messages = []
        for m in request.messages:
            if m.get("role") == "system":
                system_msg = m.get("content", "")
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                ps = m.get("provider_state")
                if ps and ps.get("adapter") == "anthropic" and ps.get("raw"):
                    # Re-emit the provider's own turn verbatim
                    messages.append(ps["raw"])
                else:
                    # Build from projection
                    tc = m["tool_calls"][0]
                    messages.append({
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tc.get("id", "tc-unknown"),
                                "name": tc.get("tool", ""),
                                "input": tc.get("args", {}),
                            }
                        ],
                    })
            elif m.get("role") == "tool":
                # Neutral tool_result → Anthropic tool_result content block
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": m.get("content", ""),
                        }
                    ],
                })
            else:
                messages.append(m)

        # Build tool schemas for Anthropic format
        tools = []
        for t in request.tools:
            tools.append({
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", t.get("input_schema", {})),
            })

        kwargs = {
            "model": self._model,
            # `remaining_tokens` is "unbounded" when the leg budget is
            # -1, and max("unbounded", 1024) is a TypeError — introduced
            # with the -1 feature and caught by reading the one adapter
            # that reads this field. Unbounded means "do not let the
            # budget shrink the reply", so it takes the ceiling.
            "max_tokens": _reply_ceiling(request.max_reply_tokens),
            "messages": messages,
        }
        if system_msg:
            kwargs["system"] = system_msg
        if tools:
            kwargs["tools"] = tools

        # PROMPT CACHE. Anthropic's cacheable prefix is ordered
        # `tools` -> `system` -> `messages`, and a breakpoint caches
        # everything up to AND INCLUDING the block it sits on. So one
        # marker on the last system block caches the tool declarations
        # too — the same scope Gemini's cachedContents holds, reached by
        # a completely different mechanism.
        #
        # The conversation is deliberately NOT cached here. Anthropic
        # can do it, by moving a breakpoint onto the last message each
        # turn, and it is the only thing that would touch the replay
        # this project actually pays for. It is not free — writes are
        # billed at a premium and a moving breakpoint writes every turn
        # — so it gets measured before it gets built, not assumed.
        policy = cache_policy()
        if policy.enabled and system_msg:
            blocks = ([{"type": "text", "text": system_msg}]
                      if isinstance(system_msg, str) else list(system_msg))
            if blocks:
                marked = dict(blocks[-1])
                marked["cache_control"] = {
                    "type": "ephemeral",
                    **({"ttl": "1h"} if policy.retention == "long" else {}),
                }
                kwargs["system"] = blocks[:-1] + [marked]

        # LR2: context overflow is a specific error type
        try:
            # THE HEADER, or the window in the registry is a lie.
            #
            # Sent as `extra_headers` on the ORDINARY endpoint rather
            # than via `beta.messages.create`. One call path, not two:
            # the beta client is a different method, and three of E5b's
            # leg-resilience tests stub `messages.create` — so the beta
            # path silently bypassed the stub and LR2, LR3 and LR6 all
            # went green against a call that never happened. A second
            # path is a second thing every test has to know about.
            #
            # opus-5 and sonnet-5 have a 1M context ONLY with
            # `context-1m-2025-08-07`. Recording 1M in the registry and
            # not sending it would budget every leg at five times what
            # the request can actually carry, and the failure would
            # arrive mid-leg as `prompt is too long` — which the harness
            # would read as context_overflow and blame on the work.
            betas = _registry.resolve(self._model).beta_headers
            if betas:
                kwargs["extra_headers"] = {
                    **kwargs.get("extra_headers", {}),
                    "anthropic-beta": ",".join(betas),
                }
            # Fb4b: the CALL is retried, not the leg. See the twin
            # comment in gemini.py — one policy, both providers, because
            # a fault handled by one vendor's SDK and not the other is
            # not handled.
            async def _call():
                return await self._client.messages.create(**kwargs)

            response = await call_with_retry(
                _call, policy=self.retry, model=self._model,
                breaker=_breaker.for_model(self._model, self.retry))
        except anthropic.BadRequestError as e:
            if "context" in str(e).lower() or "too long" in str(e).lower():
                return CompletionResponse(
                    stop_reason="context_overflow",
                    usage=NormalizedUsage(),
                )
            raise  # LR8: re-raise non-overflow errors

        # Extract usage
        raw_usage = {}
        if hasattr(response, "usage") and response.usage:
            raw_usage = {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
            }
            # Anthropic extended-thinking: thinking tokens under output_tokens_details
            if hasattr(response.usage, "output_tokens_details"):
                otd = response.usage.output_tokens_details
                if otd and hasattr(otd, "thinking_tokens"):
                    raw_usage["output_tokens_details"] = {
                        "thinking_tokens": otd.thinking_tokens,
                    }

        usage = normalize_usage(raw_usage) or NormalizedUsage()

        # LR3: truncation detection.
        #
        # COUNTS WHAT THE PROVIDER RECEIVED, not what it billed fresh.
        # Anthropic reports cache reads separately too, so once
        # `cache_control` is on, `input_tokens` is only the delta — and
        # comparing that alone against what we sent reads every cached
        # call as silent truncation. Measured on Gemini first: a cached
        # call reported input=8 against a ~2,500 token prompt, ended
        # `provider_truncated`, and threw away a good tool call.
        #
        # AND THE WRITE TURN TOO, which is Anthropic-specific: its
        # `input_tokens` excludes tokens written to a cache as well as
        # tokens read from one, so the turn that creates a cache reports
        # the prefix under `cache_creation_input_tokens` and nowhere
        # else. Omitting it here is the same defect as omitting the read
        # — latent today only because this comparison measures messages
        # while we cache system-plus-tools, and live the moment a
        # conversation-prefix breakpoint lands.
        sent_prompt_size = sum(len(str(m.get("content", ""))) for m in request.messages)
        received = ((usage.input or 0) + (usage.cacheRead or 0)
                    + (usage.cacheWrite or 0))
        if usage.input is not None and sent_prompt_size > 0:
            # rough token estimate: 4 chars per token
            expected_input_tokens = sent_prompt_size // 4
            if expected_input_tokens > 100 and received < expected_input_tokens * 0.5:
                return CompletionResponse(
                    stop_reason="provider_truncated",
                    usage=usage,
                )

        # Extract tool calls. The first is executed this step; the rest are
        # surfaced so the loop can answer them (LR4) — Anthropic requires a
        # tool_result for EVERY tool_use in the turn, and LR9 replays the turn
        # verbatim, so an unanswered call makes the next request invalid.
        tool_call = None
        extra_tool_calls: list[dict] = []
        stop_reason = "end_turn"

        if response.stop_reason == "tool_use":
            stop_reason = "tool_call"
            for block in response.content:
                if block.type != "tool_use":
                    continue
                call = {
                    "tool": normalise_tool_name(block.name),
                    "args": block.input if isinstance(block.input, dict) else {},
                    "id": block.id,
                }
                if tool_call is None:
                    tool_call = call
                else:
                    extra_tool_calls.append(call)
        elif response.stop_reason == "max_tokens":
            stop_reason = "max_tokens"
            # SAID OUT LOUD. The loop journals this as a `no_tool` step
            # and carries on, so a reply cut at the ceiling -- usually a
            # tool call that never arrived -- was otherwise invisible.
            # This line is how anyone learns REPLY_CEILING is too low.
            logger.warning(
                "%s reply hit max_tokens=%s (output_tokens=%s); any tool "
                "call it was making was cut off and not run",
                self._model, kwargs.get("max_tokens"),
                getattr(getattr(response, "usage", None), "output_tokens", None))

        # Build provider_state — the raw assistant turn for replay
        raw_assistant = {
            "role": "assistant",
            "content": [
                {"type": b.type, **(
                    {"text": b.text} if b.type == "text" else
                    {"id": b.id, "name": b.name, "input": b.input if isinstance(b.input, dict) else {}} if b.type == "tool_use" else
                    {"thinking": getattr(b, "thinking", ""), "signature": getattr(b, "signature", "")} if b.type == "thinking" else
                    {}
                )}
                for b in response.content
            ] if response.content else [],
        }

        # The model's narration for this turn. `thinking` blocks are excluded:
        # they are provider-internal reasoning, replayed verbatim through
        # provider_state (LR9), not what the model is telling us.
        text_blocks = [
            b.text for b in (response.content or [])
            if b.type == "text" and getattr(b, "text", None)
        ]

        return CompletionResponse(
            tool_call=tool_call,
            text="\n".join(text_blocks).strip() or None,
            extra_tool_calls=extra_tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            provider_state={"adapter": "anthropic", "raw": raw_assistant},
        )

    def get_parallel_tool_calls(
        self, response_content: list,
    ) -> list[dict]:
        """Extract all tool calls from a response with parallel calls (LR4).

        Returns a list of tool_call dicts, each with a unique id for
        idempotency key generation.
        """
        calls = []
        for block in response_content:
            if hasattr(block, "type") and block.type == "tool_use":
                calls.append({
                    "tool": normalise_tool_name(block.name),
                    "args": block.input if isinstance(block.input, dict) else {},
                    "id": block.id,
                })
        return calls
