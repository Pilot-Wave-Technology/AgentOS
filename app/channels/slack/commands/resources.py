"""/resources — list, scan, and allocate goal resources from Slack.

Usage:
  /resources                          list up to 50 active resources grouped by kind
  /resources <filter>                 filter by kind or name substring
  /resources scan <id>                trigger a fresh scan (async — posts result when done)
  /resources add                      open the resources page in the control panel
  /resources grant <id> <story|task> <subject-id> [relation]   allocate (admin only)
  /resources revoke <id> <story|task> <subject-id>             de-allocate (admin only)
"""
import asyncio
import logging

from app.auth import get_goal_role
from app.database import get_pool
from app.channels.slack.commands._helpers import (
    resolve_goal_from_any_channel, resolve_user, post_followup, run_followup,
)
from app.roles import GoalRole

_log = logging.getLogger(__name__)


async def handle(
    args: str, slack_user_id: str, channel_id: str, team_id: str,
    response_url: str = "",
    command: str = "",
) -> str:
    cmd = command or "resources"
    pool = get_pool()
    goal_id, bot_token = await resolve_goal_from_any_channel(channel_id, team_id, pool)
    if not goal_id:
        return f"Run /{cmd} from a goal channel, orchestrator channel, or assistant channel."

    requester_uid = await resolve_user(slack_user_id, bot_token, pool)
    if not requester_uid:
        return "I don't recognise your Slack account. Sign in first, then try again."
    if await get_goal_role(requester_uid, goal_id) is None:
        return "You're not a member of this goal."

    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("help", "--help", "-h"):
        from app.channels.slack.commands.registry import render_command_help
        return render_command_help("resources")

    if sub == "scan":
        return await _handle_scan(resource_prefix=rest, goal_id=goal_id,
                                  response_url=response_url, cmd=cmd)

    if sub == "add":
        return _handle_add(goal_id=goal_id)

    if sub == "grant":
        return await _handle_grant(rest, goal_id, requester_uid, pool, cmd=cmd)

    if sub == "revoke":
        return await _handle_revoke(rest, goal_id, requester_uid, pool, cmd=cmd)

    return await _handle_list(args=args, goal_id=goal_id, cmd=cmd)



def _handle_add(goal_id: str) -> str:
    """Return a link to the goal resources page — resource creation happens in the web UI."""
    from app.config import get_settings
    base = (get_settings().control_panel_url or "").rstrip("/")
    if base:
        url = f"{base}/goals/{goal_id}/resources/browser"
        return f"Add resources in the control panel: <{url}|Open resources page>"
    return f"Add resources at `/goals/{goal_id}/resources/browser` in the control panel."


async def _require_goal_admin(user_uid: str, goal_id: str, pool) -> str:
    """Return an error string if the user may not grant/revoke allocations, else ''."""
    role = await get_goal_role(user_uid, goal_id)
    is_sys_admin = await pool.fetchval(
        "SELECT system_role = 'admin' FROM users WHERE uid = $1", user_uid,
    )
    if role != GoalRole.GOAL_ADMIN and not is_sys_admin:
        return "Only goal admins can grant or revoke resource allocations."
    return ""


async def _resolve_resource_id(prefix: str, goal_id: str, pool) -> str:
    return await pool.fetchval(
        """SELECT id FROM goal_resources
            WHERE goal_id = $1 AND status = 'active' AND (id = $2 OR id LIKE $3)
            ORDER BY (id = $2) DESC LIMIT 1""",
        goal_id, prefix, f"{prefix[:8]}%",
    ) or ""


async def _resolve_subject_id(subject_type: str, prefix: str, goal_id: str, pool) -> str:
    table = "stories" if subject_type == "story" else "tasks"
    return await pool.fetchval(
        f"""SELECT id FROM {table}
             WHERE goal_id = $1 AND (id = $2 OR id LIKE $3)
             ORDER BY (id = $2) DESC, created_at DESC LIMIT 1""",
        goal_id, prefix, f"{prefix[:8]}%",
    ) or ""


async def _handle_grant(rest: str, goal_id: str, user_uid: str, pool, cmd: str = "resources") -> str:
    usage = f"Usage: /{cmd} grant <resource-id> <story|task> <subject-id> [needs|produces|modifies]"
    parts = rest.split()
    if len(parts) < 3:
        return usage
    resource_prefix, subject_type, subject_prefix = parts[0], parts[1].lower(), parts[2]
    relation = parts[3].lower() if len(parts) > 3 else "needs"
    if subject_type not in ("story", "task"):
        return usage

    err = await _require_goal_admin(user_uid, goal_id, pool)
    if err:
        return err

    resource_id = await _resolve_resource_id(resource_prefix, goal_id, pool)
    if not resource_id:
        return f"No active resource found with id starting with `{resource_prefix[:16]}`."
    subject_id = await _resolve_subject_id(subject_type, subject_prefix, goal_id, pool)
    if not subject_id:
        return f"No {subject_type} in this goal matched `{subject_prefix[:16]}`."

    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    from app.services.resource_allocation import grant_resource
    ok, message = await grant_resource(
        goal_id, subject_type, subject_id, resource_id, relation, actor_id=user_actor_id or "",
    )
    _log.info(
        "Slack /resources grant: goal=%s subject=%s:%s resource=%s relation=%s ok=%s by=%s",
        goal_id, subject_type, subject_id, resource_id, relation, ok, user_uid,
    )
    return ("✅ " if ok else "⚠️ ") + message


