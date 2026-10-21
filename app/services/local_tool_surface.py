"""Which CONTROL-PLANE tools a local run gets. Not which editor tools.

THE AGENT BRINGS ITS OWN TOOLBOX. Claude Code and Codex already have Read,
Grep, Bash, Write and the rest, and the person is running them on their own
machine, in their own editor, self-operating the story. A kernel profile has
no business narrowing that: the profile's tool list describes what a POD's
sandbox exposes, where the agent has nothing except what it is handed.

So the mapping here is deliberately one-sided. It answers only:

    which of this profile's grants are ways of talking to the CONTROL PLANE,
    and therefore need to exist as MCP tools on the bridge?

Everything else -- read, write, edit, bash, grep, glob, the background-job
set, web_fetch -- is the agent's own, and is neither granted nor withheld
from here.

WHY THIS IS NOT A HOLE. In a pod the tool surface IS the guarantee: it is
what stops a review profile editing the tree. Locally there is no such
guarantee to make, because the person has a terminal in the same directory.
Pretending otherwise by writing a narrow `permissions.allow` would be
theatre -- the agent's own Bash walks straight through it, and the only
thing the narrowing would reliably achieve is breaking legitimate work.

WHAT STILL REFUSES. A profile whose WORKSPACE DRIVER the local lane cannot
honour, and a profile that reaches the control plane through a verb the
bridge does not serve. Both describe a story that would not be the same run
in an editor, and a refusal is more useful than an approximation.
"""

import logging

logger = logging.getLogger(__name__)

# Kernel tool -> the MCP tool the bridge serves. Named individually rather
# than granting `mcp__agentos__*`, so adding a bridge tool does not silently
# add it to every profile's surface.
_BRIDGE: dict[str, str] = {
    "register_plan": "mcp__agentos__register_plan",
    # 2026-10-01: both were SUBSTITUTED here, on the grounds that a local
    # story's deliverable is its diff. That is a story argument and it was
    # applied to errands too, where the artifact IS the deliverable -- and it
    # is not even reliably true of stories: a benchmark, a migration note, an
    # analysis is a real output with nowhere else to go. The blocker was never
    # the bytes (`aput` is content-addressed and lane-blind) but
    # `artifact_manifest.execution_id NOT NULL`, which a local run cannot
    # satisfy. The manifest takes a story now.
    "emit_artifact": "mcp__agentos__emit_artifact",
    "artifact_read": "mcp__agentos__artifact_list",
    # The reviewer's one verb with an effect. It was in `_NOT_LOCAL` on the
    # grounds that "a local story is review-exempt, so a review profile
    # should never be resolved for one" -- an assumption that no longer
    # holds: a local coding story is reviewed before it reaches `reviewed`,
    # by the same rules and into the same table as a pod's.
    "submit_story_review": "mcp__agentos__submit_story_review",
}

# THE BRIDGE'S OWN PLUMBING, granted to every local run whatever the profile
# says. These are not capabilities a profile hands out -- they are how the
# harness functions at all: the state the hook injects each turn, and the
# three recorders the session writes its trace through. No kernel profile
# lists them, because in a pod the harness does these things itself; here the
# session does them over the bridge, so a profile-derived list alone would
# leave every local run without them.
ALWAYS_SERVED: tuple[str, ...] = (
    "mcp__agentos__get_state",
    "mcp__agentos__record_branch",
    "mcp__agentos__record_checkpoint",
    "mcp__agentos__append_ledger",
    # TAKING THE TRUNK IN IS THE AGENT'S WORK, not a capability a profile
    # grants. The catch-up plan the platform writes -- shared with the
    # cloud lane -- has always said "Call `sync_trunk` once", "Run every
    # check with `run_check`", "`story_commit` anything still
    # uncommitted". Locally those tools did not exist, so the agent read
    # its own plan correctly, found nothing to call, and waited, while
    # the extension's poll did the work behind it. Two half-actors, and
    # every failure on 06295e15 lived in the gap between them.
    #
    # Served to every local run for the same reason as the four above:
    # in a pod the harness does these things itself, so no profile lists
    # them, and a profile-derived list alone would leave a local story
    # unable to work the plan it was given.
    "mcp__agentos__sync_trunk",
    "mcp__agentos__run_check",
    "mcp__agentos__story_commit",
)

# Granted by a profile, NOT served by the bridge, and with a real local
# equivalent -- so the honest move is to name the substitute rather than
# refuse the story or grant a tool that does not exist.
#
# This dict was the bug it now fixes: four of these sat in `_BRIDGE`, so a
# coding story's allowlist named four `mcp__agentos__*` tools the extension
# has never implemented, while the four under ALWAYS_SERVED -- which it DOES
# implement -- were left out, because a served profile replaces the client's
# fallback list wholesale.
_SUBSTITUTED: dict[str, str] = {
    "goal_knowledge":
        "read `.agentos-memory/MEMORY.md` -- it is seeded at checkout from "
        "the SAME shared store this goal's web assistant writes to, so what "
        "the goal already settled is already in your workspace",
    "propose_completion":
        "submit the diff from the AgentOS panel and say the work is ready; "
        "the person approves it and marks the story done",
}

