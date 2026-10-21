"""Which stories an assistant may write against.

The rule asked for is *"the stories they have worked on"*, and the
assistant's own name is enough to answer it — because since Stage K a
story an assistant has worked has a THREAD on that assistant, and
`threads.agent_id` says whose.

WHY THIS TOOK A GATEWAY CHANGE. The actor header was
`assistant:{goalId}`: it named the goal, which the URL path already
carried, and not the caller. Every assistant in a goal sent the
identical string, so `assistant_requests.actor_id` recorded every ask as
"assistant:KER14" whoever made it, and nothing downstream could tell one
person's assistant from another's. The agent id was one field along in
the session key the tool already parses. It now sends
`assistant:{agentId}`.

WHY THE THREAD AND NOT THE ASSIGNEE. `stories.assigned_to` holds a uid,
and going assistant → owner → uid → assignee is three joins to answer a
question one already answers. It is also the WRONG question: an
assistant that ran a story for somebody else's assignment has worked it,
and one assigned a story it has never opened has not. The thread is the
record of having worked it.

FAILING SOFT, ON PURPOSE. A gateway that predates this change still
sends `assistant:{goalId}`, which is detectable — the second part is the
goal in the URL — and the check then falls back to goal scope with a
loud log. Refusing outright would make a low-stakes write into a hard
deploy-order dependency, and the failure would look like a broken tool
rather than a stale gateway.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def agent_of(actor_id: str) -> str:
    """The assistant's id out of `assistant:{agentId}`, or "" if absent."""
    actor = (actor_id or "").strip()
    if not actor.startswith("assistant:"):
        return ""
    return actor.split(":", 1)[1].strip()


def names_an_agent(actor_id: str, goal_id: str) -> bool:
    """Is this the new actor shape, or the old goal-shaped one?

    Deterministic rather than heuristic: the old form put the goal id
    where the agent id now goes, so an actor whose second part IS this
    goal is the old form and nothing else.
    """
    agent = agent_of(actor_id)
    return bool(agent) and agent.lower() != (goal_id or "").lower()


async def worked_by(conn, goal_id: str, actor_id: str, story_id: str) -> bool:
    """Has THIS assistant worked this story?

    True when it holds a thread for it. The gateway agent id is
    `{agents.id}-{goal}`, so the match is exact-or-prefix and
    case-insensitive — the gateway lowercases ids, and a thread's
    `agent_id` is the un-normalised one. Same shape as the resolver in
    `gateway_sessions.py`, deliberately.
    """
    agent = agent_of(actor_id)
    if not agent:
        return False
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM threads t
         WHERE t.story_id = $1 AND t.goal_id = $2
           AND ( lower($3) = lower(t.agent_id)
              OR starts_with(lower($3), lower(t.agent_id) || '-') )
         LIMIT 1
        """, story_id, goal_id, agent))


async def may_write(conn, goal_id: str, actor_id: str,
                    story_id: str) -> tuple[bool, str]:
    """(allowed, why-not). The one place the rule lives.

    Three outcomes rather than two, because "the caller cannot be
    identified" is a different situation from "this is not your story"
    and silently treating them alike is how a downgrade hides.
    """
    if not names_an_agent(actor_id, goal_id):
        # Old gateway. Fall back to goal scope so the tool still works,
        # and say so at WARNING — an attribution gap that logs at debug
        # is one nobody finds until they need the attribution.
        logger.warning(
            "knowledge write from an UNIDENTIFIED assistant (actor=%r) — "
            "the gateway predates the agent-id header, so this write is "
            "scoped to the goal and attributed to nobody. Redeploy the "
            "gateway.", actor_id)
        in_goal = await conn.fetchval(
            "SELECT 1 FROM stories WHERE id = $1 AND goal_id = $2",
            story_id, goal_id)
        return (bool(in_goal),
                "" if in_goal else f"story {story_id} is not in this goal")

    if await worked_by(conn, goal_id, actor_id, story_id):
        return True, ""

    in_goal = await conn.fetchval(
        "SELECT 1 FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id)
    if not in_goal:
        return False, f"story {story_id} is not in this goal"
    return False, (
        f"story {story_id} is in this goal but you have not worked it — "
        f"there is no thread for it on you. Read it with `goal_knowledge` "
        f"and `story_progress`, both of which are goal-wide; recording "
        f"against it is for whoever ran it, so the record says who "
        f"learned the thing")