async def _handle_revoke(rest: str, goal_id: str, user_uid: str, pool, cmd: str = "resources") -> str:
    usage = f"Usage: /{cmd} revoke <resource-id> <story|task> <subject-id>"
    parts = rest.split()
    if len(parts) < 3:
        return usage
    resource_prefix, subject_type, subject_prefix = parts[0], parts[1].lower(), parts[2]
    if subject_type not in ("story", "task"):
        return usage

    err = await _require_goal_admin(user_uid, goal_id, pool)
    if err:
        return err

    resource_id = await _resolve_resource_id(resource_prefix, goal_id, pool)
    if not resource_id:
        return f"No active resource found with id starting with `{resource_prefix[:16]}`."
    subject_id = await _resolve_subject_id(subject_type, subject_prefix, goal_id, pool)
    if not subject_id:
        return f"No {subject_type} in this goal matched `{subject_prefix[:16]}`."

    user_actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", user_uid)
    from app.services.resource_allocation import revoke_resource
    ok, message = await revoke_resource(
        goal_id, subject_type, subject_id, resource_id, actor_id=user_actor_id or "",
    )
    _log.info(
        "Slack /resources revoke: goal=%s subject=%s:%s resource=%s ok=%s by=%s",
        goal_id, subject_type, subject_id, resource_id, ok, user_uid,
    )
    return ("✅ " if ok else "⚠️ ") + message


async def _handle_list(args: str, goal_id: str, cmd: str = "resources") -> str:
    pool = get_pool()
    filter_term = args.strip().lower()

    rows = await pool.fetch(
        """SELECT id, name, kind, uri, human_context,
                  repo_name, branch_name, file_path
             FROM goal_resources
            WHERE goal_id = $1 AND status = 'active'
            ORDER BY kind, name
            LIMIT 50""",
        goal_id,
    )
    if not rows:
        return (
            "No active resources linked to this goal. "
            f"Use `/{cmd} add` to open the resources page and add one."
        )

    if filter_term:
        rows = [
            r for r in rows
            if filter_term in (r["name"] or "").lower()
            or filter_term in (r["kind"] or "").lower()
            or filter_term in (r["human_context"] or "").lower()
        ]
        if not rows:
            return (
                f"No resources matched `{filter_term[:40]}`. "
                f"Run `/{cmd}` (no filter) to see all active resources."
            )

    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r["kind"] or "other", []).append(r)

    lines = [f"*Resources* ({len(rows)} active):"]
    for kind in sorted(by_kind):
        lines.append(f"\n*{kind}*")
        for r in by_kind[kind]:
            # Build a useful detail string based on resource kind
            detail = ""
            if kind == "repo" and r.get("repo_name"):
                detail = r["repo_name"]
            elif kind == "branch" and r.get("branch_name"):
                detail = r["branch_name"]
            elif kind in ("file", "document") and r.get("file_path"):
                detail = r["file_path"]
            elif r.get("uri"):
                detail = r["uri"][:80]
            note = (r.get("human_context") or "")[:60]
            line = f"  • `{r['id'][:8]}` *{r['name']}*"
            if detail:
                line += f" — {detail}"
            if note:
                line += f"\n      _{note}_"
            lines.append(line)

    lines.append(
        f"\n_Use `/{cmd} scan <id>` to refresh a resource's summary. "
        f"Use `/{cmd} grant <id> story <story-id>` to allocate it to a story._"
    )
    _log.info("Slack /resources list: goal=%s filter=%r count=%d", goal_id, filter_term or "*", len(rows))
    return "\n".join(lines)


async def _handle_scan(resource_prefix: str, goal_id: str, response_url: str,
                       cmd: str = "resources") -> str:
    if not resource_prefix:
        return f"Usage: `/{cmd} scan <resource-id>`"

    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, name, kind FROM goal_resources
            WHERE goal_id = $1 AND status = 'active'
              AND id LIKE $2 || '%'
            LIMIT 1""",
        goal_id, resource_prefix,
    )
    if not row:
        return f"No active resource found with id starting with `{resource_prefix[:16]}`."

    resource_id = row["id"]
    resource_name = row["name"]
    kind = row["kind"]

    if kind not in ("repo", "branch", "file", "document"):
        return f"Scanning is not supported for `{kind}` resources."

    asyncio.create_task(run_followup(
        response_url, f"scan {resource_name}",
        _do_scan(goal_id, resource_id, resource_name, response_url),
    ))
    _log.info("Slack /resources scan: goal=%s resource=%s name=%s", goal_id, resource_id, resource_name)
    return f"Scanning *{resource_name}* (`{resource_id[:8]}`)… result will appear here."


async def _do_scan(goal_id: str, resource_id: str, resource_name: str, response_url: str) -> None:
    from app.services.resource_scanner import scan_resource
    result = await scan_resource(goal_id, resource_id) or {}

    # Scanners signal failure two ways: an explicit {"error": ...} (scan_document),
    # or a summary string that begins with "Scan failed:" (scan_repo_* on exception).
    # Treat both as failures so we never report "Scan complete" on a failed scan.
    error = result.get("error")
    summary = (result.get("summary", "") or "")
    if error:
        _log.warning("Slack scan failed goal=%s resource=%s: %s", goal_id, resource_id, error)
        await post_followup(response_url, f"⚠️ Scan failed for *{resource_name}*: {error}")
        return
    if summary.startswith("Scan failed:"):
        _log.warning("Slack scan failed goal=%s resource=%s: %s", goal_id, resource_id, summary)
        await post_followup(response_url, f"⚠️ Scan failed for *{resource_name}*: {summary[12:].strip()}")
        return

    await post_followup(response_url, f"*Scan complete*: {resource_name}\n{summary[:500]}")
