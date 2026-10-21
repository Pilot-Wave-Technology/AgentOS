"""Gemini adapter — E5b increment of Stage E.

Real SDK: google-genai (Python).  Already in requirements.txt (google-genai>=1.0.0).
reasoning = "unreported" — Gemini 2.5 Flash does extended thinking but the usage
metadata does not break it out (no thinking_tokens field).  However,
candidates_token_count (mapped to output) INCLUDES thinking tokens — the count
is complete.  budget_basis: measured (no multiplier needed).

LR1: declares reasoning="unreported", budget_basis=measured.
LR2: context overflow -> stop_reason='context_overflow', never retried.
LR3: truncation detection — compares sent prompt size against reported input.
LR4: parallel tool calls split into separate CompletionResponse objects.
LR6: no internal retries — errors propagate.
LR8: no bare except / except Exception above the SDK call.
"""

from __future__ import annotations

import logging
from typing import Optional

from google import genai
from google.genai import types as genai_types

from kernel.harness.usage import normalize_usage, NormalizedUsage
from kernel.harness.models import breaker as _breaker
from kernel.harness.models.retry import call_with_retry
from kernel.tools.dialect import normalise_tool_name
from kernel.harness.models.types import (
    AdapterCapabilities,
    CompletionRequest,
    CompletionResponse,
    ModelAdapter,
)

from kernel.harness.models import prompt_cache
from kernel.harness.models.config import cache_policy

logger = logging.getLogger(__name__)


