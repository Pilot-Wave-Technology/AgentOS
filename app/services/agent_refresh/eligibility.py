"""Which agents a refresh may touch: live agents of live goals.

THIS IS PHASE ONE AND NOT A FILTER ADDED LATER. A refresh walks the whole
fleet, and the last thing in this system that did so -- `skill_sync`'s
startup reconcile -- spammed because it swept deleted-goal zombies. A GO
session never idle-evicts, so a deleted goal leaves an agent that looks
perfectly healthy to any query that does not check its goal. Getting the
set right is the difference between a maintenance job and a fleet-wide
incident.

TWO INSTANCE MODELS, AND MISSING ONE IS SILENT. Copied deliberately from
`app/memory_v2/routers.py::admin_prompt_repush_all`, which carries the
scar:

  * SHCA instances live in `agent_instances` -- one row per thread, with
    `template_id` pointing at the template in `agents`.
  * GO instances live in the `agents` table itself: a per-goal row with
    `is_template = FALSE`, `type = 'GO'`, reached from its goal through
    `agent_contexts`. They are NOT in `agent_instances`, so an
    instance-only query returns every assistant and no orchestrator at
    all -- the 2026-06-12 bug that UNION was written to fix.

`goals.status = 'active'` IS THE LIVENESS TEST, not `goals.state`. The
lifecycle a person drives -- planning, paused, done -- lives in `state`;
`status` is the soft-delete marker. Gating on `state` would skip paused
goals, whose agents are exactly the ones a refresh should reach, and
would still include deleted ones.

ONE ID, NOT TWO. An earlier draft carried the raw composed instance id
alongside the normalized `gateway_agent_id`, because CP builds SESSION
KEYS from the raw one and the two are not interchangeable there. This
tool never touches a session key: every call it makes is an `agents.*`
RPC, which resolves its target through `normalizeAgentId`, so the stored
`gateway_agent_id` is both correct and already normalized. `repush-all`
pushes the same way in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TYPES = ("SHCA", "GO")


@dataclass(frozen=True)
class Refreshable:
    """One agent a refresh may write to."""

    gateway_agent_id: str
    agent_type: str
    template_id: str
    goal_id: str
    thread_id: str | None
    workspace: str | None

    @property
    def is_go(self) -> bool:
        return self.agent_type == "GO"


_SQL = """
    SELECT ai.gateway_agent_id, a.type AS agent_type,
           ai.template_id, ai.goal_id, ai.thread_id, ai.workspace
      FROM agent_instances ai
      JOIN agents a  ON a.id = ai.template_id
      JOIN goals  gl ON gl.id = ai.goal_id
     WHERE ai.status = 'active'
       AND gl.status = 'active'
       AND a.type = ANY($1::text[])
       AND ai.gateway_agent_id IS NOT NULL
    UNION
    SELECT ga.gateway_agent_id, ga.type AS agent_type,
           COALESCE(ga.template_id, ga.id) AS template_id,
           ac.goal_id, NULL AS thread_id, ga.workspace
      FROM agents ga
      JOIN agent_contexts ac ON ac.agent_id = ga.id
      JOIN goals          gl ON gl.id = ac.goal_id
     WHERE ga.is_template = FALSE
       AND ga.status = 'active'
       AND gl.status = 'active'
       AND ga.type = ANY($1::text[])
       AND ga.gateway_agent_id IS NOT NULL
"""


async def refreshable_agents(conn, *, goal_id: str = "",
                             agent_type: str = "") -> list[Refreshable]:
    """Every agent a refresh may write to, optionally narrowed.

    `goal_id` is the blast-radius control: prove a refresh on one goal
    before running it across the fleet. Filtered in Python rather than
    in SQL so that the enumeration above stays ONE query with one
    definition of "live" -- a second, narrower query is how the scoped
    and unscoped runs would come to disagree about who is eligible.
    """
    wanted = (agent_type or "").strip().upper()
    if wanted and wanted not in TYPES:
        raise ValueError(
            f"agent_type must be one of {', '.join(TYPES)}, or empty for "
            f"both; got {agent_type!r}")
    types = [wanted] if wanted else list(TYPES)

    rows = await conn.fetch(_SQL, types)
    out: list[Refreshable] = []
    seen: set[str] = set()
    for r in rows:
        gw = (r["gateway_agent_id"] or "").strip()
        # An agent can surface from both arms; the first wins and the
        # duplicate is dropped rather than pushed to twice.
        if not gw or gw.lower() in seen:
            continue
        if goal_id and r["goal_id"] != goal_id:
            continue
        seen.add(gw.lower())
        out.append(Refreshable(
            gateway_agent_id=gw,
            agent_type=r["agent_type"],
            template_id=r["template_id"],
            goal_id=r["goal_id"],
            thread_id=r["thread_id"],
            workspace=r["workspace"]))

    by_type: dict[str, int] = {}
    for a in out:
        by_type[a.agent_type] = by_type.get(a.agent_type, 0) + 1
    # Logged before anything is written, so a coverage gap -- "0 GOs" --
    # is visible in the record rather than inferred from what changed.
    logger.info("agent refresh: %d eligible agent(s) by type=%s%s",
                len(out), by_type,
                f" (goal={goal_id})" if goal_id else "")
    return out
