"""The investigator: an agent that finds out WHY a claim was refused.

THE VERDICT IS ALREADY WRITTEN when this runs, and nothing here can
touch it. What changed since the one-shot explainer: instead of being
handed a log and asked to phrase it, the model gets the same evidence a
person debugging the refusal would want -- the exact tree the checks
ran against, and a sandbox to poke it in -- and works the question
properly. No plans, no approvals, no output budget: it reads, runs,
and reports, bounded only by a step count and a wall clock, which are
stop-losses rather than plans.

THE SANDBOX IS THE CRIMINAL'S CELL, NOT A WORKSHOP. `run` executes in
the same image, same offline network, same nobody uid as the failed
criterion, against the same prepared tree -- so what the investigator
observes is what the check saw, and nothing it does can fix, fetch, or
leak anything. A hostile prompt buried in the repo can waste the
investigator's steps; it cannot move the verdict, which was decided
before the model was invoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from kernel.harness.models.quick import available
from kernel.harness.models.types import (CompletionRequest, ToolResult,
                                         make_assistant_tool_call_message,
                                         make_tool_result_message)

logger = logging.getLogger(__name__)

MAX_STEPS = 20          # a stop-loss, not a plan
WALL_CLOCK_S = 360.0
RUN_TIMEOUT_S = 60.0

TOOLS = [
    {"name": "list_files",
     "description": "List files in the verified tree, optionally under a "
                    "prefix. Paths are relative to the tree root.",
     "input_schema": {"type": "object", "properties": {
         "prefix": {"type": "string"}}, "required": []}},
    {"name": "read_file",
     "description": "Read one file from the EXACT tree the checks ran "
                    "against (first 20KB).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "run",
     "description": "Run a shell command in the same container the "
                    "failed check used: same image, offline, same "
                    "unprivileged user, cwd at the tree root. Use it to "
                    "re-run the failing check with more verbosity, list "
                    "installed packages, or inspect the environment.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
    {"name": "report",
     "description": "Finish: your findings, for the author and the "
                    "assistant driving the story.",
     "input_schema": {"type": "object", "properties": {
         "why": {"type": "string",
                 "description": "What actually went wrong, precisely."},
         "fix": {"type": "string",
                 "description": "The most direct fix, naming files and "
                                "tools. The environment comes from "
                                "committed manifests + env_setup."},
         "category": {"type": "string",
                      "enum": ["work-bug", "environment", "check-bug",
                               "infrastructure"]}},
         "required": ["why", "fix", "category"]}},
]

SYSTEM = """You are the verification investigator inside an agent
platform. An automated verifier REFUSED a completion claim. The verdict
is final and is not yours to change -- your job is to find out exactly
why it failed and report it so the author can fix it in one move.

You have the exact tree the checks ran against and a sandbox identical
to the one the check failed in. Investigate like an engineer: re-run
the failing command, read the files it names, check the environment.
Then call `report` once, with a precise why, a direct fix, and a
category -- you have a bounded number of tool calls, so report as soon
as the evidence is conclusive rather than exploring further. Prefer
evidence you gathered over guesses. Note: the
environment is built from committed manifests (requirements.txt,
package-lock.json, pnpm-lock.yaml) or a committed .agentos/setup.sh via
the `env_setup` tool; installs through the shell or downloads during
tests never reach verification.