class GeminiAdapter:
    """Gemini model adapter — reasoning="unreported", budget_basis=measured.

    Thinking is included in candidates_token_count (output) — count is complete.
    """

    def __init__(self, model: str | None = None):
        # NOT a default argument. The plan has recorded gemini-3.7-flash
        # since ES3 cycle-09 and this said gemini-2.5-flash, so every
        # figure Stage G produced was measured on the wrong model and
        # nothing disagreed out loud. See models/config.py.
        from kernel.harness.models.config import for_adapter

        from kernel.harness.models.config import api_key

        cfg = for_adapter("gemini", model)
        self._model = cfg.model
        self.retry = cfg.retry
        self._client = genai.Client(
            api_key=api_key("gemini"),
        )

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def capabilities(self) -> AdapterCapabilities:
        """FROM THE MODEL, not from this class.

        These were constants on the adapter, so `gemini-3.1-flash-lite`
        and `gemini-3.1-pro-preview` reported the same window — and LR7
        derives the leg budget from it, so one of them was budgeted on
        the other's figures. Now that an agent picks its own model, that
        stops being a latent problem and becomes a wrong number.
        """
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
        """Call Gemini API.  One tool call + usage + stop reason out.

        LR2: context overflow caught -> stop_reason='context_overflow'.
        LR6: no internal retries — errors propagate to the harness as steps.
        LR8: no bare except above the SDK call.
        """
        # Build contents for Gemini format — translate neutral tool messages
        # to Gemini's native functionCall/functionResponse parts.
        # provider_state rule: if provider_state["adapter"] == "gemini",
        # re-emit raw unchanged (preserves thought_signature etc.);
        # otherwise build from the projection.
        contents = []
        system_instruction = None
        for m in request.messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_instruction = content
            elif role == "assistant" and m.get("tool_calls"):
                ps = m.get("provider_state")
                if ps and ps.get("adapter") == "gemini" and ps.get("raw"):
                    # Re-emit the provider's own turn verbatim
                    contents.append(ps["raw"])
                else:
                    # Build from projection
                    tc = m["tool_calls"][0]
                    contents.append(
                        genai_types.Content(
                            role="model",
                            parts=[genai_types.Part(
                                function_call=genai_types.FunctionCall(
                                    name=tc.get("tool", ""),
                                    args=tc.get("args", {}),
                                )
                            )],
                        )
                    )
            elif role == "tool":
                # Neutral tool_result → Gemini functionResponse part
                # Use the tool name from the preceding assistant message if available
                tool_name = "tool"
                tc_id = m.get("tool_call_id", "")
                contents.append(
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name=tool_name,
                                response={"result": m.get("content", "")},
                            )
                        )],
                    )
                )
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append(
                    genai_types.Content(
                        role=gemini_role,
                        parts=[genai_types.Part(text=str(content) if content else "")],
                    )
                )

        # Build tool declarations for Gemini format
        tools = None
        if request.tools:
            function_declarations = []
            for t in request.tools:
                function_declarations.append(
                    genai_types.FunctionDeclaration(
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        parameters=t.get("parameters", {}),
                    )
                )
            tools = [genai_types.Tool(function_declarations=function_declarations)]

        # PROMPT CACHE. The system instruction and the tool declarations
        # are byte-identical on every step of every leg, and they are
        # re-sent every time. Cached as one `cachedContents` resource and
        # then REMOVED from the request — sending them as well would pay
        # for both.
        #
        # `cached` is None whenever caching is off, unavailable, or the
        # prompt is below the provider's minimum. That is not an error:
        # the request simply carries them inline, as it always did.
        cached = await prompt_cache.ensure(
            self._client, self._model, system_instruction or "",
            tools, cache_policy())

        fields = {"temperature": 0}
        if cached:
            fields["cached_content"] = cached
        else:
            if system_instruction:
                fields["system_instruction"] = system_instruction
            if tools:
                fields["tools"] = tools
        config = genai_types.GenerateContentConfig(**fields)

        # LR2: context overflow. Fb4b: the CALL is retried, not the leg.
        #
        # A transient 429 used to raise, `_take_leg` advanced the
        # generation, and the next tick paid E5c's 4.6-6.1K to re-orient
        # a conversation that was still perfectly good. Nothing about
        # the request was wrong, so repeating it is the whole fix.
        # Overflow is re-raised untouched below, because LR2 is a
        # predicate and must stay one.
        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )

        try:
            response = await call_with_retry(
                _call, policy=self.retry, model=self._model,
                breaker=_breaker.for_model(self._model, self.retry))
        except Exception as e:
            err_str = str(e).lower()
            if "context" in err_str or "too long" in err_str or "token limit" in err_str:
                return CompletionResponse(
                    stop_reason="context_overflow",
                    usage=NormalizedUsage(),
                )
            raise  # LR8: re-raise non-overflow errors

        # Extract usage
        raw_usage = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            # GEMINI'S prompt_token_count INCLUDES THE CACHED TOKENS.
            #
            # Anthropic's does not — `input_tokens`, `cache_read_input_tokens`
            # and `cache_creation_input_tokens` are additive there, and the
            # normaliser is built to that convention. Passing Gemini's figure
            # through unchanged made `input` and `cacheRead` overlap, and
            # `_sum_step_spend` adds all four fields.
            #
            # Measured: prompt=20,433 cached=16,345 output=12, and Google's
            # own total_token_count says 20,445. We were reporting 36,790.
            # The same request UNCACHED reports 20,444 — so a cached step
            # looked 80% MORE expensive than an uncached one, and every
            # step's spend was inflated by exactly whatever the cache saved.
            #
            # Left unfixed this would have made Fb4c look like a
            # regression: build caching, watch the numbers get worse,
            # revert the thing that was saving the money.
            prompt = getattr(um, "prompt_token_count", None)
            cached = getattr(um, "cached_content_token_count", None)
            fresh = prompt
            if prompt is not None and cached:
                # Clamped: a provider that ever reports cached > prompt
                # would otherwise produce a negative input.
                fresh = max(prompt - cached, 0)
            raw_usage = {
                "promptTokens": fresh,
                "completionTokens": getattr(um, "candidates_token_count", None),
                "totalTokens": getattr(um, "total_token_count", None),
                "cached_tokens": cached,
            }

        usage = normalize_usage(raw_usage) or NormalizedUsage()

        # LR3: truncation detection
        sent_prompt_size = sum(len(str(m.get("content", ""))) for m in request.messages)
        # LR3 COUNTS WHAT THE PROVIDER RECEIVED, NOT WHAT IT BILLED FRESH.
        #
        # `input` is the fresh portion once caching is on; the cached
        # portion arrived too, it was simply not re-processed. Comparing
        # the fresh count alone against what we sent made every cached
        # call look like silent truncation — measured: a cached call
        # reported input=8 against a ~2,500 token prompt and the leg
        # ended `provider_truncated` with the tool call thrown away.
        #
        # LR3 is still doing its job: a provider that really drops the
        # brief reports neither fresh nor cached tokens for it.
        received = ((usage.input or 0) + (usage.cacheRead or 0)
                    + (usage.cacheWrite or 0))
        if usage.input is not None and sent_prompt_size > 0:
            expected_input_tokens = sent_prompt_size // 4
            if expected_input_tokens > 100 and received < expected_input_tokens * 0.5:
                return CompletionResponse(
                    stop_reason="provider_truncated",
                    usage=usage,
                )

        # Extract tool call and any prose the model returned.
        # `content` can be truthy while `parts` is None — Gemini returns that
        # for an empty or blocked turn. Iterating it crashed the adapter and
        # took the whole leg down (TypeError: 'NoneType' is not iterable).
        # A turn that produced nothing is a legitimate step — the loop
        # journals `no_tool` and the predicates carry on — never a crash.
        tool_call = None
        extra_tool_calls: list[dict] = []
        stop_reason = "end_turn"
        text_parts: list[str] = []

        if response.candidates and response.candidates[0].content:
            for part in (response.candidates[0].content.parts or []):
                part_text = getattr(part, "text", None)
                if part_text and not getattr(part, "thought", False):
                    text_parts.append(part_text)
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    call = {
                        "tool": normalise_tool_name(fc.name),
                        "args": dict(fc.args) if fc.args else {},
                        "id": f"gemini-{fc.name}-{id(part)}",
                    }
                    stop_reason = "tool_call"
                    # LR4: collect the rest rather than breaking. Every call in
                    # the turn needs an answer, because LR9 replays the turn
                    # verbatim and an unanswered call corrupts the history.
                    if tool_call is None:
                        tool_call = call
                    else:
                        extra_tool_calls.append(call)

            # Check finish reason
            finish_reason = response.candidates[0].finish_reason
            if finish_reason and str(finish_reason).upper() == "MAX_TOKENS":
                stop_reason = "max_tokens"

        # AN EMPTY PROVIDER RESPONSE IS NOT A MODEL THAT CHOSE TO SAY
        # NOTHING, and conflating them hid a real defect for a whole
        # increment. G4 named a tool `ls`; Gemini then returned zero
        # output tokens and no content for some prompts, deterministically
        # — and because that arrived as `end_turn` with no tool call, the
        # loop journalled `no_tool` and the run looked like a model
        # sitting on its hands for twelve legs.
        #
        # `provider_empty` is not a refusal and not a failure: it says the
        # provider returned nothing at all, which is a fact about the
        # request rather than about the work. Retrying it verbatim gets
        # the same nothing.
        if (tool_call is None and not text_parts
                and stop_reason == "end_turn"
                and not (usage and usage.output)):
            stop_reason = "provider_empty"
            logger.warning(
                "gemini returned no content and no output tokens; the "
                "request produced nothing. This is NOT the model "
                "declining to act — check the tool surface, which is "
                "where an unusable function declaration shows up.")

        # Build provider_state — the raw model Content for replay.
        # This carries thought_signature etc. that Gemini requires on replay.
        raw_content = None
        if response.candidates and response.candidates[0].content:
            raw_content = response.candidates[0].content

        return CompletionResponse(
            tool_call=tool_call,
            text="\n".join(text_parts).strip() or None,
            usage=usage,
            stop_reason=stop_reason,
            provider_state={"adapter": "gemini", "raw": raw_content},
        )

    def get_parallel_tool_calls(self, response) -> list[dict]:
        """Extract all tool calls from a response with parallel calls (LR4)."""
        calls = []
        if response.candidates and response.candidates[0].content:
            for part in (response.candidates[0].content.parts or []):
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    calls.append({
                        "tool": normalise_tool_name(fc.name),
                        "args": dict(fc.args) if fc.args else {},
                        "id": f"gemini-{fc.name}-{id(part)}",
                    })
        return calls
