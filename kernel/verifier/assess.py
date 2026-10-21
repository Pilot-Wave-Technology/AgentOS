"""A second opinion on work whose checks already passed.

WHAT THIS IS FOR. The mechanical verdict is only as good as the checks,
and the checks are written by the same model that did the work. A story
whose guard was `test -f README.md` and whose goal was a bare `pytest`
passes verification with three trivial tests and a service that does
nothing the specification asked for -- and every layer agrees it is
done. That is the failure this exists to catch: not a broken check, but
an honest check that never asked the question.

IT CAN ONLY REFUSE. This is the whole design, and it is worth being
explicit about why.

A model that can PASS work makes verification unreproducible: the same
tree, the same commands, and a different answer on a different day,
decided by something that can be argued with. Worse, a repository can
argue with it -- a prompt in a README gets a vote on whether the work
shipped. So the mechanical checks remain the floor. This agent adds a
second key that can turn only one way: two keys to pass, either key to
refuse.

SILENCE IS ITS DEFAULT, and every ambiguous outcome resolves to
silence. Out of steps, out of time, model unavailable, adapter error,
nothing found -- all accept. Absence of evidence is not evidence, and a
verifier that blocks work when its LLM times out is a verifier nobody
will keep switched on.

WHAT MAKES A REFUSAL LEGITIMATE. Evidence, not opinion. "The tests are
thin" is not a refusal; "POST /orders with quantity 26 returns 201, and
VALIDATION_RULES.md R2 says 1..25" is. The tool surface is the
investigator's -- the same tree, the same offline container, the same
nobody uid -- so anything it claims, it had to run.
"""

from __future__ import annotations

import asyncio
import logging
import time

from kernel.harness.models.quick import available
from kernel.harness.models.types import (CompletionRequest, ToolResult,
                                         make_assistant_tool_call_message,
                                         make_tool_result_message)
from kernel.verifier.investigate import TOOLS as _BASE_TOOLS

logger = logging.getLogger(__name__)

MAX_STEPS = 24          # a stop-loss, not a plan
WALL_CLOCK_S = 420.0

SYSTEM = """You are checking whether finished work actually does what
its story asked. Its automated checks have ALREADY PASSED — you are
here because passing checks and doing the work are different things,
and the checks were written by whoever did the work.

You have the exact tree the checks ran against and a container to run
things in. It is offline.

YOUR ONLY POWER IS TO REFUSE. You cannot approve: the checks already
did that. If the work holds up, call `accept` and say nothing more.

REFUSE ONLY ON EVIDENCE YOU PRODUCED. Run the thing. Call the endpoint.
Read the file the story names and compare it to the behaviour you
observe. "The tests look thin" is not a refusal. "R2 says quantity must
be 1..25 and POST /orders with 26 returned 201" is a refusal, and you
must include the command and its output.

WHAT IS WORTH REFUSING FOR:
  - behaviour the story or its documents specify, that the code does
    not do
  - a rule implemented backwards, off by one, or silently skipped
  - tests that assert nothing, or assert the implementation back to
    itself rather than the requirement
  - a documented interface the work does not actually expose

WHAT IS NOT:
  - style, structure, naming, or how you would have written it
  - missing work nobody asked for
  - anything you could not demonstrate by running it

If you are unsure, accept. A wrong refusal costs a person their
attention and teaches everyone to ignore you."""

TOOLS = [t for t in _BASE_TOOLS if t["name"] != "report"] + [
    {"name": "accept",
     "description": "The work does what the story asked, as far as you "
                    "could determine. Ends your turn; nothing is "
                    "recorded against the work.",
     "parameters": {"type": "object", "properties": {
         "checked": {"type": "string",
                     "description": "One line on what you actually "
                                    "exercised, for the record."}}}},
    {"name": "refuse",
     "description": "The work does not do what the story asked, and you "
                    "can show it. This BLOCKS the story.",
     "parameters": {"type": "object", "properties": {
         "what": {"type": "string",
                  "description": "What the story requires and the work "
                                 "does not do. Precise."},
         "evidence": {"type": "string",
                      "description": "The command you ran and what came "
                                     "back. Without this the refusal is "
                                     "an opinion and will be discarded."},
         "fix": {"type": "string",
                 "description": "The most direct fix, naming files."}},
         "required": ["what", "evidence"]}},
]


async def assess(driver, contract: dict, task: str, plan: str,
                 tree, usage_out: list | None = None,
                 model: str | None = None,
                 changed: str = "") -> dict | None:
    """`{'what','evidence','fix'}` to REFUSE, or None to accept.

    Never raises, and never returns a refusal it cannot evidence.

    USAGE COMES OUT THROUGH `usage_out`, NOT THROUGH THE RETURN VALUE, and
    that is the whole reason it is a parameter rather than a second result.
    This function accepts on failure -- a timeout, a dead adapter, a bug --
    and every one of those paths returns None. A model call that happened
    before the timeout was still billed, so a return-value design would drop
    exactly the spend of the runs that went WORST, quietly making a
    misbehaving assessor look like the cheap one. Appending as we go means
    whatever was spent survives however the call ends.
    """
    # `tree` is `inspect_tree.PodTree` on Kubernetes -- the warm pod
    # the checks ran in -- and `TarTree` on Docker and E0. It used to be
    # the archive itself, which is why the assessor did not run at all
    # on the warm path: `stage.py` sets `_tree_tar` and the warm path
    # does not call `stage`, so the SECOND KEY was silently absent from
    # every verification that used the volume.
    if not (available() and tree is not None):
        return None
    try:
        return await asyncio.wait_for(
            _loop(driver, contract, task, plan, tree,
                  usage_out if usage_out is not None else [], model=model,
                  changed=changed),
            timeout=WALL_CLOCK_S)
    except Exception as e:                                  # noqa: BLE001
        # ACCEPT ON FAILURE, always. A timeout, a dead adapter or a bug
        # in this file must not block delivered work -- the mechanical
        # checks already passed, and this was only ever a second
        # opinion.
        logger.warning("assessor stopped (accepting): %s", e)
        return None