KNOW YOUR OWN CELL: your `run` sandbox has NO NETWORK. Any `pip
install` or `npm install` you attempt there will fail with "no matching
distribution" or a fetch error REGARDLESS of whether the versions
exist -- that failure is evidence about the sandbox, never about the
manifest. Judge dependency availability only from the environment
prepare report included in the refusal context (it ran WITH network),
or say plainly that you could not determine it."""


def _env(mounts_env: dict) -> tuple[dict, str]:
    prepends, env = [], {}
    for at in sorted(mounts_env or {}):
        env.update(mounts_env[at].get("env") or {})
        prepends += mounts_env[at].get("prepend") or []
    return env, ":".join(dict.fromkeys(prepends))


async def investigate(driver, contract: dict, task: str, entries: list,
                      tree, model: str | None = None) -> dict | None:
    """{'why','fix','category'} or None. Never raises; never decides.

    `model` is the GOAL's choice (`choice.judge_for`), and it was not a
    parameter at all until it was found missing: this file built
    `GeminiAdapter(ASSIST_MODEL)` directly, so a goal that chose an
    Anthropic judge got Anthropic for the assessment and GEMINI for the
    investigation of the same run. `assess.py` had already removed this
    exact line from itself and recorded why; the investigator was not
    looked at when it did.

    None means the deployment default, which is what every caller got
    before this argument existed.
    """
    # `tree` IS THE TREE ACCESS, not an archive. `inspect_tree.PodTree`
    # on Kubernetes -- the warm pod the checks ran in, so this reads the
    # actual aftermath of the failure rather than a clean unpack of the
    # same commit -- and `TarTree` on Docker and E0, which is exactly
    # what this file used to do inline.
    if not (available() and tree is not None):
        return None
    try:
        return await asyncio.wait_for(
            _loop(driver, contract, task, entries, tree, model=model),
            timeout=WALL_CLOCK_S)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("investigator stopped: %s", e)
        return None


async def _loop(driver, contract, task, entries, tree,
                model: str | None = None):
    from kernel.harness.models import adapter_for_model      # noqa: PLC0415
    from kernel.harness.models import choice                 # noqa: PLC0415
    from kernel.verifier.inspect_tree import dispatch        # noqa: PLC0415

    # THE ADAPTER FOLLOWS THE MODEL. This was `GeminiAdapter(ASSIST_MODEL)`
    # -- the same hardcoding `assess.py` removed from itself, left here
    # because nothing looked for a second copy. A goal naming a Claude
    # model would have had its id handed to a Gemini adapter, which is a
    # vendor 404 several seconds in.
    adapter = adapter_for_model(model or choice.default_model())

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            "THE STORY'S TASK:\n" + (task or "(none)")[:4000]
            + "\n\nTHE REFUSAL (verbatim entries):\n"
            + json.dumps(entries, default=str)[:120_000]
            + "\n\nWHERE YOU ARE: " + str(tree)},
    ]

    started = time.monotonic()
    for step in range(MAX_STEPS):
        response = await adapter.complete(
            CompletionRequest(messages=messages, tools=TOOLS))
        tc = response.tool_call
        if tc is None:
            # The model spoke instead of acting; take its words as the
            # report rather than forcing the shape.
            text = (response.text or "").strip()
            return ({"why": text, "fix": "", "category": "unshaped"}
                    if text else None)
        name, args = tc.get("tool"), tc.get("args") or {}
        if name == "report":
            if str(args.get("why") or "").strip():
                return {"why": str(args.get("why") or "")[:2000],
                        "fix": str(args.get("fix") or "")[:2000],
                        "category": str(args.get("category") or "")}
            # A hollow report is not a finding. Hand it back as a tool
            # result and keep going -- the first live run called report
            # with every field empty and the note evaporated.
            messages.append(make_assistant_tool_call_message(
                tc, provider_state=response.provider_state))
            messages.append(make_tool_result_message(ToolResult(
                tool_call_id=tc.get("id") or f"i{step}",
                output="report rejected: `why` was empty. Investigate "
                       "with the tools, then report with substance.",
                error="", exit_code=0)))
            continue

        output = await dispatch(tree, name, args)
        messages.append(make_assistant_tool_call_message(
            tc, provider_state=response.provider_state))
        messages.append(make_tool_result_message(ToolResult(
            tool_call_id=tc.get("id") or f"i{step}",
            output=output[:30_000], error="", exit_code=0)))
        if time.monotonic() - started > WALL_CLOCK_S - 60:
            break

    # ── The forced verdict-on-the-verdict ─────────────────────────────
    # The first live run investigated superbly for twelve steps --
    # reproduced the failure, found the vendor tree, even proved a
    # PYTHONPATH workaround -- and then hit the stop-loss with the
    # report unwritten, so all of it evaporated. An investigator that
    # runs out of steps still knows more than anyone else in the room:
    # ask for the report and accept nothing else.
    messages.append({"role": "user", "content":
                     "You are out of investigation steps. Call `report` "
                     "NOW with your findings so far -- partial evidence "
                     "beats none."})
    response = await adapter.complete(CompletionRequest(
        messages=messages,
        tools=[tool for tool in TOOLS if tool["name"] == "report"]))
    tc = response.tool_call
    if tc and tc.get("tool") == "report":
        args = tc.get("args") or {}
        if str(args.get("why") or "").strip():
            return {"why": str(args.get("why") or "")[:2000],
                    "fix": str(args.get("fix") or "")[:2000],
                    "category": str(args.get("category") or "")}
    text = (response.text or "").strip()
    return ({"why": text, "fix": "", "category": "unshaped"}
            if text else None)


# `_tool` LIVED HERE and is now `inspect_tree.TarTree` alongside
# `inspect_tree.PodTree`. It answered its three tools out of an archive
# held in memory and started an ephemeral container per `run` -- both
# the `io.BytesIO` wall plan 5.5 E5 names as what breaks large
# codebases, and the reason a fifteen-step investigation was fifteen
# pods. On Kubernetes those three tools are now three HTTP calls into
# the pod the checks ran in, so the investigator reads what the failure
# actually left behind.
