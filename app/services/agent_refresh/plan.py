"""What a refresh WOULD do, printed for a person, changing nothing.

THE DEFAULT MODE, and the main safety feature of the whole tool. A
fleet-wide write is safe when you can read what it will do first;
everything else here is secondary to that.

WHAT CAN AND CANNOT BE DIFFED, stated honestly because a plan that
overstates its knowledge is worse than one that admits a gap:

  TOOLS   a real diff when the gateway reports them in `agents.list`.
          It may not -- nothing in this repo reads a `tools` key off
          that response today -- so the absence is reported as "cannot
          read back", never as "no change". Claiming no change on a
          missing field is how a plan tells you it will do nothing and
          then does something.

  FILES   no diff is possible: the gateway offers no file read-back.
          Reported as a re-push count. The push is an idempotent
          overwrite, so a needless one costs nothing -- but the plan
          must not pretend it knows the file differs.

  SKILLS  `skill_push_state` records a content hash PER SKILL, not per
          agent, so it answers "has this skill changed since the last
          fan-out" and not "does this agent have it". Reported at that
          resolution and labelled as such.

NO WRITES HAPPEN HERE. Not to the gateway, not to the database. The one
thing this module may do is read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.agent_refresh.eligibility import Refreshable
from app.services.agent_refresh.render import Desired

logger = logging.getLogger(__name__)


@dataclass
class AgentPlan:
    agent: Refreshable
    desired: Desired
    tools_note: str = ""
    skills_changed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def file_names(self) -> list[str]:
        return sorted(self.desired.files)


async def _live_tools(gateway_admin) -> dict[str, dict] | None:
    """Tools per agent as the gateway reports them, or None if it doesn't.

    Returning None rather than {} matters: an empty map would make every
    agent look like it has no tools and therefore a pending change.
    """
    try:
        result = await gateway_admin.list_agents()
    except Exception as exc:                                # noqa: BLE001
        logger.warning("agent refresh: agents.list failed (%s); tools "
                       "cannot be diffed", exc)
        return None
    agents = (result or {}).get("agents") or []
    out: dict[str, dict] = {}
    saw_tools = False
    for a in agents:
        tools = a.get("tools")
        if tools is not None:
            saw_tools = True
        out[str(a.get("id", "")).lower()] = tools or {}
    return out if saw_tools else None


def _tools_note(desired: Desired, live: dict | None, gw_id: str) -> str:
    if not desired.tools_allow:
        return "tools: nothing to say (empty allow list is not a change)"
    if live is None:
        # NOT A TRANSIENT FAILURE, and the wording should not suggest
        # one. `listAgentsForGateway` (openclaw
        # `src/gateway/session-utils.ts:334`) returns id, name and
        # identity only -- there is no tools field to read. So a tool
        # change cannot be previewed here, and cannot be confirmed
        # afterwards either; re-running this plan will say the same
        # thing. The push itself is an idempotent overwrite.
        return (f"tools: {len(desired.tools_allow)} allowed, "
                f"{len(desired.tools_deny)} denied -- the gateway does not "
                f"report live tools, so this cannot be diffed (it is a "
                f"no-op if they already match)")
    have = live.get(gw_id.lower())
    if have is None:
        return (f"tools: agent not in agents.list -- it may not be "
                f"registered; the push will say")
    cur_allow = sorted(have.get("allow") or [])
    cur_deny = sorted(have.get("deny") or [])
    if cur_allow == sorted(desired.tools_allow) and \
            cur_deny == sorted(desired.tools_deny):
        return ""
    added = sorted(set(desired.tools_allow) - set(cur_allow))
    removed = sorted(set(cur_allow) - set(desired.tools_allow))
    bits = []
    if added:
        bits.append(f"+{', '.join(added)}")
    if removed:
        bits.append(f"-{', '.join(removed)}")
    if sorted(desired.tools_deny) != cur_deny:
        bits.append(f"deny -> {', '.join(sorted(desired.tools_deny)) or '(none)'}")
    return "tools: " + ("; ".join(bits) or "reordered only")


async def _changed_skills(conn, desired: Desired) -> list[str]:
    """Skills whose content differs from the last recorded fan-out."""
    if not desired.skills:
        return []
    try:
        rows = await conn.fetch(
            "SELECT s.name FROM skills s "
            "  LEFT JOIN skill_push_state p ON p.skill_name = s.name "
            " WHERE s.name = ANY($1::text[]) "
            "   AND (p.content_hash IS NULL OR p.content_hash <> s.content_hash)",
            list(desired.skills))
    except Exception as exc:                                # noqa: BLE001
        logger.warning("agent refresh: skill state unreadable (%s)", exc)
        return []
    return sorted(r["name"] for r in rows)


async def plan_for(conn, gateway_admin, agents: list[Refreshable],
                   desired_by_id: dict[str, Desired]) -> list[AgentPlan]:
    """Work out what each agent would receive. Reads only."""
    live = await _live_tools(gateway_admin)
    plans: list[AgentPlan] = []
    for agent in agents:
        desired = desired_by_id[agent.gateway_agent_id]
        plans.append(AgentPlan(
            agent=agent,
            desired=desired,
            tools_note=_tools_note(desired, live, agent.gateway_agent_id),
            skills_changed=await _changed_skills(conn, desired),
            notes=list(desired.notes)))
    return plans


def render_report(plans: list[AgentPlan]) -> str:
    """The plan, as a person reads it."""
    if not plans:
        return ("No eligible agents. Either every goal is deleted, or the "
                "scope you gave matches nothing.")
    lines = [f"{len(plans)} agent(s) eligible.", ""]
    for p in plans:
        a = p.agent
        lines.append(f"  {a.gateway_agent_id}  [{a.agent_type}  goal={a.goal_id}]")
        lines.append(f"      files : {', '.join(p.file_names) or '(none)'}"
                     f"  -- pushed unconditionally, no read-back")
        if p.tools_note:
            lines.append(f"      {p.tools_note}")
        else:
            lines.append("      tools : unchanged")
        if p.desired.skills:
            changed = (f" ({len(p.skills_changed)} changed since last "
                       f"fan-out)" if p.skills_changed else "")
            lines.append(f"      skills: {len(p.desired.skills)}{changed}")
        else:
            # SAID OUT LOUD RATHER THAN OMITTED. An absent line reads as
            # "not computed" just as easily as "none", and for the
            # kernel-assistant and go-v2 templates zero is a deliberate
            # design decision worth seeing confirmed.
            lines.append("      skills: none — this template has none "
                         "by design")
        for note in p.notes:
            lines.append(f"      note  : {note}")
        lines.append("")
    lines.append("Nothing above has been written. Press Apply to write it.")
    return "\n".join(lines)
