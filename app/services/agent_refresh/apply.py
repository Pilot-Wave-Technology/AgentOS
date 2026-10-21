"""Push the rendered content to one agent, and report honestly.

PUSH ORDER IS PROMPTS, THEN TOOLS, THEN SKILLS, and it is not arbitrary.
Only prompts are inert: the gateway caches bootstrap files per session
(`src/agents/bootstrap-cache.ts`), so a pushed `AGENTS.md` changes
nothing until the restart. Tools are re-resolved per run and skills are
read on demand, so pushing either of those IS activating it. Writing the
inert thing first keeps the live changes as close to the restart as
possible.

PARTIAL IS THE OUTCOME THAT MATTERS. A whole agent failing is harmless:
it stays exactly as it was, which is where it started. The dangerous
shape is one agent half-succeeding -- prompts written, tools refused --
because after the restart that agent runs new prompts describing tools
it does not have. That is the mismatch the whole design exists to
prevent, and it can only happen inside a single agent. So it gets its own
flag, and the caller refuses to offer a restart when any agent carries
it.

ONE AGENT'S FAILURE NEVER ABORTS THE RUN. Stopping at the first error
would leave the fleet split between refreshed and not, with no record of
which -- worse than finishing and printing a list, because the operator
cannot act on what they cannot see.

EVERY FILENAME IS RE-CHECKED HERE. `render` already filters to the
managed set, so this is redundant on purpose: it makes the renderer no
longer the only thing standing between a bug and an agent's `MEMORY.md`.
The gateway will not refuse a memory write -- its own allowlist permits
one -- so the last check has to be ours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.agent_refresh.eligibility import Refreshable
from app.services.agent_refresh.managed import is_writable, refuse_reason
from app.services.agent_refresh.render import Desired

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What actually happened to one agent."""

    gateway_agent_id: str
    files: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    tools_set: bool = False
    failures: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.files or self.skills or self.tools_set)

    @property
    def partial(self) -> bool:
        """Something landed AND something did not -- the unsafe shape."""
        return bool(self.failures) and self.touched

    @property
    def clean(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        bits = []
        if self.files:
            bits.append(f"{len(self.files)} file(s)")
        if self.tools_set:
            bits.append("tools")
        if self.skills:
            bits.append(f"{len(self.skills)} skill(s)")
        done = ", ".join(bits) or "nothing"
        if self.partial:
            return f"PARTIAL  {self.gateway_agent_id}: {done}; " \
                   f"FAILED: {'; '.join(self.failures)}"
        if self.failures:
            return f"FAILED   {self.gateway_agent_id}: " \
                   f"{'; '.join(self.failures)}"
        return f"ok       {self.gateway_agent_id}: {done}"


async def apply_to_agent(gateway_admin, agent: Refreshable,
                         desired: Desired) -> Outcome:
    """Write one agent's prompts, tools and skills. Never raises."""
    out = Outcome(gateway_agent_id=agent.gateway_agent_id)
    gw = agent.gateway_agent_id

    # ── prompts (inert until the restart) ────────────────────────────
    for name, content in sorted(desired.files.items()):
        if not is_writable(name):
            # Cannot happen through `render`, which filters first. Kept
            # because "cannot happen" is a property of today's renderer.
            out.refused.append(refuse_reason(name))
            logger.error("agent refresh: REFUSED %s for %s -- %s",
                         name, gw, refuse_reason(name))
            continue
        try:
            await gateway_admin.set_agent_file(gw, name, content)
            out.files.append(name)
        except Exception as exc:                            # noqa: BLE001
            out.failures.append(f"file {name}: {exc}")
            logger.warning("agent refresh: %s file %s failed: %s",
                           gw, name, exc)

    # ── tools (live on the next turn) ────────────────────────────────
    #
    # AN EMPTY ALLOW LIST IS NOT "NO TOOLS" TO THE GATEWAY -- it means
    # every tool is permitted. `platform_council` learned this and
    # guards the same way. So an empty render is treated as "nothing to
    # say about tools", never as an instruction to open the agent up.
    if desired.tools_allow:
        try:
            await gateway_admin.set_agent_tools(
                gw, desired.tools_allow, desired.tools_deny)
            out.tools_set = True
        except Exception as exc:                            # noqa: BLE001
            out.failures.append(f"tools: {exc}")
            logger.warning("agent refresh: %s tools failed: %s", gw, exc)
    elif desired.tools_deny:
        out.failures.append(
            "tools: a deny list with an empty allow list would read as "
            "'all tools permitted, minus these' -- refusing to send it")

    # ── skills (read on demand) ──────────────────────────────────────
    for name, content in sorted(desired.skills.items()):
        try:
            await gateway_admin.set_agent_skill(gw, name, content)
            out.skills.append(name)
        except Exception as exc:                            # noqa: BLE001
            out.failures.append(f"skill {name}: {exc}")
            logger.warning("agent refresh: %s skill %s failed: %s",
                           gw, name, exc)

    if out.partial:
        # Loud, because this is the one outcome that makes a restart
        # unsafe rather than merely incomplete.
        logger.error("agent refresh: %s is PARTIAL -- %s", gw,
                     "; ".join(out.failures))
    return out
