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


@dataclass(frozen=True)
class SlackCommand:
    name: str                                   # canonical command, no leading slash
    summary: str                                # one-line description for /help
    usage: str                                  # invocation hint for /help
    group: str
    dispatch: Callable[[CommandContext], Awaitable[str]]
    admin: bool = False                         # True → marked "(admin)" in /help
    aliases: tuple = ()                         # alternate command names


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


async def _d_task(c: CommandContext) -> str:
    from app.channels.slack.commands.task import handle
    return await handle(
        c.args, c.channel_id, c.team_id,
        slack_user_id=c.user_id, response_url=c.response_url,
        command=c.command,
    )


async def _d_resources(c: CommandContext) -> str:
    from app.channels.slack.commands.resources import handle
    return await handle(c.args, c.user_id, c.channel_id, c.team_id, c.response_url,
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
            "  /goal pause [reason]\n"
            "      Freeze autonomous task dispatch for this goal. Admin only.\n"
            "\n"
            "  /goal resume\n"
            "      Clear a pause and restart task dispatch. Admin only.\n"
            "\n"
            "  /goal refresh\n"
            "      Reset the gateway session for this channel. Use if the agent stops responding."
        ),
        GROUP_READ, _d_goal,
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
            "      Accept a proposed story and claim it (proposed → accepted).\n"
            "\n"
            "  /story start <id>\n"
            "      Start work on an accepted story (accepted → in_progress).\n"
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
            "  /story work <title-or-id>\n"
            "      Open your personal assistant channel for a specific story.\n"
            "      Equivalent to @mentioning the bot in the goal channel.\n"
            "\n"
            "  /story review <id> [approve|reject] [feedback]\n"
            "      Clear a human-review checkpoint on a story. Feedback is optional."
        ),
        GROUP_WORK, _d_story,
    ),
    SlackCommand(
        "task",
        "List, show status, or delete tasks. Run `/task help` for subcommands.",
        (
            "/task [help]\n"
            "\n"
            "  /task list <story-id>\n"
            "      List all tasks for a story, ordered by state (in-progress first).\n"
            "      <story-id> is the 8-char story id from /story. Example: `/task list a1b2c3d4`\n"
            "\n"
            "  /task <id>\n"
            "      Show the current status of a single task — state and any blocked reason.\n"
            "      <id> is the 8-char task id shown in /task list. Example: `/task b5c6d7e8`\n"
            "\n"
            "  /task delete <id>\n"
            "      Hard-delete a task and its subtask tree. Admin only."
        ),
        GROUP_WORK, _d_task,
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
    ),
    SlackCommand(
        "memory",
        "Search Assistant memory, decisions, and task history",
        "/memory <query>",
        GROUP_READ, _d_memory,
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
    out: dict[str, SlackCommand] = {}
    for cmd in COMMANDS:
        out[cmd.name] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


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
