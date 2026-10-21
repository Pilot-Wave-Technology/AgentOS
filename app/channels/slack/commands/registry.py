"""Single source of truth for Slack slash commands.

Every command is declared ONCE here, with its metadata (summary, usage, group,
admin flag) and a thin dispatch adapter that calls the command's handler with the
right arguments. Both the dispatcher (`handle_slack_command`) and the `/help`
command read from this list, so:

  - adding a command  = add one SlackCommand entry (+ its handler module)
  - removing a command = delete its entry
  - /help and the "unknown command" message update automatically

Commands are grouped into 5 top-level slash commands. Subcommands are parsed
inside each handler — users only need to remember 5 names.

Interactive Block Kit buttons (Approve, Request Changes, Retry, …) are handled in
actions.py, not as slash commands; they are listed in INTERACTIVE_ACTIONS so /help
can show them too.
"""
from dataclasses import dataclass, field
from typing import Awaitable, Callable

GROUP_READ = "Read / inspect"
GROUP_WORK = "Work & execution control"
GROUP_META = "Help"


@dataclass(frozen=True)
class CommandContext:
    """Everything a command handler might need, normalised in one place."""
    args: str
    user_id: str
    channel_id: str
    team_id: str
    response_url: str = ""
    # Full command name as the user typed it (with env suffix), e.g. "resources-feature".
    # Handlers use this in usage strings so they always show the correct invocation.
    command: str = ""
    # SLACK'S HANDLE FOR OPENING A MODAL, and it is perishable: valid for
    # three seconds, single use, and absent when Slack retries. A handler
    # that wants a dialog must therefore still work without one -- see
    # `/run pause`, which takes its reason inline when this is empty.
    trigger_id: str = ""


@dataclass(frozen=True)
class SlackCommand:
    name: str                                   # canonical command, no leading slash
    summary: str                                # one-line description for /help
    usage: str                                  # invocation hint for /help
    group: str
    dispatch: Callable[[CommandContext], Awaitable[str]]
    admin: bool = False                         # True → marked "(admin)" in /help
    # Slack's one-line argument prompt, shown in the composer. Explicit
    # rather than parsed out of `usage`: the first line of that block is a
    # header like "/goal [help]", so deriving it produced "[help]" for half
    # the commands and a sentence fragment for the rest.
    hint: str = ""


# ── Dispatch adapters ─────────────────────────────────────────────────────────

async def _d_goal(c: CommandContext) -> str:
    from app.channels.slack.commands.goal import handle
    return await handle(
        c.args, c.channel_id, c.team_id,
        slack_user_id=c.user_id, response_url=c.response_url,
        command=c.command,
    )


async def _d_story(c: CommandContext) -> str:
    from app.channels.slack.commands.story import handle
    return await handle(
        c.args, c.channel_id, c.team_id,
        slack_user_id=c.user_id, response_url=c.response_url,
        command=c.command,
    )


async def _d_resources(c: CommandContext) -> str:
    from app.channels.slack.commands.resources import handle
    return await handle(c.args, c.user_id, c.channel_id, c.team_id, c.response_url,
                        command=c.command)


async def _d_run(c: CommandContext) -> str:
    from app.channels.slack.commands.run import handle
    return await handle(c.args, c.channel_id, c.team_id,
                        slack_user_id=c.user_id, response_url=c.response_url,
                        command=c.command, trigger_id=c.trigger_id)


async def _d_park(c: CommandContext) -> str:
    from app.channels.slack.commands.park import handle
    return await handle(c.args, c.channel_id, c.team_id,
                        slack_user_id=c.user_id, response_url=c.response_url,
                        command=c.command)


async def _d_errand(c: CommandContext) -> str:
    from app.channels.slack.commands.errand import handle
    return await handle(c.args, c.channel_id, c.team_id,
                        slack_user_id=c.user_id, response_url=c.response_url,
                        command=c.command)


async def _d_memory(c: CommandContext) -> str:
    from app.channels.slack.commands.memory import handle
    return await handle(c.args, c.user_id, c.channel_id, c.team_id, command=c.command)


async def _d_help(c: CommandContext) -> str:
    return render_help()


# ── The registry ───────────────────────────────────────────────────────────────

