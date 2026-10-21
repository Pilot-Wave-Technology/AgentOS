"""Goal membership service — add or remove a member from a goal.

Single source of truth for goal membership changes, shared by the web settings
UI (app/routers/goals.py add_member / remove_member) and the Slack /goal
invite / remove commands.

Adding a member is more than an INSERT: it emits the ledger event, auto-provisions
the member's SHCA + standard IAs, notifies the GO agent, and provisions a channel
for the member on every enabled platform (Slack/Notion/…). Keeping all of that in
one place means every entry point behaves identically. Removing a member is the
mirror image: precondition checks (finish or hand off active work first), the
ledger event, SHCA deprovisioning, a GO notice, and channel deprovisioning —
plus freezing the member's SHCA thread(s) read-only and goal-wide visible
rather than deleting them.

add_goal_member returns (ok, message) — expected failures (unknown user, not
authorised) come back as (False, reason). remove_goal_member returns a dict
instead, since removal has a third outcome (needs_reassignment) beyond
success/fail. Neither function raises on expected failures or returns HTTP
objects — that's the caller's job.
"""
import logging

from app.database import get_pool
from app.roles import GoalRole

_log = logging.getLogger(__name__)

_VALID_ROLES = (*GoalRole.ALL, "viewer")


async def count_goal_admins(pool, goal_id: str) -> int:
    """How many goal.manager members `goal_id` currently has.

    Single source of truth for the last-admin guard, shared by every
    membership mutation that must never leave a goal without a manager:
    add (demotion-via-reinvite), role change, and removal.
    """
    return await pool.fetchval(
        "SELECT COUNT(*) FROM goal_members WHERE goal_id = $1 AND role = $2",
        goal_id, GoalRole.GOAL_ADMIN,
    )


