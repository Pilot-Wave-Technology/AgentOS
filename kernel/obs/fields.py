"""THE FIELD SET. Edit this file to change the shape of a log line.

Everything else in kernel/obs/ is mechanism: this is policy, kept in one
place so that adding a field, renaming one, or dropping one is a
one-line edit that every service picks up. Nothing downstream reads a
field name it did not get from here.

THREE KINDS OF FIELD, and the difference matters:

  CONTEXT   set by `context.bound(...)` at a boundary, carried on every
            line beneath it. The ids. This is the list that makes a run
            greppable.
  PROCESS   fixed for the life of the process -- component, build sha.
            Computed once at setup, not per record.
  RECORD    read off the LogRecord itself -- severity, logger, message,
            source location, exception.

WHAT DOES NOT GO IN A LOG LINE, ever: model prompts, completions, tool
arguments, tool stdout. They go to the content-addressed artifact store
and the line carries the sha256. That single rule is the difference
between a free logging bill and a $227/month one, and
`ci/check_log_payloads.sh` is what keeps it true.
"""

from __future__ import annotations

import os

# ── CONTEXT ───────────────────────────────────────────────────────────
# name -> one line on why it earns its bytes. The comment is the
# documentation; there is no second place describing these.
CONTEXT_FIELDS = {
    "run_id":       "the join key -- one query returns a whole run, and it "
                    "IS the trace id, so no mapping table",
    "goal_id":      "the tenant boundary; every operator question starts here",
    "story_id":     "the unit of work. An errand's id IS its story id",
    "work_kind":    "story | errand. NULL in the database means story, so it "
                    "is normalised on the way in, not on the way out",
    "execution_id": "one story can have several executions; this is the one "
                    "that ran",
    "leg":          "the generation. 'the same run, the 4th attempt at leg 7' "
                    "is the unit people actually debug",
    "thread_id":    "joins a run to the conversation a person is watching",
    "tool":         "set for the span of one tool call",
    "criterion_id": "set for the span of one verification criterion",
}

# ── PROCESS ───────────────────────────────────────────────────────────
PROCESS_FIELDS = ("component", "build_sha", "instance")

# ── EVENTS ────────────────────────────────────────────────────────────
# A SMALL CLOSED VOCABULARY. The point is that a person who does not know
# this codebase can filter `jsonPayload.event="leg_end"` instead of
# guessing at message prose. Adding one is fine; adding twenty defeats
# the purpose, because then it is prose again with extra steps.
EVENTS = (
    "admit", "dispatch", "leg_start", "leg_end", "tool_call",
    "park", "unpark", "verdict", "publish", "reap", "tick",
    "credential_issue", "credential_revoke", "drain", "heartbeat",
)

# ── the six boundaries where context is set ──────────────────────────
# Documentation, not machinery -- but keeping it beside the field list
# means the two cannot drift apart silently.
BOUNDARIES = (
    "HTTP middleware: run_id from traceparent or minted, goal_id from route",
    "worker tick, per execution: execution_id, goal_id, story_id, "
    "work_kind, leg",
    "leg spawn: inherits by contextvar copy, sets nothing",
    "bridge call: tool, plus traceparent on the POST",
    "verifier Job / assessor pod: from pod-spec env",
    "git engine NOTIFY consumer: run_id from the payload",
)

# ── GCP-specific keys ─────────────────────────────────────────────────
# Two extra keys, both inert bytes on any other cloud. `trace` is what
# makes Logs Explorer group by request and enables the log<->trace jump;
# `sourceLocation` drives Error Reporting grouping. Emitting them costs
# nothing and does not bind the process to GCP -- no SDK, just stdout.
TRACE_KEY = "logging.googleapis.com/trace"
SOURCE_KEY = "logging.googleapis.com/sourceLocation"

SEVERITY = {
    10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL",
}


def process_fields() -> dict:
    """Computed once at setup. B1: no default inside environ.get."""
    sha = os.environ.get("BUILD_SHA") or os.environ.get("PLATFORM_BUILD_SHA")
    instance = os.environ.get("HOSTNAME") or os.environ.get("K_REVISION")
    return {
        "component": os.environ.get("AGENTOS_COMPONENT") or "unknown",
        "build_sha": sha or "unset",
        "instance": instance or "local",
    }


def trace_value(run_id: str) -> str | None:
    """A run_id rendered as Cloud Logging expects a trace.

    The run_id is a UUIDv7 -- 128 bits, which is exactly a W3C trace id,
    which is why there is no mapping table anywhere in this design.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
        "GCP_PROJECT")
    if not project or not run_id:
        return None
    return "projects/%s/traces/%s" % (project, run_id.replace("-", ""))
