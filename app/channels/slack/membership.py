"""Who can see a channel: exactly the people who can see its thread.

THE WEB RULE IS TWO MODES, not one, and Slack has to reproduce both
(`app/routers/chat.py:426-437`):

    participants = thread_row.get("participants") or []
    if participants and uid not in participants and uid != created_by:
        # denied

  * `participants` EMPTY  -> every member of the goal may read it
  * `participants` SET    -> those uids plus created_by, and nobody else

Both additionally require goal membership. A channel that mirrors the
wrong mode is not a cosmetic bug: it either hides a conversation from
people entitled to it, or shows a private one to the whole goal.

A TRAP WORTH NAMING. `thread_participants` is NOT the access list. It
is the unread-counter cache, and its own migration backfills it with
every goal member for GO threads -- keying visibility off it would put
the entire goal into every GO channel. The access list is
`threads.participants`.

AND MEMBERSHIP DECAYS. Any human already in a private channel can
invite another human, and Slack offers no way to forbid it. So
"visible only to thread participants" is true at creation and drifts
from there. `reconcile_channel` is the answer: it removes anyone who
should not be there and says so in the channel, because a silent
removal is its own kind of rude.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def expected_uids(pool, thread_id: str) -> set[str]:
    """The control-panel uids entitled to read this thread.

    Returns an empty set when the thread does not exist, which reads
    correctly downstream: nobody is entitled to a thread that is gone.
    """
    row = await pool.fetchrow(
        "SELECT goal_id, created_by, participants FROM threads WHERE id = $1",
        thread_id)
    if not row:
        return set()

    participants = list(row["participants"] or [])
    if participants:
        uids = set(participants)
        if row["created_by"]:
            uids.add(row["created_by"])
    else:
        # Mode one: the whole goal. Read from goal_members rather than
        # from thread_participants — see the module docstring.
        uids = {r["uid"] for r in await pool.fetch(
            "SELECT uid FROM goal_members WHERE goal_id = $1", row["goal_id"])}

    # Goal membership gates both modes, so somebody removed from the goal
    # loses the thread even if they are still named on it.
    if participants:
        members = {r["uid"] for r in await pool.fetch(
            "SELECT uid FROM goal_members WHERE goal_id = $1", row["goal_id"])}
        uids &= members
    return uids


async def _slack_ids_for(pool, uids: set[str]) -> dict[str, str]:
    """uid -> Slack user id, for the ones we have a confirmed mapping for.

    Somebody with no mapping is simply not invited. Guessing by email
    here would bypass `platform_user_map`'s review state, which exists
    precisely so a half-matched identity cannot be handed access.
    """
    if not uids:
        return {}
    rows = await pool.fetch(
        """
        SELECT u.uid, m.external_user_id
          FROM platform_user_map m
          JOIN users u ON u.actor_id = m.actor_id
         WHERE m.platform = 'slack' AND m.mapping_status = 'confirmed'
           AND u.uid = ANY($1::text[])
        """, list(uids))
    return {r["uid"]: r["external_user_id"] for r in rows}


async def sync_channel_members(pool, thread_id: str, *, kick: bool = True) -> dict:
    """Make the channel's membership match the thread's.

    `kick=False` is for the invite-only paths (somebody was just added),
    where walking the whole channel to remove strangers is work nobody
    asked for. The reconcile pass is what kicks.

    Returns a summary rather than raising: this runs on the edge of the
    request that added a participant, and Slack being unreachable must
    not fail that.
    """
    out = {"invited": [], "kicked": [], "skipped": 0, "error": ""}

    binding = await pool.fetchrow(
        """
        SELECT b.external_channel_id, b.integration_id, pi.credentials_json
          FROM platform_channel_bindings b
          JOIN platform_integrations pi ON pi.id = b.integration_id
         WHERE b.thread_id = $1 AND b.archived_at IS NULL
           AND pi.status = 'active'
        """, thread_id)
    if not binding or not binding["external_channel_id"]:
        return out

    creds = binding["credentials_json"]
    if isinstance(creds, str):
        import json
        creds = json.loads(creds) if creds else {}
    token = (creds or {}).get("bot_token", "")
    bot_user_id = (creds or {}).get("bot_user_id", "")
    if not token:
        return out

    channel = binding["external_channel_id"]
    want_uids = await expected_uids(pool, thread_id)
    want = await _slack_ids_for(pool, want_uids)
    out["skipped"] = len(want_uids) - len(want)

    from app.channels.slack.client import make_async_client, on_failure
    client = make_async_client(token)

    try:
        resp = await client.conversations_members(channel=channel)
        current = set(resp.get("members") or [])
    except Exception as exc:                                   # noqa: BLE001
        await on_failure(exc, pool=pool,
                         integration_id=str(binding["integration_id"]),
                         where="read channel members")
        out["error"] = "could not read members"
        return out

    # The bot is in the channel because it has to be.
    current.discard(bot_user_id)

    for uid, slack_id in want.items():
        if slack_id in current:
            continue
        try:
            await client.conversations_invite(channel=channel, users=slack_id)
            out["invited"].append(uid)
        except Exception as exc:                               # noqa: BLE001
            from app.channels.slack.client import error_code
            if error_code(exc) != "already_in_channel":
                _log.warning("Slack: could not invite %s to %s: %s",
                             uid, channel, error_code(exc) or exc)

    if kick:
        for slack_id in current - set(want.values()):
            try:
                await client.conversations_kick(channel=channel, user=slack_id)
                out["kicked"].append(slack_id)
            except Exception as exc:                           # noqa: BLE001
                from app.channels.slack.client import error_code
                # cant_kick_self and cant_kick_from_general are both
                # "correct, nothing to do".
                if error_code(exc) not in ("cant_kick_self",
                                           "cant_kick_from_general"):
                    _log.warning("Slack: could not remove %s from %s: %s",
                                 slack_id, channel, error_code(exc) or exc)

    if out["kicked"]:
        # Never silently. Somebody losing access should be able to see
        # that it happened and why.
        try:
            await client.chat_postMessage(
                channel=channel,
                text=(f"Removed {len(out['kicked'])} member(s) who are not "
                      f"part of this thread. Membership here follows the "
                      f"thread in the control panel."),
                username="AgentOS")
        except Exception:                                      # noqa: BLE001
            pass

    if out["invited"] or out["kicked"]:
        _log.info("Slack membership sync thread=%s invited=%d removed=%d",
                  thread_id[:12], len(out["invited"]), len(out["kicked"]))
    return out