# THE PERSON IS IN THE ROOM. `ask_human` and `reply_to_human` exist because
# a pod's executor is talking to somebody who is not there -- the question
# goes to the mailbox, parks the run, and waits for a card to be answered.
# Locally the agent is running in that person's editor: Claude Code and
# Codex already pause and ask, and the answer arrives by them typing it.
# Routing that through the control plane would replace a conversation with
# a ticket.
#
# The conversation is still CAPTURED -- every turn is mirrored into the
# story's thread -- but capture is a recording, not a channel.
_ASKED_IN_THE_EDITOR: dict[str, str] = {
    "ask_human": "the IDE already pauses and asks; the person is right there",
    "reply_to_human": "the reply is the next thing they read in the editor",
}

# Reaches the control plane, and the local lane gets there another way. Not
# a gap: recorded with the reason so a reader does not have to work out
# whether something is missing.
_REACHED_OTHERWISE: dict[str, str] = {
    "story_commit": "the person commits; the extension pushes via landing.ts",
    "sync_trunk": "trunk is merged by the git engine, never by a local run",
    "publish_artifact": "artifacts reach the store through the control plane",
    "register_action_template": "action templates are discovered during pod execution and emitted to ledger",
}

# The agent's own. Listed so that a NEW kernel tool is not silently assumed
# to be one of these -- an unrecognised name refuses, and somebody decides
# which side it belongs on.
_THE_AGENTS_OWN = frozenset({
    "read", "glob", "grep", "list_dir", "write", "edit", "skill", "bash",
    "run_check", "env_setup", "env_install", "bash_background", "bash_wait",
    "bash_output", "bash_jobs", "bash_kill", "web_fetch", "subagent",
})

# Inapplicable locally, and refusing is correct: a profile granting this is
# one that should not be resolved for a local story at all.
# Inapplicable locally, and refusing is correct: a profile granting one of
# these is one that should not be resolved for a local story at all.
#
# EMPTY SINCE 2026-10-01. Its only entry was `submit_story_review`, kept out
# because local stories were review-exempt. They are not any more. The dict
# stays because the CLASSIFICATION still has to exist -- an unrecognised tool
# must refuse rather than be assumed harmless -- and the next genuinely
# pod-only verb belongs here.
_NOT_LOCAL: dict[str, str] = {}


class ProfileNotRunnableLocally(RuntimeError):
    """Named, so the refusal can say which tool and why."""


def bridge_tools_for(tools) -> list[str]:
    """The `mcp__agentos__*` names this profile needs on the bridge.

    Raises `ProfileNotRunnableLocally` for a grant that reaches the control
    plane by a verb the bridge cannot serve. Editor tools are ignored, not
    refused and not granted -- see the module docstring.
    """
    needed: list[str] = list(ALWAYS_SERVED)
    unmappable: list[str] = []
    for tool in sorted(set(tools or ())):
        if tool in _BRIDGE:
            needed.append(_BRIDGE[tool])
        elif (tool in _SUBSTITUTED or tool in _REACHED_OTHERWISE
              or tool in _THE_AGENTS_OWN or tool in _ASKED_IN_THE_EDITOR):
            continue
        elif tool in _NOT_LOCAL:
            unmappable.append(f"{tool} ({_NOT_LOCAL[tool]})")
        else:
            unmappable.append(f"{tool} (the bridge serves no such tool)")
    if unmappable:
        raise ProfileNotRunnableLocally(
            "this story's executor profile reaches the control plane through "
            "tools the local bridge does not serve, so running it here would "
            "not be the same run: " + "; ".join(unmappable))
    return sorted(set(needed))


def local_substitutes(tools) -> list[tuple[str, str]]:
    """What this profile grants that the local lane reaches another way, each
    with the thing to do instead -- in profile-tool order, for the prompt.

    CLAUDE.md states these explicitly rather than staying silent. A session
    told only what it HAS will reach for `propose_completion` anyway, because
    its profile prose says to; being told the substitute in the same breath is
    the difference between a redirect and a dead end.
    """
    out: list[tuple[str, str]] = []
    for tool in sorted(set(tools or ())):
        if tool in _SUBSTITUTED:
            out.append((tool, _SUBSTITUTED[tool]))
        elif tool in _REACHED_OTHERWISE:
            out.append((tool, _REACHED_OTHERWISE[tool]))
        elif tool in _ASKED_IN_THE_EDITOR:
            out.append((tool, _ASKED_IN_THE_EDITOR[tool]))
    return out


def refuse_driver(workspace_driver: str) -> str | None:
    """A driver the local lane cannot honour, or None.

    `copyin` hands the sandbox a copy of files with no git remote; locally
    there is only a clone. Degrading it to git would give a business story a
    working tree it is not supposed to have and a branch nobody asked for.
    """
    if (workspace_driver or "none") == "copyin":
        return ("this story's profile uses the `copyin` workspace driver, "
                "which the local lane has no equivalent for — it runs in a "
                "pod, not in your editor.")
    return None
