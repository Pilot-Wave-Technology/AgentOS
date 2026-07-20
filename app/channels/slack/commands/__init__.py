"""Slack slash command dispatcher.

This module is intentionally thin: the list of commands, their metadata, and how
each one is invoked all live in registry.py (the single source of truth). The
dispatcher just normalises the inbound request into a CommandContext and routes it
to the registered handler. /help is itself a registry command, so it always shows
exactly the commands that are wired up here — add or remove a command in registry.py
and both dispatch and help update together.

No imports from app.slack.* — fully self-contained in the new channel system.
"""
import logging

from app.channels.slack.commands.registry import (
    CommandContext, lookup, known_command_names, _cmd_prefix,
)

_log = logging.getLogger(__name__)


async def handle_slack_command(
    command: str,
    text: str,
    user_id: str,
    channel_id: str,
    team_id: str,
    response_url: str = "",
) -> str:
    """Dispatch a Slack slash command. Returns an ephemeral message string."""
    cmd = (command or "").strip().lower().lstrip("/")
    # Strip env suffix so /goal-prod, /goal-dev, /goal-feature all map to "goal".
    # Suffix is configured via SLACK_COMMAND_SUFFIX env var; fall back to
    # stripping any known suffix so old deployments without the var still work.
    from app.config import get_settings
    configured_suffix = get_settings().slack_command_suffix.strip().lstrip("-")
    if configured_suffix and cmd.endswith(f"-{configured_suffix}"):
        cmd = cmd[: -(len(configured_suffix) + 1)]
    elif not configured_suffix:
        for _fallback in ("prod", "dev", "feature"):
            if cmd.endswith(f"-{_fallback}"):
                cmd = cmd[: -(len(_fallback) + 1)]
                break
    # Strip Slack formatting characters that leak through in copy-pasted text
    args = (text or "").replace("`", "").strip()

    _log.info(
        "Slack command: /%s %r (user=%s channel=%s team=%s)",
        cmd, args[:80], user_id, channel_id, team_id,
    )

    spec = lookup(cmd)
    if spec is None:
        names = ", ".join(f"/{n}" for n in known_command_names())
        help_cmd = _cmd_prefix("help")
        return (
            f"/{command.lstrip('/')} is not a recognised command. "
            f"Try /{help_cmd}.\nAvailable: {names}"
        )

    ctx = CommandContext(
        args=args, user_id=user_id, channel_id=channel_id,
        team_id=team_id, response_url=response_url,
        command=_cmd_prefix(cmd),
    )
    return await spec.dispatch(ctx)