async def _loop(driver, contract, task, plan, tree,
                usage_out, model=None, changed=""):
    from kernel.verifier.inspect_tree import dispatch     # noqa: PLC0415

    # THE ADAPTER FOLLOWS THE MODEL, and this line used to be
    # `GeminiAdapter(ASSIST_MODEL)` -- hardcoded to one vendor. A
    # goal choosing an Anthropic judge would have built a Gemini
    # adapter around a Claude model id and reached an SDK that has
    # never heard of it: a vendor 404 several seconds and one
    # confusing log line later, which is the exact failure
    # `adapter_for_model` was written to remove.
    #
    # `model` is the GOAL's choice (`choice.for_verifier`). None
    # means the deployment default, which is what every caller got
    # before this argument existed.
    from kernel.harness.models import adapter_for_model  # noqa: PLC0415
    from kernel.harness.models import choice             # noqa: PLC0415
    # NAMED, because the ledger needs the same answer the adapter got.
    judged_on = model or choice.default_model()
    adapter = adapter_for_model(judged_on)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            "THE STORY'S TASK:\n" + (task or "(none)")[:8000]
            + "\n\nTHE PLAN ITS AUTHOR REGISTERED:\n" + (plan or "(none)")[:4000]
            # THE WORK FIRST, THE TREE SECOND. Without this the assessor
            # was given a task and a whole repository and left to find
            # the four files the story touched -- on a 2,447-file tree
            # whose listing caps at 400 in alphabetical order, which
            # showed 14 of 488 test files and nothing under `tests/`.
            # The commit bounds the work; the repository does not.
            + ("\n\nWHAT THIS STORY CHANGED (cumulative, from the branch "
               "point):\n" + changed[:8000] if changed else "")
            + "\n\nWHERE YOU ARE: " + str(tree)},
    ]

    started = time.monotonic()
    for step in range(MAX_STEPS):
        response = await adapter.complete(
            CompletionRequest(messages=messages, tools=TOOLS))
        _note_usage(usage_out, response, judged_on)
        tc = response.tool_call
        if tc is None:
            # It spoke instead of acting. Prose is not evidence, so this
            # is an accept -- the one direction silence may resolve to.
            logger.info("assessor: ended in prose, accepting")
            return None
        name, args = tc.get("tool"), tc.get("args") or {}

        if name == "accept":
            logger.info("assessor accepted: %s",
                        str(args.get("checked") or "")[:200])
            return None

        if name == "refuse":
            what = str(args.get("what") or "").strip()
            evidence = str(args.get("evidence") or "").strip()
            if what and evidence:
                logger.info("assessor REFUSED: %s", what[:200])
                return {"what": what[:2000], "evidence": evidence[:4000],
                        "fix": str(args.get("fix") or "")[:2000]}
            # AN UNEVIDENCED REFUSAL IS AN OPINION. Handed back rather
            # than accepted, because the model usually has the evidence
            # and simply did not paste it -- and because a refusal that
            # blocks a story on a feeling is the thing this must never
            # become.
            messages.append(make_assistant_tool_call_message(
                tc, provider_state=response.provider_state))
            messages.append(make_tool_result_message(ToolResult(
                tool_call_id=tc.get("id") or f"a{step}",
                output="refusal rejected: it needs BOTH `what` and the "
                       "`evidence` you produced — the command you ran and "
                       "what came back. Run it, then refuse again, or "
                       "accept.",
                error="", exit_code=0)))
            continue

        output = await dispatch(tree, name, args)
        messages.append(make_assistant_tool_call_message(
            tc, provider_state=response.provider_state))
        messages.append(make_tool_result_message(ToolResult(
            tool_call_id=tc.get("id") or f"a{step}",
            output=output[:30_000], error="", exit_code=0)))
        if time.monotonic() - started > WALL_CLOCK_S - 60:
            break

    # OUT OF STEPS IS AN ACCEPT. The investigator is asked for a forced
    # report here, because it is explaining a refusal that already
    # happened and partial evidence beats none. This is the opposite
    # case: a forced answer from a model that has not finished looking
    # would be a guess, and a guess that can block delivered work.
    logger.info("assessor: out of steps without a finding, accepting")
    return None


def _note_usage(usage_out: list, response, model: str) -> None:
    """One entry per model call, in the shape `step_journal.cost` uses.

    Same keys as the executor's payload so both roles reach the ledger
    through one function. Never raises: this is bookkeeping bolted to the
    side of a verification, and a verification must not fail because a
    counter did.
    """
    try:
        u = getattr(response, "usage", None)
        if u is None:
            return
        usage_out.append({
            # THE MODEL THAT ACTUALLY ANSWERED, not the deployment
            # default. This was the constant `ASSIST_MODEL`, so a goal
            # judging on Anthropic had every token of it ledgered
            # against the default Gemini id -- the cost pipeline keys on
            # this field, so the bill named a model that never ran and
            # the model that did looked free.
            "model": model,
            "input": getattr(u, "input", None) or 0,
            "output": getattr(u, "output", None) or 0,
            "cacheRead": getattr(u, "cacheRead", None) or 0,
            "cacheWrite": getattr(u, "cacheWrite", None) or 0,
        })
    except Exception:                                       # noqa: BLE001
        logger.debug("assessor usage not recorded", exc_info=True)
