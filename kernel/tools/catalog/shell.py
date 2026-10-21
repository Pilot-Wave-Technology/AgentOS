"""The shell, foreground and background.

The only family declared `by_argument`. Classifying `bash` by name
would classify `rm -rf /` as contained, so `effects.py` reads the
command instead — see `kernel.harness.destructive` for what it looks for.

The background three exist because a leg that blocks on a server
it started never gets to use it.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _I, _S, _schema

TOOLS: dict[str, dict] = {
    # ── Shell, foreground and background ─────────────────────────────────
    "bash": _schema(
        "bash",
        "Run a shell command and wait for it. Default 30s; pass `timeout` "
        "for longer, up to 120. Anything that could take longer than that "
        "belongs in bash_background, where you can watch it.",
        {"command": {"type": _S},
         # IMPLEMENTED SINCE IT WAS WRITTEN, AND NOT DECLARED UNTIL Ec1c.
         # `_tool_bash` has always read this and capped it at 120, so the
         # 120s the description advertised was unreachable and every
         # foreground command died at the default. The same shape as
         # `read`'s offset/limit before G4: a parameter that is
         # implemented, documented and impossible to send.
         "timeout": {"type": _I,
                     "description": "Seconds to wait, 1 to 120. Default "
                                    "30. Over 120 is refused, not "
                                    "shortened — use bash_background."}},
        ["command"],
        side="sandbox", effect="by_argument",
    ),
    "bash_background": _schema(
        "bash_background",
        "Start a long-running command without blocking. Returns a job id. "
        "Use for builds and test suites: a foreground command that outlives "
        "the leg's wall clock ends the leg with nothing to show for it.",
        {"command": {"type": _S},
         # OPTIONAL, AND UNLIMITED WHEN ABSENT. `bash_background` has
         # never had a budget; giving every job a default one would kill
         # the forty-minute suites that work today. Declaring a timeout
         # buys a clean, explained stop instead of a leg that ends with
         # the job still running.
         "timeout": {"type": _I,
                     "description": "Optional. Seconds before the job is "
                                    "stopped and reported as a TIMEOUT "
                                    "rather than a failure. Omit for no "
                                    "limit. Out of range is refused, not "
                                    "shortened."}},
        ["command"],
        side="sandbox", effect="by_argument",
    ),
    "bash_output": _schema(
        "bash_output",
        "Read output from a background job. Pass `since` to get only what "
        "is new — the previous result tells you the number. Polling "
        "without it re-reads everything you have already seen, and a run "
        "of identical polls is what the progress clock ends a leg for.",
        {"job_id": {"type": _S},
         "since": {"type": _I,
                   "description": "Byte position to read from, as printed "
                                  "by the previous poll. Omit for the "
                                  "whole buffer."}},
        ["job_id"],
        side="sandbox", effect="contained",
    ),
    "run_check": _schema(
        "run_check",
        "Run one of the checks your approved plan declared, using the "
        "timeout the plan gave it. Names the check by its label. This is "
        "how you measure a suite that takes longer than a foreground "
        "command may -- you do not choose the budget, the plan did. "
        "If that exact command has already run over this exact tree, "
        "committed work and uncommitted alike, you get that run's "
        "result and the job it came from instead of a second run.",
        {"which": {"type": _S,
                   "description": "Which check, when the plan declares "
                                  "more than one: its number, or any "
                                  "distinctive part of its command. Omit "
                                  "when there is only one -- a LABEL will "
                                  "not do, since `goal` and `guard` say "
                                  "what a check promises, not which one "
                                  "it is."}},
        [],
        side="worker", effect="contained",
    ),
    "bash_wait": _schema(
        "bash_wait",
        "Wait for a background job to finish and return its result. This "
        "is ONE step however long the job takes — minutes or hours — so "
        "prefer it over polling bash_output in a loop, which spends a "
        "step per poll and ends the leg as stalled after eight identical "
        "ones. Pass `since` to get only output newer than a cursor you "
        "already have.",
        {"job_id": {"type": _S},
         "since": {"type": _I,
                   "description": "Byte offset you have already seen. "
                                  "Omit for everything in the window."}},
        ["job_id"],
        side="sandbox", effect="contained",
    ),
    "bash_jobs": _schema(
        "bash_jobs",
        "List the background jobs still running. A leg boundary loses "
        "your memory of a job id, not the job itself — if you started "
        "something long and no longer know its id, this is how you find "
        "it again.",
        {},
        [],
        side="sandbox", effect="contained",
    ),
    "bash_kill": _schema(
        "bash_kill", "Stop a background job.",
        {"job_id": {"type": _S}},
        ["job_id"],
        side="sandbox", effect="contained",
    ),
}
