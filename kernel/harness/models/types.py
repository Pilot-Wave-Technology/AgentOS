"""Model seam types — E5b increment of Stage E.

complete(request) -> Response
request:  messages + tool schemas + budget hint
Response: one tool call + usage + stop reason

Nothing above the adapter knows the vendor; nothing below it knows about
legs, budgets or parking.  Vendor SDKs live inside an adapter and nowhere
else.  Lives in kernel/harness/models/, beside kernel/drivers/.

The seam is Python, not TypeScript.  Everything it touches — the loop,
the predicates, step_journal, proposals — is already Python.  The model
API key is a worker credential; it belongs where the git credential lives,
not in the sandbox tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from kernel.harness.usage import NormalizedUsage


@dataclass(frozen=True)
class CompletionRequest:
    """Input to the model seam."""
    messages: list[dict]
    tools: list[dict]
    # NO BUDGET IS SENT TO THE MODEL. This carries one harness-internal
    # number — how long a reply may be — and nothing about the leg.
    #
    # It replaces `budget_hint`, which was named as though the model
    # read it and never did: neither adapter rendered it into a prompt.
    # Its only consumer was Anthropic's max_tokens. That coupling was
    # itself the bug: remaining_tokens is a LEG budget, and once context
    # and spend were split it went to zero almost immediately, silently
    # clamping every Anthropic reply from 4,096 to the 1,024 floor.
    #
    # A leg near its budget still needs a full-size tool call, so reply
    # length has no business depending on budget at all. None = the
    # adapter's own ceiling.
    max_reply_tokens: Optional[int] = None


@dataclass(frozen=True)
class CompletionResponse:
    """Output from the model seam — one tool call + usage + stop reason.

    provider_state: the provider's own representation of this assistant turn,
    verbatim, tagged with the adapter name. Never durable — stripped before
    journalling, lives in memory for one leg only (E6b rebuilds from brief).
    The loop passes it into the appended message; the adapter re-emits it
    unchanged if adapter names match, builds from the projection otherwise.
    """
    tool_call: Optional[dict] = None  # {"tool": str, "args": dict, "id": str}
    # Prose the model produced in the same turn. Real coding agents narrate and
    # act together — Anthropic returns [{type:"text"},{type:"tool_use"}], Gemini
    # returns parts with both. Dropping it made the journal record `no_tool` for
    # a turn where the model spoke, and left replayed history as a bare sequence
    # of tool calls with no reasoning in it.
    #
    # RULE: text is EVIDENCE, never CONTROL FLOW. It belongs in the journal, the
    # conversation history and the brief. It must never enter the progress
    # clock's hash (LR5) — a stalling model still talks, usually more, so
    # counting prose as progress would mask exactly the stall we watch for.
    text: Optional[str] = None
    # LR4: a turn may contain several tool calls. The harness executes one per
    # step, but every call the provider emitted still needs a result or the
    # next request is malformed — Anthropic rejects with "tool_use ids were
    # found without tool_result blocks immediately after". Because LR9 replays
    # the raw assistant turn verbatim, dropping a call silently is not an
    # option: the dropped call is still in the history. These are the calls we
    # did NOT execute, and the loop answers each with an explicit refusal.
    extra_tool_calls: list = field(default_factory=list)
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    stop_reason: str = "end_turn"
    # stop_reason values: tool_call | end_turn | max_tokens | context_overflow | error
    provider_state: Optional[dict] = None  # {"adapter": str, "raw": <SDK object>}


@dataclass(frozen=True)
class ToolResult:
    """Result of executing a tool — neutral, provider-independent.

    Size bound: output truncated at MAX_TOOL_OUTPUT_SIZE (32 KB).
    E7's artifact store (8 KB inline, artifact id for the rest) refines this
    at ES5; 32 KB is generous enough to not need redesign.
    """
    tool_call_id: str
    output: str          # stdout or structured result, size-bounded
    error: Optional[str] = None  # stderr if failed
    exit_code: int = 0   # 0 = success


MAX_TOOL_OUTPUT_SIZE = 32 * 1024  # 32 KB — refined at E7


def make_assistant_tool_call_message(
    tool_call: dict,
    provider_state: Optional[dict] = None,
    text: Optional[str] = None,
) -> dict:
    """Build a neutral assistant message carrying a tool call.

    Two layers:
    - Projection (neutral): role, tool_calls. Everything above the adapter reads this.
    - provider_state: the provider's raw turn, verbatim. The adapter re-emits it
      unchanged if adapter names match; builds from projection otherwise.
      Never durable — stripped before journalling.
    """
    # `content` carries the model's own narration for this turn. It was None,
    # so any adapter falling back to the projection replayed history as a bare
    # sequence of tool calls — the model could not see its own reasoning from
    # earlier turns. provider_state preserves it for the adapter that produced
    # it; the projection is what every OTHER adapter sees, so it needs the text
    # too or cross-adapter replay silently loses the thread.
    msg = {
        "role": "assistant",
        "tool_calls": [tool_call],
        "content": text,
    }
    if provider_state is not None:
        msg["provider_state"] = provider_state
    return msg


def make_tool_result_message(result: ToolResult) -> dict:
    """Build a neutral tool-result message.

    Provider-independent: each adapter translates this to its native format.
    """
    content = result.output
    if result.error:
        content = f"Error (exit {result.exit_code}): {result.error}\n{result.output}"
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": content,
    }


def normalise_tool_schema(tool: dict) -> dict:
    """Normalise a tool schema to use 'parameters' as the canonical key.

    Anthropic reads input_schema; Gemini reads parameters. A tool declared
    with only input_schema silently degrades to no-parameters on Gemini.
    This normalises to 'parameters' — one key, not a silent difference.
    """
    schema = dict(tool)
    if "input_schema" in schema and "parameters" not in schema:
        schema["parameters"] = schema.pop("input_schema")
    elif "input_schema" in schema and "parameters" in schema:
        # Both present — prefer parameters, drop input_schema
        schema.pop("input_schema")
    return schema


@dataclass(frozen=True)
class AdapterCapabilities:
    """Declared capabilities — the harness reads them and adapts deterministically.

    reasoning: three-valued capability replacing the former reports_reasoning bool.
      "none"       — model does no thinking. thinking is absent/0.
                     input + output + cacheRead + cacheWrite is complete.
                     budget_basis = measured. Multiplier never applied.
      "reported"   — model thinks, provider reports thinking separately.
                     thinking is a real number. budget_basis = measured.
      "unreported" — model thinks, provider does not report it separately.
                     The sub-case is declared by thinking_in_output:
                       True  → thinking included in output, count complete → measured.
                       False → thinking excluded from output, count incomplete → estimated.

    thinking_in_output: meaningful only when reasoning="unreported".
      True  (Gemini) — candidates_token_count includes thinking. Count complete.
      False (no current adapter) — thinking excluded from output. Triggers the
             estimated path + reasoning_multiplier.
      None  (default) — undeclared. The loop raises ValueError at runtime if
             reasoning="unreported" and this is None, forcing every unreported
             adapter to state which sub-case it is. The default is deliberately
             NOT True — an optimistic default would silently undercount spend.
    """
    max_context: int = 200_000
    native_tools: bool = True
    reasoning: str = "none"  # "none" | "reported" | "unreported"
    thinking_in_output: Optional[bool] = None  # must be declared for "unreported"
    prompt_caching: bool = False
    parallel_tool_calls: bool = True
    temperature_zero: bool = True
    leg_token_budget: int = 100_000  # LR7: per-adapter, not a constant


@runtime_checkable
class ModelAdapter(Protocol):
    """The model seam interface — one method, provider-neutral in both directions."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> AdapterCapabilities: ...

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
