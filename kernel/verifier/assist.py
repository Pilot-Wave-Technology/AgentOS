"""The explainer: a refusal a person can act on, written after the verdict.

THE MACHINE GRADES, THE BRAIN EXPLAINS. The verdict is already decided
when this runs, from exit codes and hashes, and nothing here can touch
it. What this changes is the sentence underneath: the first live story
was refused with `ModuleNotFoundError: No module named 'fastapi'`, the
author learned nothing from it, and the model's response was to commit
503 vendored library files to make the message go away. A refusal that
teaches nothing does not stop bad work -- it redirects it.

Failure-safe by construction: no API key, a down model, or a bad reply
all produce "" and the raw log stands alone, exactly as today.
"""

from __future__ import annotations

import json
import logging

from kernel.harness.models.quick import available, complete_text

logger = logging.getLogger(__name__)

PROMPT = """You are the verification explainer inside an agent platform.
An automated verifier just REFUSED a completion claim (or could not run
its checks). Your job: one short note telling the author exactly what
went wrong and the most direct fix. The verdict is already decided; you
only explain it.

Rules:
- 2 to 4 plain sentences. Start with "Why:" then "Fix:".
- Name files and commands precisely; never invent ones you cannot see.
- The environment is built from committed manifests (requirements.txt,
  package-lock.json, pnpm-lock.yaml) or a committed .agentos/setup.sh,
  then checks run OFFLINE in a clean container. Installing through the
  shell, vendoring libraries, or downloading during tests will not work
  -- steer the author to the manifest and the `env_setup` tool instead.
- If the failure is not about the work (infrastructure, timeout), say
  so plainly.

THE STORY'S TASK:
{task}

WHAT THE VERIFIER SAW (refusal entries / criterion output):
{entries}

ENVIRONMENT PREPARE REPORT (how the check environment was built):
{setup}
"""


async def explain_refusal(task: str, entries: list,
                          setup_report: dict | None = None) -> str:
    """A short human note for a refused or errored claim, or ""."""
    if not available():
        return ""
    try:
        note = await complete_text(PROMPT.format(
            task=(task or "(no task text)")[:4000],
            entries=json.dumps(entries, default=str)[:200_000],
            setup=json.dumps(setup_report or {}, default=str)[:20_000],
        ))
    except Exception:                                       # noqa: BLE001
        logger.exception("explainer failed; the raw log stands")
        return ""
    return note[:4000]
