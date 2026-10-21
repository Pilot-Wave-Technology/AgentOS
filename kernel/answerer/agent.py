"""The bounded loop that produces an answer.

FAILURE NEVER HOLDS, and that is the one inversion from the assessor.
The assessor accepts when it breaks, because its silence must not block
delivered work. This breaks the other way for the same underlying
reason: a question is not a control input, so a question that could not
be answered must not cost the story its momentum. Out of steps, out of
time, no model, an exception -- all of them return an answer that says
plainly that there is no answer, and the work carries on.

Which means a flaky answerer degrades to "we could not answer that",
never to a stalled story. Anything else would make asking a question a
risk, and a channel people are afraid to use is not a channel.
"""

from __future__ import annotations

import asyncio
import logging
import time

from kernel.answerer.prompt import SYSTEM, TOOLS
from kernel.harness.models.quick import ASSIST_MODEL, available
from kernel.harness.models.types import (CompletionRequest, ToolResult,
                                         make_assistant_tool_call_message,
                                         make_tool_result_message)
from kernel.verifier.investigate import _env

logger = logging.getLogger(__name__)

MAX_STEPS = 16          # a stop-loss, not a plan
WALL_CLOCK_S = 300.0

# What a person gets when the machinery could not run at all. Said in
# the first person, because it appears on a card under their question
# and "the answerer is unavailable" tells them nothing they can use.
_NO_MODEL = ("I could not look into this — the model that answers "
             "questions about a story is not available right now. The "
             "work has not been touched or paused. Ask again in a "
             "moment, or ask me for the plan and the checks, which I "
             "can read straight off the record.")


def _unanswered(detail: str) -> dict:
    return {"text": _NO_MODEL, "looked_at": "", "hold": None,
            "failed": True, "detail": detail}


async def answer_question(driver, question: str, record: str,
                          tree_tar: bytes, contract: dict,
                          mounts_env: dict | None = None,
                          tree_error: str = "") -> dict:
    """`{'text', 'looked_at', 'hold'}`. `hold` is None unless it stops work.

    Never raises. `hold` is None on every failure path.

    `tree_error` is set when the tree SHOULD have been there and could
    not be built. It is not the same as having no tree, and the model is
    told which — an answer that says "there is no repository to inspect"
    when there is one is a confident falsehood, and the person reading
    it has no way to tell.
    """
    if not available():
        return _unanswered("no model")
    try:
        return await asyncio.wait_for(
            _loop(driver, question, record, tree_tar, contract,
                  mounts_env or {}, tree_error),
            timeout=WALL_CLOCK_S)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("answerer stopped (%s); the run is untouched", e)
        return _unanswered(str(e)[:200])


