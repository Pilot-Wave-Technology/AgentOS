"""What the executor is doing right now, in one line per tool call.

FOR A PERSON WATCHING, AND FOR NOBODY ELSE. A story runs for minutes between
cards, and from the thread it looks identical to a story that has stalled. This
turns that into a ticker: read this, wrote that, ran this. It answers "is it
moving" without answering "what did it find", which is the assistant's job and
a different question.

TWO PROPERTIES IT MUST NOT BREAK, and both are structural rather than
promised:

  IT NEVER ENTERS THE ASSISTANT'S CONTEXT. Nothing here writes to `messages`.
  A row there would be in the assistant's next prompt, and forty tool lines
  per leg would crowd out the conversation they are meant to sit beside — the
  assistant would start summarising a ticker instead of talking to a person.
  This module only READS, and the browser renders it into the DOM.

  IT NEVER WAKES THE ASSISTANT. There is no gateway call, no dispatch, no
  NOTIFY. Polling a table is the whole mechanism, so a person watching costs
  one indexed query every couple of seconds and nothing else. An assistant
  woken forty times a leg would spend the story's budget narrating it.

WHAT IS DELIBERATELY NOT SHOWN. Never a tool RESULT and never file content:
`write` shows its path and not a byte of what it wrote. The point is presence,
not a second copy of the work — and a result stream is where a token would
eventually appear.
"""
from __future__ import annotations

import json
import re

# Token-shaped things that must never reach a browser, even truncated. A bash
# command is the one field here that carries whatever the model typed, and
# `cd repos/x && git push https://x-access-token:ghs_...` is a real shape.
_SECRETS = re.compile(
    r"(gh[psuor]_[A-Za-z0-9]{16,}"          # GitHub tokens
    r"|sk-[A-Za-z0-9_-]{16,}"               # OpenAI-style
    r"|AIza[A-Za-z0-9_-]{20,}"              # Google
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)")  # JWT
# NO GENERIC "long opaque string" RULE. The first version had one — any run of
# 40+ base64 characters — and it redacted long file paths and long test names,
# which is worse than the leak it guarded: a person reading "<redacted>" where
# `pytest tests/test_calculator_behaviour.py` should be cannot tell what ran,
# and learns to distrust the whole ticker. The prefixed shapes above are the
# ones that actually appear.

# Internal bookkeeping the executor emits that means nothing to a person.
# Showing them would make the ticker mostly noise, which is how a live view
# stops being read.
HIDDEN = {
    "boundary_prompt",   # the harness asking the model whether it is done
    "null_step",         # a probe. 95 rows in this database — on its own it
                         # would be most of what a person saw
    "injected",          # a message the harness put in, not a tool the model ran
    "seed",              # corpus setup, before any work
}

# One colour per FAMILY, not per tool: a person is scanning for a change of
# kind — it started writing, it started running things — not memorising a
# palette of fifteen.
FAMILY = {
    "read": "read", "list_dir": "read", "glob": "read", "grep": "read",
    "goal_knowledge": "read", "story_status": "read",
    "write": "write", "edit": "write", "apply_patch": "write",
    "bash": "exec", "env_install": "exec", "env_setup": "exec",
    # I4c — the background three and egress. All four are on
    # story-coding's surface too, so this changes a STORY's ticker from
    # slate-with-no-detail to coloured-with-detail. Strictly additive and
    # a visible improvement: a twenty-poll job was twenty identical blank
    # lines, which reads as a stuck run rather than a watched one.
    "bash_background": "exec", "bash_output": "exec", "bash_kill": "exec",
    # The rest of the background set, added with the tools themselves --
    # this table is the JOIN the surface has no other link to, so a tool
    # granted in a profile and missed here renders as an anonymous grey
    # dot and nobody finds out by reading either file.
    #
    # `bash_wait` IS `exec` AND NOT AN IDLE FAMILY, which is the call
    # worth stating. It is the step where nothing appears to happen --
    # possibly for two hours -- and the temptation is to colour it as
    # waiting. But the JOB is executing, and the whole point of the tool
    # is that one step covers a long run: rendering it as idle would put
    # a quiet colour on the busiest part of a story and recreate, in the
    # ticker, exactly the "stuck run" misreading that `bash_background`
    # was given a colour to fix.
    "bash_wait": "exec", "bash_jobs": "read",
    # `run_check` runs the command the plan declared. Same family as the
    # `bash` it replaces for a long check.
    "run_check": "exec",
    "web_fetch": "read",
    "artifact_read": "read",
    # The review phase. Its one act, so it takes `plan`'s family:
    # the colour a person scans for to see whether it finished.
    "submit_story_review": "plan",
    "story_commit": "commit",
    # `exec` AND NOT `commit`, because it does not keep the work -- it
    # brings somebody else's in. Fetch the trunk lane and merge it, which
    # is a change to the tree the model then has to commit; rendering it
    # as a commit would show a story as having saved work at the moment
    # it acquired a conflict.
    "sync_trunk": "exec",
    # I4b — AN ERRAND'S ONLY DURABLE OUTPUT. It has no `story_commit`,
    # so without this the one step that makes its work survive rendered
    # as an anonymous grey dot. `commit` is the right family for the same
    # reason: this is the errand's version of "the work is now kept".
    "emit_artifact": "commit", "publish_artifact": "commit",
    "register_action_template": "commit",
    "register_plan": "plan", "propose_completion": "plan",
    "ask_human": "ask", "skill": "plan",
    # THE OTHER DIRECTION OF THE SAME CONVERSATION. `ask_human` is the
    # run asking a person; this is the run ANSWERING one. Same family:
    # both are the human channel, and someone scanning a ticker for
    # "when did a person get involved" wants both marks.
    "reply_to_human": "ask",
    "subagent": "exec",
    # NOT HIDDEN, and the reason is the whole point of the ticker. `no_tool`
    # is a turn where the model SPOKE and called nothing, and a run doing only
    # that is the failure loop.py:568 describes — "a story spent TWELVE legs
    # looking like a model sitting on its hands". Hidden, that run shows a
    # ticker that has simply gone quiet, which reads the same as being between
    # legs. Shown, the person can see it is alive and not acting. It is 2% of
    # this database's steps and never more than five in a run, so it costs
    # almost nothing to show.
    "no_tool": "idle", "provider_empty": "idle",
}