COMMANDS: list[SlackCommand] = [
    SlackCommand(
        "goal",
        "Goal summary, status, members, invitations, and execution control. Run `/goal help` for subcommands.",
        (
            "/goal [help]\n"
            "\n"
            "  /goal\n"
            "      Show the goal summary: name, story counts, what's in progress and blocked.\n"
            "      Run from any goal channel, orchestrator channel, or your assistant channel.\n"
            "\n"
            "  /goal status [audit]\n"
            "      Detailed health dashboard. Add `audit` to also show recent activity log.\n"
            "\n"
            "  /goal members\n"
            "      List all members of this goal and their roles (admin / member / viewer).\n"
            "\n"
            "  /goal invite <email> [admin|member|viewer]\n"
            "      Add an existing AgentOS user to this goal by their email address.\n"
            "      Role defaults to `member` if omitted. Admin only.\n"
            "      Example: `/goal invite alice@example.com member`\n"
            "\n"
            "  /goal remove <email>\n"
            "      Remove a member from this goal. Admin only (no platform.admin bypass).\n"
            "      Blocked if the member has in-progress work; not-yet-started stories\n"
            "      needing reassignment must be handled from the web Members page.\n"
            "      Example: `/goal remove alice@example.com`\n"
            "\n"
            "  /goal refresh\n"
            "      Reset the gateway session for this channel. Use if the agent stops responding."
        ),
        GROUP_READ, _d_goal,
        hint="[help | status | members | invite <email> | refresh]",
    ),
    SlackCommand(
        "story",
        "List, create, transition, complete, and review stories. Run `/story help` for subcommands.",
        (
            "/story [help]\n"
            "\n"
            "  /story\n"
            "      List all active stories in this goal (proposed, accepted, in progress, blocked).\n"
            "\n"
            "  /story <id>\n"
            "      Show full detail for a story — description, state, assignee, tasks.\n"
            "      <id> is the 8-char story id shown in the list. Example: `/story a1b2c3d4`\n"
            "\n"
            "  /story create <title>\n"
            "      Create a new story in 'proposed' state. The Goal Orchestrator will\n"
            "      elaborate it into tasks. Example: `/story create Add export to CSV`\n"
            "\n"
            "  /story accept <id>\n"
            "      Claim a proposed story without starting it yet\n"
            "      (proposed → accepted). Use `start` to begin.\n"
            "\n"
            "  /story start <id>\n"
            "      BEGIN. Accepts the story if it is still proposed, assigns\n"
            "      it to you, sets up your assistant's workspace and opens\n"
            "      the story's channel — one command from proposed to\n"
            "      working. `open`, `import` and `work` do the same thing.\n"
            "\n"
            "  /story cancel <id> [reason]\n"
            "      Cancel a story: bridges whatever depended on it and records\n"
            "      why. A started code story is refused — its branch is cut and\n"
            "      work may already be committed.\n"
            "\n"
            "  /story block <id> <reason>\n"
            "      Mark a story as blocked with a reason. Example: `/story block a1b2c3d4 waiting on API key`\n"
            "\n"
            "  /story unblock <id>\n"
            "      Unblock a story (blocked → in_progress).\n"
            "\n"
            "  /story done <id> [feedback]\n"
            "      Mark a story done. Optionally include feedback for the GO.\n"
            "\n"
            "  /story review <id> [approve|reject] [feedback]\n"
            "      Clear a human-review checkpoint on a story. Feedback is optional."
        ),
        GROUP_WORK, _d_story,
        hint="[<id> | create <title> | start|cancel|accept|block|done <id> | review <id>]",
    ),
    SlackCommand(
        "resources",
        "List, scan, and allocate goal resources. Run `/resources help` for subcommands.",
        (
            "/resources [help]\n"
            "\n"
            "  /resources\n"
            "      List all active resources for this goal, grouped by kind (repo, branch, file, …).\n"
            "\n"
            "  /resources <text>\n"
            "      Filter the list by name, kind, or notes. Example: `/resources repo` shows only repos.\n"
            "\n"
            "  /resources add\n"
            "      Returns a link to the goal resources browser in the control panel where you\n"
            "      can add new resources (repos, branches, documents, API keys, etc.).\n"
            "\n"
            "  /resources scan <id>\n"
            "      Trigger a fresh scan of a repo, branch, file, or document resource. The scan\n"
            "      re-reads the content and updates the summary the SHCA and GO use for planning.\n"
            "      <id> is the 8-char resource id shown in /resources. Example: `/resources scan a1b2c3d4`\n"
            "\n"
            "  /resources grant <resource-id> <story|task> <subject-id> [needs|produces|modifies]\n"
            "      Allocate a resource to a story or task. Relation defaults to `needs`. Admin only.\n"
            "      Example: `/resources grant a1b2c3d4 story b5c6d7e8`\n"
            "\n"
            "  /resources revoke <resource-id> <story|task> <subject-id>\n"
            "      Remove a resource allocation from a story or task. Admin only."
        ),
        GROUP_READ, _d_resources,
        hint="[help | <filter> | add | scan <id> | grant|revoke <id> story <subj>]",
    ),
    SlackCommand(
        "memory",
        "Search Assistant memory, decisions, and task history",
        "/memory <query>",
        GROUP_READ, _d_memory,
        hint="<query>",
    ),
    SlackCommand(
        "run",
        "See and steer a story's execution. Run `/run help` for subcommands.",
        __import__("app.channels.slack.commands.run", fromlist=["USAGE"]).USAGE,
        GROUP_WORK, _d_run,
        hint="[<story-id> | pause | resume | retry | cancel | more <what>]",
    ),
    SlackCommand(
        "park",
        "Answer the question an execution is waiting on. `/park help` for more.",
        __import__("app.channels.slack.commands.park", fromlist=["USAGE"]).USAGE,
        GROUP_WORK, _d_park,
        hint="[answer <text> | approve | revise <what> | decline <why>]",
    ),
    SlackCommand(
        "errand",
        "Approve or decline the autonomous runs the GO proposes.",
        __import__("app.channels.slack.commands.errand", fromlist=["USAGE"]).USAGE,
        GROUP_WORK, _d_errand,
        hint="[approve <id> | decline <id> <why>]",
    ),
    SlackCommand("help", "Show this list of commands", "/help", GROUP_META, _d_help),
]

