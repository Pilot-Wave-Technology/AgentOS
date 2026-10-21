"""One-shot text completion, for the kernel's advisory brains.

The executor's adapter seam (CompletionRequest, tools, legs) is built
for driving work. The explainer and the setup scout are not work: they
are single prompt-in, prose-out calls whose failure must cost nothing.
This is the smallest honest wrapper for that — and it is ADVISORY BY
CONTRACT: nothing that calls it may let the reply decide a verdict,
gate a transition, or write a file the mechanical path would not have
written anyway. The brains explain and suggest; the machine decides.

MODEL AND BUDGET are the deployment's, not the caller's: gemini-3.8-flash
with a 500K-token reading budget unless the environment says otherwise.
The budget is enforced by truncating INPUT here, because every caller
assembling "as much context as fits" would otherwise invent its own
arithmetic.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# The repo's B1 guard bans environ.get with silent defaults; this form
# names the default in the open and still lets the env override.
ASSIST_MODEL = (os.environ["AGENTOS_ASSIST_MODEL"]
                if "AGENTOS_ASSIST_MODEL" in os.environ
                else "gemini-3.8-flash")
ASSIST_CONTEXT_TOKENS = int(
    os.environ["AGENTOS_ASSIST_CONTEXT_TOKENS"]
    if "AGENTOS_ASSIST_CONTEXT_TOKENS" in os.environ else "500000")

# ~3.2 chars per token is conservative for code-heavy text; erring low
# means we truncate a little early rather than get a 400 back.
MAX_INPUT_CHARS = int(ASSIST_CONTEXT_TOKENS * 3.2)


def available() -> bool:
    """On only where somebody turned it on. The E0 gates run in the same
    container with the same key, and a deterministic suite that quietly
    starts calling a live model on every refusal is a suite whose green
    means something different on Tuesdays. AGENTOS_ASSIST=1 is set by
    the live console's start script and nothing else."""
    return bool(os.environ.get("GEMINI_API_KEY")) and (
        os.environ.get("AGENTOS_ASSIST") == "1")


async def complete_text(prompt: str) -> str:
    """The model's reply as plain text, or "" on any failure.

    NO OUTPUT BUDGET AND NO THINKING KNOBS. The first version capped the
    reply at 400 tokens; the model spent them thinking and the note was
    cut mid-sentence. The lesson was not "turn thinking off" -- it was
    that this seam has no business rationing the model's answer at all.
    The model thinks as long as it thinks and says what it has to say;
    the INPUT is what the deployment bounds (the 500K reading budget),
    because that is the figure that costs money per call site.

    Empty-on-failure is the contract that keeps this advisory: a caller
    that cannot tell "no note" from "the API was down" cannot be tempted
    to treat the note as load-bearing.
    """
    if not available():
        return ""
    if len(prompt) > MAX_INPUT_CHARS:
        head = int(MAX_INPUT_CHARS * 0.8)
        tail = MAX_INPUT_CHARS - head
        prompt = (prompt[:head] + "\n\n[... truncated to fit the reading "
                  "budget ...]\n\n" + prompt[-tail:])

    def _call() -> str:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        resp = client.models.generate_content(
            model=ASSIST_MODEL, contents=prompt)
        return (resp.text or "").strip()

    try:
        # The SDK is synchronous; the adjudicator loop is not.
        return await asyncio.to_thread(_call)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("assist model call failed (%s): %s",
                       ASSIST_MODEL, e)
        return ""