# Families the FEED can emit that no tool produces: a verification outcome is
# not a tool call, but it shares this strip and needs its own colour.
EXTRA_FAMILIES = {"pass", "fail"}


def clip(text: str, n: int = 88) -> str:
    text = _SECRETS.sub("<redacted>", " ".join(str(text or "").split()))
    return text if len(text) <= n else text[: n - 1] + "…"


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
        return got if isinstance(got, dict) else {}
    except (TypeError, ValueError):
        return {}


def summarise(tool: str, raw_args) -> dict | None:
    """One line for one tool call, or None when it should not be shown.

    Written as a table of what to SHOW rather than what to hide, so a tool
    added later is unknown-but-visible (its name, no detail) instead of
    silently leaking whatever its first argument happens to be.
    """
    tool = (tool or "").strip()
    if not tool or tool in HIDDEN:
        return None
    a = _args(raw_args)

    if tool in ("read", "write", "edit", "list_dir", "apply_patch"):
        detail = clip(a.get("path") or a.get("file") or "")
    elif tool in ("glob", "grep"):
        detail = clip(a.get("pattern") or a.get("query") or "")
    elif tool == "bash":
        detail = clip(a.get("command") or "")
    elif tool == "env_install":
        pkgs = a.get("packages")
        if isinstance(pkgs, list):
            pkgs = ", ".join(str(p) for p in pkgs[:4])
        detail = clip(f"{a.get('manager') or ''} {pkgs or ''}".strip())
    elif tool == "env_setup":
        detail = "preparing the environment"
    elif tool == "skill":
        detail = clip(a.get("name") or "")
    elif tool == "story_commit":
        detail = clip(a.get("message") or "")
    elif tool == "register_plan":
        steps = a.get("steps") if isinstance(a.get("steps"), list) else []
        checks = a.get("checks") if isinstance(a.get("checks"), list) else []
        detail = f"{len(steps)} step(s), {len(checks)} check(s)"
    elif tool == "propose_completion":
        detail = "claimed the work is done"
    elif tool == "ask_human":
        detail = "asked a question"
    elif tool == "reply_to_human":
        detail = "answered a person's question"
    elif tool == "no_tool":
        detail = "spoke without calling a tool"
    elif tool == "provider_empty":
        detail = "the model returned nothing — retrying"
    elif tool == "artifact_read":
        detail = clip(a.get("artifact_id") or "")
    elif tool in ("emit_artifact", "publish_artifact"):
        # WHAT IS BEING KEPT, not that something was. "emit_artifact" on
        # its own tells a person watching nothing they did not know.
        name = a.get("name") or a.get("path") or ""
        role = a.get("role") or ""
        detail = clip(f"{role}: {name}".strip(": ") or name)
    elif tool == "bash_background":
        detail = clip(a.get("command") or "")
    elif tool == "bash_output":
        # THE JOB AND THE CURSOR. Polling one job twenty times is twenty
        # rows, and without these they are indistinguishable — which is
        # exactly what the Ec1e cursor was added to fix, and the ticker
        # was still showing the old picture.
        since = a.get("since")
        detail = clip(f"{a.get('job_id') or 'job'}"
                      + (f" from {since}" if since else ""))
    elif tool == "bash_kill":
        detail = clip(f"stopping {a.get('job_id') or 'a job'}")
    elif tool == "web_fetch":
        detail = clip(a.get("url") or "")
    elif tool == "subagent":
        # Delegation is worth seeing: the executor has handed part of the work
        # to a child, and from the outside that otherwise looks like a pause.
        detail = clip(f"{a.get('flavour') or 'child'}: {a.get('task') or ''}")
    else:
        # UNKNOWN TOOL, NAMED AND NOT DESCRIBED. A default that guessed at the
        # first argument is how a future tool's secret argument ends up on a
        # screen; the name alone is still useful and cannot leak.
        detail = ""

    return {"tool": tool, "detail": detail,
            "family": FAMILY.get(tool, "other")}