async def _loop(driver, question, record, tree_tar, contract, mounts_env,
                tree_error=""):
    from kernel.harness.models.gemini import GeminiAdapter
    from kernel.verifier.tarslice import names

    adapter = GeminiAdapter(ASSIST_MODEL)
    # A story can be asked about before it has ever built a tree -- while
    # it is still planning, or parked on its very first approval. The
    # record alone is a perfectly good basis for an answer then, so an
    # empty tree loses the file tools and nothing else.
    tree_names = sorted(names(tree_tar)) if tree_tar else []
    extra_env, prepend = _env(mounts_env)
    image = contract.get("base_digest") or contract.get("base_image")
    # THE TAR BACKEND, DELIBERATELY, even where a verification pod
    # exists. This agent is NOT verification: it answers a question
    # about a run from the record and a throwaway copy of the tree, in
    # any state -- PENDING, PARKED, VERIFYING -- and most of those have
    # no pod at all. `deliver.py` is explicit that it "never touches the
    # sandbox", which is what lets it answer a run that has not started.
    #
    # The three tools are the same three; only where they run differs.
    from kernel.verifier.inspect_tree import TarTree, dispatch
    tree = TarTree(driver, tree_tar, tree_names, image,
                   prepend=prepend, env=extra_env)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            "WHAT IS KNOWN ABOUT THIS STORY:\n\n" + record[:24000]
            + ("\n\nYou also have the tree, "
               + str(len(tree_names)) + " files, in a throwaway container."
               if tree_names else
               # THE THREE CASES ARE NOT THE SAME, and saying the wrong
               # one is a confident falsehood a person cannot check.
               ("\n\nTHE TREE COULD NOT BE READ — this is a FAULT on our "
                "side (" + tree_error + "), not an empty workspace. There "
                "may well be code; you simply cannot see it. Say exactly "
                "that: you could not read the tree, so you can only "
                "answer from the record below. Do NOT say the code does "
                "not exist."
                if tree_error else
                "\n\nTHERE IS NO TREE YET — nothing has been written. "
                "Answer from the record above, and say so if the question "
                "is about code that does not exist yet."))
            + "\n\n---\n\nTHE QUESTION:\n\n" + (question or "").strip()},
    ]

    started = time.monotonic()
    for step in range(MAX_STEPS):
        response = await adapter.complete(
            CompletionRequest(messages=messages, tools=TOOLS))
        tc = response.tool_call
        if tc is None:
            # It answered in prose instead of calling `answer`. The prose
            # IS the answer, so it is taken rather than discarded -- and
            # prose cannot hold, which is the safe direction.
            said = (response.text or "").strip()
            if said:
                logger.info("answerer: answered in prose")
                return {"text": said[:8000], "looked_at": "", "hold": None}
            return _unanswered("empty response")

        name, args = tc.get("tool"), tc.get("args") or {}

        if name == "answer":
            text = str(args.get("text") or "").strip()
            if text:
                logger.info("answerer: answered, work untouched")
                return {"text": text[:8000],
                        "looked_at": str(args.get("looked_at") or "")[:1000],
                        "hold": None}
            return _unanswered("answer with no text")

        if name == "hold":
            text = str(args.get("text") or "").strip()
            decision = str(args.get("decision") or "").strip()
            if text and decision:
                logger.info("answerer HELD the run: %s", decision[:200])
                return {
                    "text": text[:8000],
                    "looked_at": str(args.get("looked_at") or "")[:1000],
                    "hold": {"decision": decision[:1000],
                             "options": str(args.get("options") or "")[:2000],
                             "why_now": str(args.get("why_now") or "")[:1000]},
                }
            # A HOLD WITH NOTHING TO DECIDE IS A STALL. Handed back once
            # rather than honoured: stopping a story and then showing a
            # person a card with no question on it wastes their attention
            # and leaves them no way to release it except guessing.
            messages.append(make_assistant_tool_call_message(
                tc, provider_state=response.provider_state))
            messages.append(make_tool_result_message(ToolResult(
                tool_call_id=tc.get("id") or f"q{step}",
                output="hold rejected: it needs BOTH the `text` of your "
                       "answer and the `decision` the person must now "
                       "make, as one plain question. If there is nothing "
                       "for them to decide, call `answer` instead and let "
                       "the work carry on.",
                error="", exit_code=0)))
            continue

        output = await dispatch(tree, name, args)
        messages.append(make_assistant_tool_call_message(
            tc, provider_state=response.provider_state))
        messages.append(make_tool_result_message(ToolResult(
            tool_call_id=tc.get("id") or f"q{step}",
            output=output[:30_000], error="", exit_code=0)))
        if time.monotonic() - started > WALL_CLOCK_S - 45:
            break

    # OUT OF STEPS. Whatever it had found, it never committed to an
    # answer, and a forced one from a model still looking is a guess.
    logger.info("answerer: out of steps without an answer")
    return {"text": "I ran out of room to look into this properly, so I "
                    "would rather not guess. The work has not been touched "
                    "or paused. Narrow the question and ask again — a "
                    "question about one file or one behaviour is one I can "
                    "answer.",
            "looked_at": "", "hold": None, "failed": True,
            "detail": "step budget"}