async def add_goal_member(
    goal_id: str,
    target_uid: str,
    role: str,
    added_by_uid: str,
    added_by_actor_id,
) -> tuple[bool, str]:
    """Add target_uid to goal_id with the given role, running the full provisioning chain.

    Caller is responsible for authorising added_by_uid (admin check) before calling.
    Idempotent on membership: ON CONFLICT updates the role — this is deliberate,
    e.g. Slack's `/goal invite @user admin` is a legitimate way to promote an
    existing member. But that same idempotency means re-adding the last
    goal.manager with a lower role would silently demote them via this path,
    bypassing update_member_role's last-admin guard entirely — so that guard
    is re-checked here too (see count_goal_admins).
    """
    if role not in _VALID_ROLES:
        return False, f"Role must be one of: {', '.join(_VALID_ROLES)}."

    pool = get_pool()
    target = await pool.fetchrow(
        "SELECT uid, display_name FROM users WHERE uid = $1", target_uid,
    )
    if not target:
        return False, "User not found."

    display_name = target.get("display_name") or target_uid

    existing = await pool.fetchrow(
        "SELECT role FROM goal_members WHERE goal_id = $1 AND uid = $2", goal_id, target_uid,
    )
    if existing and existing["role"] == GoalRole.GOAL_ADMIN and role != GoalRole.GOAL_ADMIN:
        if await count_goal_admins(pool, goal_id) <= 1:
            _log.warning(
                "add_goal_member: blocked last-admin demotion-via-add goal=%s uid=%s by=%s",
                goal_id, target_uid, added_by_uid,
            )
            return False, (
                f"{display_name} is the only goal.manager for this goal. Promote "
                f"another member to goal.manager first if you want to change this."
            )

    from app.ledger.journal import append_event
    from app.ledger.schema import GOAL_MEMBER_ADDED
    async with pool.acquire() as conn, conn.transaction():
        await append_event(
            conn,
            event_type=GOAL_MEMBER_ADDED,
            payload={
                "goal_id": goal_id,
                "uid": target_uid,
                "role": role,
                "added_by": added_by_uid,
            },
            actor_id=added_by_actor_id,
            context_goal_id=goal_id,
        )
        await conn.execute(
            """INSERT INTO goal_members (goal_id, uid, role, added_by)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (goal_id, uid) DO UPDATE SET role = $3""",
            goal_id, target_uid, role, added_by_uid,
        )

    # Auto-provision: add the user's SHCAs + standard IAs to the goal.
    target_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", target_uid,
    )
    try:
        from app.assistant_provisioning import auto_provision_goal_agents
        result = await auto_provision_goal_agents(
            goal_id, target_uid, target_actor_id, target.get("display_name", ""),
        )
        _log.info("add_goal_member: auto-provisioned goal=%s uid=%s: %s", goal_id, target_uid, result)
    except Exception as exc:
        _log.warning("add_goal_member: auto-provision failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    # Seed thread_participants so the new member receives unread badge counts
    # for the GO thread from the moment they join. Non-fatal — the sidebar
    # backfills a missing row on first page visit via mark_thread_read.
    try:
        await pool.execute(
            """
            INSERT INTO thread_participants (thread_id, uid)
            SELECT g.go_thread_id, $1
              FROM goals g
             WHERE g.id = $2
               AND g.go_thread_id IS NOT NULL
               AND g.go_thread_id != ''
            ON CONFLICT (thread_id, uid) DO NOTHING
            """,
            target_uid, goal_id,
        )
        _log.debug(
            "add_goal_member: thread_participants seeded for uid=%s goal=%s",
            target_uid, goal_id,
        )
    except Exception as exc:
        _log.warning(
            "add_goal_member: thread_participants seed failed goal=%s uid=%s: %s",
            goal_id, target_uid, exc,
        )

    # Notify the GO agent of the new member.
    try:
        from app.services.go_events import on_member_added
        await on_member_added(goal_id, target_uid, role, added_by_uid)
    except Exception as exc:
        _log.warning("add_goal_member: GO notify failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    # Provision a channel for the member on every platform enabled for this goal.
    from app.channels.registry import registered_platforms, get_provisioner
    for platform in registered_platforms():
        provisioner = get_provisioner(platform)
        if not provisioner:
            continue
        try:
            await provisioner.provision_member(goal_id, target_uid, pool)
        except Exception as exc:
            _log.warning(
                "add_goal_member: channel provision failed platform=%s goal=%s uid=%s: %s",
                platform, goal_id, target_uid, exc,
            )

    # Emit member.joined once after all provisioners have run.
    try:
        from app.channels.base import relay_lifecycle_event
        display_name = target.get("display_name") or target_uid
        await relay_lifecycle_event(goal_id, "member.joined", {
            "uid": target_uid, "display_name": display_name, "role": role,
        })
    except Exception as exc:
        _log.warning("add_goal_member: member.joined relay failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    # Rejoin: flip the user's SHCA thread(s) for this goal back to private.
    # They may have been marked left_goal_at by a prior removal (read-only,
    # visible to every current member — see remove_goal_member) — reusing the
    # same thread_id rather than starting fresh means conversation history and
    # goal/global-scope memory carry straight through.
    try:
        from app.services.agent_registry.assistant_queries import list_user_assistant_agents
        assistant_agents = await list_user_assistant_agents(pool, target_actor_id)
        agent_ids = [a["agent_id"] for a in assistant_agents]
        if agent_ids:
            restored = await pool.fetch(
                """UPDATE threads SET left_goal_at = NULL
                    WHERE goal_id = $1 AND created_by = $2 AND agent_id = ANY($3::text[])
                      AND left_goal_at IS NOT NULL
                    RETURNING id""",
                goal_id, target_uid, agent_ids,
            )
            if restored:
                _log.info(
                    "add_goal_member: restored %d thread(s) to private goal=%s uid=%s",
                    len(restored), goal_id, target_uid,
                )
    except Exception as exc:
        _log.warning("add_goal_member: thread restore failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    _log.info("add_goal_member: goal=%s uid=%s role=%s by=%s", goal_id, target_uid, role, added_by_uid)
    if existing is None:
        return True, f"Added {display_name} to the goal as {role}."
    if existing["role"] == role:
        return True, f"{display_name} is already a member with role {role}. No changes made."
    return True, f"{display_name} is already a member — role updated from {existing['role']} to {role}."


async def remove_goal_member(
    goal_id: str,
    target_uid: str,
    removed_by_uid: str,
    removed_by_actor_id,
    reassignments: dict | None = None,
) -> dict:
    """Remove target_uid from goal_id, running the full deprovisioning chain.

    Caller is responsible for authorising removed_by_uid (admin check) before
    calling. Unlike add_goal_member's (ok, message) tuple, this returns a dict:
    the removal can also come back needing a reassignment decision from the
    caller before it can proceed, which is a third outcome beyond success/fail.

    reassignments maps story_id -> new assignee uid, for the target's stories
    that haven't been started yet (state in proposed/accepted). Stories already
    in progress, and any non-terminal task, block removal outright — no
    mid-flight reassignment for those; the assignee (or someone else acting on
    their behalf) has to finish them first.
    """
    reassignments = reassignments or {}
    pool = get_pool()

    target = await pool.fetchrow(
        "SELECT uid, display_name, actor_id FROM users WHERE uid = $1", target_uid,
    )
    if not target:
        return {"ok": False, "message": "User not found."}

    member_row = await pool.fetchrow(
        "SELECT role FROM goal_members WHERE goal_id = $1 AND uid = $2", goal_id, target_uid,
    )
    if not member_row:
        return {"ok": False, "message": "User is not a member of this goal."}

    # Last-admin guard — shared with update_member_role (app/routers/goals.py)
    # and add_goal_member above, applied identically whether removing self or
    # someone else: removal is strictly more destructive than demotion (no
    # "promote someone else" recovery once the row is gone).
    if member_row["role"] == GoalRole.GOAL_ADMIN:
        if await count_goal_admins(pool, goal_id) <= 1:
            return {
                "ok": False,
                "message": "Cannot remove the only goal.manager. Promote another member first.",
            }

    display_name = target.get("display_name") or target_uid

    # Task check — no reassignment path offered; must simply be finished.
    active_tasks = await pool.fetch(
        """SELECT id, title FROM tasks
            WHERE goal_id = $1 AND assigned_to = $2 AND status = 'active'
              AND state NOT IN ('done', 'failed', 'rejected', 'cancelled')
            ORDER BY created_at""",
        goal_id, target_uid,
    )
    if active_tasks:
        names = ", ".join(f'"{t["title"]}"' for t in active_tasks[:5])
        return {
            "ok": False,
            "message": (
                f"{display_name} has {len(active_tasks)} task(s) still in progress: "
                f"{names}. These must be completed before removal."
            ),
        }

    # Started-story check — same rule as tasks: no mid-flight reassignment.
    started_stories = await pool.fetch(
        """SELECT id, title FROM stories
            WHERE goal_id = $1 AND assigned_to = $2
              AND state IN ('in_progress', 'blocked', 'awaiting_merge')
            ORDER BY created_at""",
        goal_id, target_uid,
    )
    if started_stories:
        names = ", ".join(f'"{s["title"]}"' for s in started_stories[:5])
        return {
            "ok": False,
            "message": (
                f"{display_name} has {len(started_stories)} story(ies) already in "
                f"progress: {names}. These must be completed before removal."
            ),
        }

    # Not-started-story check — these can be handed to someone else as part
    # of this removal, unlike the two hard gates above.
    pending_stories = await pool.fetch(
        """SELECT id, title FROM stories
            WHERE goal_id = $1 AND assigned_to = $2 AND state IN ('proposed', 'accepted')
            ORDER BY created_at""",
        goal_id, target_uid,
    )
    unresolved = [s for s in pending_stories if s["id"] not in reassignments]
    if unresolved:
        candidate_rows = await pool.fetch(
            """SELECT u.uid, u.display_name FROM goal_members gm
                 JOIN users u ON u.uid = gm.uid
                WHERE gm.goal_id = $1 AND gm.uid != $2
                ORDER BY u.display_name""",
            goal_id, target_uid,
        )
        return {
            "ok": False,
            "status": "needs_reassignment",
            "stories": [{"id": s["id"], "title": s["title"]} for s in pending_stories],
            "candidates": [
                {"uid": c["uid"], "name": c["display_name"] or c["uid"]} for c in candidate_rows
            ],
        }

    if pending_stories:
        valid_uids = {
            r["uid"] for r in await pool.fetch(
                "SELECT uid FROM goal_members WHERE goal_id = $1 AND uid != $2",
                goal_id, target_uid,
            )
        }
        for story in pending_stories:
            new_uid = reassignments.get(story["id"])
            if new_uid not in valid_uids:
                return {
                    "ok": False,
                    "message": f"'{new_uid}' is not a current member of this goal.",
                }

        from app.ledger.journal import append_event
        from app.ledger.schema import STORY_ASSIGNMENT_CHANGED
        async with pool.acquire() as conn, conn.transaction():
            for story in pending_stories:
                new_uid = reassignments[story["id"]]
                await conn.execute(
                    "UPDATE stories SET assigned_to = $1, updated_at = now() "
                    "WHERE id = $2 AND goal_id = $3",
                    new_uid, story["id"], goal_id,
                )
                await append_event(
                    conn,
                    event_type=STORY_ASSIGNMENT_CHANGED,
                    payload={
                        "story_id": story["id"],
                        "from_assignee": target_uid,
                        "to_assignee": new_uid,
                        "requested_by_uid": removed_by_uid,
                    },
                    actor_id=removed_by_actor_id,
                    context_goal_id=goal_id,
                )
        _log.info(
            "remove_goal_member: reassigned %d stor(y/ies) goal=%s from=%s by=%s",
            len(pending_stories), goal_id, target_uid, removed_by_uid,
        )

    from app.ledger.journal import append_event
    from app.ledger.schema import GOAL_MEMBER_REMOVED
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM goal_members WHERE goal_id = $1 AND uid = $2", goal_id, target_uid,
        )
        await append_event(
            conn,
            event_type=GOAL_MEMBER_REMOVED,
            payload={
                "goal_id": goal_id,
                "uid": target_uid,
                "previous_role": member_row["role"],
                "removed_by": removed_by_uid,
            },
            actor_id=removed_by_actor_id,
            context_goal_id=goal_id,
        )

    # Freeze the removed member's SHCA thread(s) for this goal: read-only,
    # visible to any current goal member from now on (see threads_left_goal_at
    # migration). Same thread reused if they rejoin — never deleted or forked.
    try:
        if target["actor_id"]:
            from app.services.agent_registry.assistant_queries import list_user_assistant_agents
            assistant_agents = await list_user_assistant_agents(pool, target["actor_id"])
            agent_ids = [a["agent_id"] for a in assistant_agents]
            if agent_ids:
                frozen = await pool.fetch(
                    """UPDATE threads SET left_goal_at = now()
                        WHERE goal_id = $1 AND created_by = $2 AND agent_id = ANY($3::text[])
                          AND left_goal_at IS NULL
                        RETURNING id""",
                    goal_id, target_uid, agent_ids,
                )
                _log.info(
                    "remove_goal_member: froze %d thread(s) goal=%s uid=%s",
                    len(frozen), goal_id, target_uid,
                )
    except Exception as exc:
        _log.warning(
            "remove_goal_member: thread freeze failed goal=%s uid=%s: %s", goal_id, target_uid, exc,
        )

    # Deprovision: remove the user's SHCAs from this goal's agent_contexts
    # (IAs stay — they're shared).
    try:
        from app.assistant_provisioning import deprovision_user_from_goal
        removed_assistants = await deprovision_user_from_goal(goal_id, target_uid)
        _log.info(
            "remove_goal_member: deprovisioned assistants=%s goal=%s uid=%s",
            removed_assistants, goal_id, target_uid,
        )
    except Exception as exc:
        _log.warning("remove_goal_member: deprovision failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    # Notify the GO agent.
    try:
        from app.services.go_events import on_member_removed
        await on_member_removed(goal_id, target_uid, removed_by_uid)
    except Exception as exc:
        _log.warning("remove_goal_member: GO notify failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    # Deprovision channel access on every platform enabled for this goal.
    from app.channels.registry import registered_platforms, get_provisioner
    for platform in registered_platforms():
        provisioner = get_provisioner(platform)
        if not provisioner:
            continue
        try:
            await provisioner.deprovision_member(goal_id, target_uid, pool)
        except Exception as exc:
            _log.warning(
                "remove_goal_member: channel deprovision failed platform=%s goal=%s uid=%s: %s",
                platform, goal_id, target_uid, exc,
            )

    # Emit member.left once after all deprovisioners have run.
    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(goal_id, "member.left", {
            "uid": target_uid, "display_name": display_name,
        })
    except Exception as exc:
        _log.warning("remove_goal_member: member.left relay failed goal=%s uid=%s: %s", goal_id, target_uid, exc)

    _log.info("remove_goal_member: goal=%s uid=%s by=%s", goal_id, target_uid, removed_by_uid)
    return {"ok": True, "message": f"Removed *{display_name}* from the goal."}