# Interactive Block Kit buttons handled by actions.py (not slash commands).
# Listed here so /help can surface them; keep in sync when adding card buttons.
INTERACTIVE_ACTIONS: list[tuple] = [
    ("Approve", "Approve a plan or completion card"),
    ("Request Changes", "Send feedback and trigger a re-plan"),
    ("Retry", "Retry a failed task"),
    ("Cancel", "Cancel a task from its card"),
    ("Reply", "Answer a question card inline"),
    ("View Details", "Open the full plan / context in a modal"),
]


# ── Lookup + help rendering ──────────────────────────────────────────────────

def _command_map() -> dict[str, SlackCommand]:
    """name/alias → SlackCommand. Built fresh so it always reflects COMMANDS."""
    # `aliases` used to be a field here, declared on every command and set
    # on none — so this loop ran zero times, forever, and the field read as
    # a feature the product had. Removed; if an alias is ever wanted, it
    # should arrive with a command that uses it.
    return {cmd.name: cmd for cmd in COMMANDS}


def lookup(cmd_name: str) -> SlackCommand | None:
    return _command_map().get(cmd_name)


def known_command_names() -> list[str]:
    """Suffixed command names as users must type them, for the unknown-command message."""
    return [_cmd_prefix(c.name) for c in COMMANDS]


def _cmd_prefix(name: str) -> str:
    """Return the full slash command name for this environment, e.g. 'goal-prod'."""
    from app.config import get_settings
    suffix = get_settings().slack_command_suffix.strip().lstrip("-")
    return f"{name}-{suffix}" if suffix else name


def render_help() -> str:
    """Render /help from the live registry, grouped and with admin markers."""
    lines = ["*AgentOS Slack commands*  _Run `/<command> help` for subcommand details._"]
    for group in (GROUP_READ, GROUP_WORK, GROUP_META):
        group_cmds = [c for c in COMMANDS if c.group == group]
        if not group_cmds:
            continue
        lines.append(f"\n*{group}*")
        for c in group_cmds:
            prefix = _cmd_prefix(c.name)
            summary = c.summary.replace(f"/{c.name}", f"/{prefix}")
            lines.append(f"  `/{prefix}` — {summary}")

    lines.append("\n*Card buttons* (on approval / question / failure cards)")
    for label, desc in INTERACTIVE_ACTIONS:
        lines.append(f"  • *{label}* — {desc}")

    return "\n".join(lines)


def render_command_help(cmd_name: str) -> str:
    """Render detailed help for a single command, including all subcommands."""
    cmd = lookup(cmd_name)
    if not cmd:
        return f"Unknown command `/{cmd_name}`. Run `/help` to see all commands."
    # Replace bare /command references in usage text with the suffixed form
    prefix = _cmd_prefix(cmd.name)
    usage = cmd.usage.replace(f"/{cmd.name}", f"/{prefix}")
    return f"*/{prefix}* — {cmd.summary}\n\n```{usage}```"
