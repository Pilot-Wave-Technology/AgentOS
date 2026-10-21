"""The request the executor sends Opus 5.5 and Sonnet 5.5 is one they accept.

Both answer 400 to sampling parameters (temperature/top_p/top_k), to
thinking that is disabled or given a budget, and to a forced tool_choice.
A run that sends any of them dies on its first step. Three layers:

  OFFLINE   every machine, every CI run. The `anthropic` package is
            replaced by a stub that records the request and returns a
            realistic reply, so the adapter's whole `complete()` runs --
            request building, the call, reply parsing -- with no SDK and
            no network. Covers a first turn, a second turn that replays
            the first (thinking signature included) and caching on/off.

  SDK       when the SDK is installed: every field the adapter sends must
            exist in the SDK's own `MessageCreateParams`. Catches an
            invented or misspelled field, which the API would refuse.

  LIVE      opt-in, never in CI: AGENTOS_LIVE_ANTHROPIC=1 and a real
            ANTHROPIC_API_KEY. Two real turns per model through the real
            adapter -- a tool call, then its result with the first turn
            replayed. The only layer that proves the API accepts it.
            Spends well under a cent per model.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from types import SimpleNamespace

import pytest

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5"]
REFUSED = ("temperature", "top_p", "top_k", "thinking", "tool_choice")
TOOLS = [{"name": "read", "description": "Read one file.",
          "parameters": {"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": ["path"]}}]
ADAPTER = "kernel.harness.models.anthropic"


# -- the stub SDK ------------------------------------------------------------

def _reply():
    """A realistic 5.5 turn: an (omitted-display) thinking block, then a
    tool call. Thinking text is empty by default on these models; the
    signature is what must survive replay."""
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="thinking", thinking="",
                                 signature="sig-abc"),
                 SimpleNamespace(type="tool_use", id="toolu_1", name="read",
                                 input={"path": "a.py"})],
        usage=SimpleNamespace(input_tokens=900, output_tokens=60,
                              cache_creation_input_tokens=0,
                              cache_read_input_tokens=0,
                              output_tokens_details=None))


def _cut_off_reply():
    """What the API returns when the ceiling is hit mid tool call."""
    return SimpleNamespace(
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="thinking", thinking="",
                                 signature="sig-cut")],
        usage=SimpleNamespace(input_tokens=900, output_tokens=16_000,
                              cache_creation_input_tokens=0,
                              cache_read_input_tokens=0,
                              output_tokens_details=None))


def _stub_sdk(sent: list, replies=None):
    mod = types.ModuleType("anthropic")

    class BadRequestError(Exception):
        pass

    class _Messages:
        async def create(self, **kwargs):
            sent.append(kwargs)
            return (replies or {}).get("next", _reply)()

    class AsyncAnthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    mod.BadRequestError = BadRequestError
    mod.AsyncAnthropic = AsyncAnthropic
    return mod


@pytest.fixture
def stubbed(monkeypatch):
    """The adapter module bound to the stub, whether or not the real SDK
    is installed -- and unbound again afterwards, so no other test sees it."""
    sent: list = []
    replies: dict = {}
    stub = _stub_sdk(sent, replies)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-a-secret")
    monkeypatch.delenv("AGENTOS_CACHE", raising=False)
    had_real = importlib.util.find_spec("anthropic") is not None
    imported_with_stub = False
    if not had_real:
        monkeypatch.setitem(sys.modules, "anthropic", stub)
        sys.modules.pop(ADAPTER, None)
        imported_with_stub = True
    mod = importlib.import_module(ADAPTER)
    monkeypatch.setattr(mod, "anthropic", stub)
    mod.REPLIES = replies          # tests set REPLIES["next"] to vary it
    yield mod, sent
    if imported_with_stub:
        sys.modules.pop(ADAPTER, None)


def _first_turn():
    from kernel.harness.models.types import CompletionRequest
    return CompletionRequest(
        messages=[{"role": "system", "content": "You are an executor."},
                  {"role": "user", "content": "Read a.py"}],
        tools=TOOLS)


def _second_turn(first):
    from kernel.harness.models.types import CompletionRequest
    return CompletionRequest(
        messages=[{"role": "system", "content": "You are an executor."},
                  {"role": "user", "content": "Read a.py"},
                  {"role": "assistant",
                   "tool_calls": [first.tool_call],
                   "provider_state": first.provider_state},
                  {"role": "tool", "tool_call_id": first.tool_call["id"],
                   "content": "print('hello')"}],
        tools=TOOLS)


# -- offline -------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_the_first_turn_carries_nothing_the_model_refuses(stubbed, model):
    mod, sent = stubbed
    out = await mod.AnthropicAdapter(model).complete(_first_turn())

    req = sent[0]
    assert req["model"] == model
    for field in REFUSED:
        assert field not in req, f"the adapter sends {field}"
    # 1M is the standard window on 5.5: the 5-series beta header is not sent.
    assert "anthropic-beta" not in (req.get("extra_headers") or {})
    assert 1024 <= req["max_tokens"] <= 128_000
    assert req["tools"][0]["name"] == "read"
    assert req["tools"][0]["input_schema"]["required"] == ["path"]
    # And the reply is understood: one tool call, ready to run.
    assert out.stop_reason == "tool_call"
    assert out.tool_call == {"tool": "read", "args": {"path": "a.py"},
                             "id": "toolu_1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_the_second_turn_replays_the_first_verbatim(stubbed, model):
    """On the 5.5 models a thinking block is bound to its conversation:
    replay it unchanged, signature and all, or it is dropped or refused."""
    mod, sent = stubbed
    adapter = mod.AnthropicAdapter(model)
    first = await adapter.complete(_first_turn())
    await adapter.complete(_second_turn(first))

    req = sent[1]
    for field in REFUSED:
        assert field not in req
    assistant, result = req["messages"][1], req["messages"][2]
    assert assistant == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "tool_use", "id": "toolu_1", "name": "read",
         "input": {"path": "a.py"}}]}
    assert result == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": "print('hello')"}]}
    # The earlier turns are byte-identical between the two requests:
    # append-only, which is what preserved thinking checks.
    assert req["messages"][0] == sent[0]["messages"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("cache, ttl", [(None, None), ("long", "1h")])
async def test_prompt_caching_marks_the_system_prompt(stubbed, monkeypatch,
                                                      model, cache, ttl):
    mod, sent = stubbed
    if cache:
        monkeypatch.setenv("AGENTOS_CACHE", cache)
    await mod.AnthropicAdapter(model).complete(_first_turn())
    marked = sent[0]["system"][-1]["cache_control"]
    assert marked["type"] == "ephemeral" and marked.get("ttl") == ttl


@pytest.mark.asyncio
async def test_the_offline_layer_needs_no_sdk_and_no_network(stubbed):
    """The point of the stub: this file passes on a machine with no
    `anthropic` package and no key, so it can never be the test that
    quietly does not run."""
    mod, sent = stubbed
    await mod.AnthropicAdapter("claude-opus-5-5").complete(_first_turn())
    assert len(sent) == 1


# -- the reply ceiling (16,000, thinking included) ---------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_a_step_may_reply_with_up_to_sixteen_thousand_tokens(stubbed,
                                                                   model):
    """4,096 cut off thinking + a single file write; 16,000 does not."""
    mod, sent = stubbed
    await mod.AnthropicAdapter(model).complete(_first_turn())
    assert sent[0]["max_tokens"] == 16_000 == mod.REPLY_CEILING


@pytest.mark.parametrize("requested, want", [
    (None, 16_000), ("lots", 16_000), (True, 16_000),
    (10, 1_024), (1_024, 1_024), (8_000, 8_000), (16_000, 16_000),
    (100_000, 16_000),
])
def test_the_ceiling_bounds_what_a_caller_asks_for(stubbed, requested, want):
    mod, _ = stubbed
    assert mod._reply_ceiling(requested) == want


def test_the_ceiling_stays_under_the_sdks_non_streaming_limit(stubbed):
    """The adapter does not stream, and the SDK refuses a non-streaming call
    whose max_tokens implies more than ten minutes at 128K tokens/hour --
    i.e. above ~21,333. Raising the ceiling past that means streaming."""
    mod, _ = stubbed
    assert 60 * 60 * mod.REPLY_CEILING / 128_000 <= 60 * 10


def test_the_real_sdk_accepts_the_ceiling_without_streaming():
    sdk = pytest.importorskip(
        "anthropic", reason="the anthropic SDK is not installed here")
    from kernel.harness.models.anthropic import REPLY_CEILING

    client = sdk.Anthropic(api_key="test-key-not-a-secret")
    client._calculate_nonstreaming_timeout(REPLY_CEILING, None)   # no raise
    with pytest.raises(ValueError):
        client._calculate_nonstreaming_timeout(65_536, None)


@pytest.mark.asyncio
async def test_a_reply_cut_at_the_ceiling_is_said_out_loud(stubbed, caplog):
    """The loop journals it as `no_tool` and carries on; without this line
    nobody learns the ceiling is too low."""
    mod, _ = stubbed
    mod.REPLIES["next"] = _cut_off_reply
    with caplog.at_level("WARNING", logger=ADAPTER):
        out = await mod.AnthropicAdapter("claude-sonnet-5-5").complete(
            _first_turn())
    assert out.stop_reason == "max_tokens" and out.tool_call is None
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "claude-sonnet-5-5" in said and "max_tokens=16000" in said
    assert "output_tokens=16000" in said and "cut off" in said


# -- against the SDK's own request definition --------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_every_field_sent_exists_in_the_sdks_request_type(stubbed, model):
    import typing

    params = pytest.importorskip(
        "anthropic.types.message_create_params",
        reason="the anthropic SDK is not installed here; CI installs it "
               "from requirements.txt")
    # A union of the streaming and non-streaming TypedDicts in SDK 1.x;
    # the adapter does not stream, so the non-streaming one is the type.
    allowed = set(typing.get_type_hints(params.MessageCreateParamsNonStreaming))
    assert {"model", "messages", "max_tokens", "tools"} <= allowed
    # `extra_headers` is a per-request client option, not a body field.
    allowed |= {"extra_headers"}

    mod, sent = stubbed
    adapter = mod.AnthropicAdapter(model)
    first = await adapter.complete(_first_turn())
    await adapter.complete(_second_turn(first))
    for req in sent:
        unknown = set(req) - allowed
        assert not unknown, f"fields the SDK does not define: {unknown}"


# -- live, opt-in --------------------------------------------------------------

LIVE = (os.environ.get("AGENTOS_LIVE_ANTHROPIC") == "1"
        and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()))


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE, reason="live API test: set AGENTOS_LIVE_ANTHROPIC=1 "
                                     "and ANTHROPIC_API_KEY to run")
@pytest.mark.parametrize("model", MODELS)
async def test_live_the_api_accepts_two_real_turns(model):
    pytest.importorskip("anthropic")
    sys.modules.pop(ADAPTER, None)
    from kernel.harness.models.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(model)
    first = await adapter.complete(_first_turn())
    assert first.stop_reason in ("tool_call", "end_turn"), first.stop_reason
    assert first.usage.input, "no usage reported"
    if first.stop_reason != "tool_call":
        pytest.skip(f"{model} answered without calling the tool this time; "
                    "the first request was still accepted")
    second = await adapter.complete(_second_turn(first))
    assert second.stop_reason in ("tool_call", "end_turn", "max_tokens"), \
        second.stop_reason
