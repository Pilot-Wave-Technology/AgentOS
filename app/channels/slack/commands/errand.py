"""`/errand` — decide the autonomous runs the GO wants to start.

AN ERRAND PROPOSAL IS A STANDING PERMISSION, which is why only a
human can grant one and why it waits. Before this it waited in the
panel: the proposal card existed in the database and had no Slack
path at all, so a GO asking "may I run this nightly" asked somebody
who was not looking.

Approve and decline both go through `errand_grants`, the same
functions the panel's route calls, inside the same transaction shape.
A decline carries a reason so the GO does not simply propose it again.
"""

from __future__ import annotations

import logging

from app.channels.slack.commands._context import resolve

_log = logging.getLogger(__name__)

USAGE = (
    "/errand — autonomous runs waiting for a decision\n"
    "\n"
    "  /errand                      list the open proposals\n"
    "  /errand approve <id>         approve one — you own the run\n"
    "  /errand decline <id> <why>   refuse it, with a reason"
)


async def handle(args: str, channel_id: str, team_id: str, *,
                 slack_user_id: str = "", response_url: str = "",
                 command: str = "errand") -> str:
    from app.channels.slack.thread_map import get_user_uid
    from app.database import get_pool

    pool = get_pool()
    parts = (args or "").strip().split(None, 2)
    sub = parts[0].lower() if parts else ""

    if sub in ("help", "-h", "--help"):
        return USAGE

    ctx = await resolve(channel_id, team_id, pool)
    if not ctx.ok:
        return "This channel isn't connected to a goal."

    user_uid = await get_user_uid(slack_user_id, pool=pool)
    if not user_uid:
        return ("I don't recognise your account. Sign in to the control panel "
                "with the same email as your Slack account.")

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, ctx.goal_id) is None:
        return "You don't have access to this goal."

    if sub not in ("approve", "decline"):
        return await _list(pool, ctx.goal_id)

    if len(parts) < 2 or not parts[1].strip().isdigit():
        return f"Which proposal? `/errand {sub} <id>` — ids are in `/errand`."
    proposal_id = int(parts[1].strip())
    reason = parts[2].strip() if len(parts) > 2 else ""

    if sub == "decline" and not reason:
        return ("A decline needs a reason, or the GO proposes it again: "
                "`/errand decline <id> <why>`.")

    owner = await pool.fetchval(
        "SELECT goal_id FROM kernel.errand_proposals WHERE id = $1",
        proposal_id)
    if not owner:
        return f"No proposal `{proposal_id}`."
    # The goal in the channel and the goal on the proposal must agree:
    # membership of one goal is not permission over another's errands.
    if owner != ctx.goal_id:
        return f"Proposal `{proposal_id}` doesn't belong to this goal."

    from app.services.errand_grants import approve, decline
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if sub == "approve":
                    await approve(conn, ctx.goal_id, proposal_id, user_uid)
                else:
                    await decline(conn, ctx.goal_id, proposal_id, user_uid,
                                  reason)
    except Exception as exc:                                   # noqa: BLE001
        return f"Couldn't record that: {exc}"
    return ("Approved — it will run, and you own it."
            if sub == "approve" else "Declined.")


async def _list(pool, goal_id: str) -> str:
    rows = await pool.fetch(
        """
        SELECT id, state, name, task, cron, created_at
          FROM kernel.errand_proposals
         WHERE goal_id = $1 AND state = 'PROPOSED'
         ORDER BY id
        """, goal_id)
    if not rows:
        return "No errand proposals are waiting."
    lines = [f"*Errand proposals waiting ({len(rows)}):*"]
    for r in rows:
        label = (r["name"] or "").strip()
        if not label:
            label = ((r["task"] or "").strip().splitlines() or [""])[0]
        when = f" — `{r['cron']}`" if r["cron"] else ""
        lines.append(f"  • `{r['id']}` {label}{when}")
    lines.append("")
    lines.append("`/errand approve <id>` or `/errand decline <id> <why>`.")
    return "\n".join(lines)
