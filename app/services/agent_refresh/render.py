"""What an agent's prompts, tools and skills SHOULD be, right now.

MIRRORS PROVISIONING RATHER THAN IMPROVING ON IT. A refresh that renders
content a freshly-created agent would never receive is not a refresh --
it is an undocumented second configuration that only existing agents get.
So every rule below is copied from the creation path even where the
creation path is odd, and the oddities are named so nobody "fixes" one
here in isolation.

TWO PATHS, AND THEY GENUINELY DIFFER. This is the one part of the
refresh that cannot be written once:

  FILES      SHCA: `template_files` of the base agent's template,
             overlaid by that agent's own `agent_files` rows, which win
             on a filename clash (`app/openclaw.py:3155-3165`).
             GO:   `template_files` of `GO_TEMPLATE_ID`, no overlay
             (`app/services/go_agent.py:585`).

  TOOLS      SHCA: read from the `templates` TABLE, falling back to the
             base agent's own columns, with NO registry resolution
             (`app/openclaw.py:2966-2986`).
             GO:   read from an `agents` row and put through
             `resolve_tool_allowlist` with the template's profiles, so
             profile expansions and cancellations apply
             (`app/services/go_agent.py:452-476`).

             That divergence is REAL and predates this tool: an SHCA's
             allowlist is whatever the column literally says, while a
             GO's is expanded. Mirrored here rather than unified,
             because unifying it would change what tools existing
             assistants are granted -- a behaviour change wearing a
             refactor's clothes.

  SKILLS     both: active skills whose `applicable_to` contains the
             agent's type -- EXCEPT that `kernel-assistant` and `go-v2`
             templates get none at all, by design. Their whole surface is
             two prompt files, and a `skills/` directory they never read
             once routed the kernel assistant into announcing a phase for
             a state machine that does not exist here.

TWO MEANINGS OF `template_id`, which is the trap in this file:
  `agent_instances.template_id` -> `agents.id`      (the BASE AGENT)
  `agents.template_id`          -> `templates.id`   (the TEMPLATE)
Reading the first where the second is meant silently yields no files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.agent_refresh.eligibility import Refreshable
from app.services.agent_refresh.managed import MANAGED_FILES, is_writable

logger = logging.getLogger(__name__)

# Templates whose agents have zero skills by design -- not few, zero.
NO_SKILL_TEMPLATES = ("kernel-assistant", "go-v2")


@dataclass(frozen=True)
class Desired:
    """The content an agent should be holding."""

    files: dict[str, str] = field(default_factory=dict)
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    skills: dict[str, str] = field(default_factory=dict)
    # Why a render came back thin, for the plan to print. A template with
    # no AGENTS.md is a misconfiguration, and a refresh that silently
    # pushed nothing would look identical to one with nothing to do.
    notes: list[str] = field(default_factory=list)


async def _template_of(conn, agent: Refreshable) -> str | None:
    """The `templates.id` whose files and tools this agent should hold."""
    if agent.is_go:
        from app.services.go_agent import GO_TEMPLATE_ID
        return GO_TEMPLATE_ID
    # SHCA: instance -> base agent -> template.
    return await conn.fetchval(
        "SELECT template_id FROM agents WHERE id = $1", agent.template_id)


async def _files(conn, agent: Refreshable, template_id: str | None,
                 notes: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    if template_id:
        for r in await conn.fetch(
                "SELECT filename, content FROM template_files "
                " WHERE template_id = $1", template_id):
            merged[r["filename"]] = r["content"]
    if not agent.is_go:
        # The per-agent overlay WINS, exactly as provisioning has it.
        for r in await conn.fetch(
                "SELECT filename, content FROM agent_files WHERE agent_id = $1",
                agent.template_id):
            merged[r["filename"]] = r["content"]

    out = {n: c for n, c in merged.items() if is_writable(n)}
    for name in MANAGED_FILES:
        if name not in out:
            notes.append(
                f"no {name} to push (template {template_id or '<none>'} "
                f"has none)")
    return out


async def _tools(conn, agent: Refreshable, template_id: str | None,
                 notes: list[str]) -> tuple[list[str], list[str]]:
    if agent.is_go:
        # FROM `templates`, NOT `agents`, and `status = 'active'` --
        # `go_agent.py:417` reads exactly this row. An earlier draft of
        # this function queried `agents`, where `go-v2` does not exist at
        # all: the lookup returned nothing, the render reported empty
        # tools, and because an empty allow list is never pushed, every
        # GO would have been silently skipped for tools while the plan
        # said the refresh had run.
        row = await conn.fetchrow(
            "SELECT id, tools_allow, tools_deny FROM templates "
            " WHERE id = $1 AND status = 'active'", template_id)
        if row is None:
            notes.append(f"GO template {template_id!r} is missing from "
                         f"`templates`, or is not active")
            return [], []
        raw_allow = list(row["tools_allow"] or [])
        raw_deny = list(row["tools_deny"] or [])
        if not raw_allow:
            notes.append(f"GO template {template_id!r} has an empty "
                         f"tools_allow; refusing to propose a tool change")
            return [], []
        try:
            from app.services.tool_registry import (
                get_template_profiles, resolve_tool_allowlist)
            profiles = await get_template_profiles(conn, row["id"])
            return await resolve_tool_allowlist(
                conn, profiles, raw_allow, raw_deny)
        except Exception as exc:                            # noqa: BLE001
            # Provisioning falls back to the raw columns here and says so;
            # so does this, because a refresh that proposed the raw list
            # while creation used the resolved one would show a diff that
            # is an artefact of the resolver being down.
            notes.append(f"tool resolver unavailable ({exc}); using the raw "
                         f"columns, same as provisioning does")
            return raw_allow, raw_deny

    allow: list[str] = []
    deny: list[str] = []
    row = await conn.fetchrow(
        "SELECT tools_allow, tools_deny FROM templates WHERE id = $1",
        template_id) if template_id else None
    if row and row["tools_allow"]:
        allow = list(row["tools_allow"])
        deny = list(row["tools_deny"] or [])
    if not allow:
        base = await conn.fetchrow(
            "SELECT tools_allow, tools_deny FROM agents WHERE id = $1",
            agent.template_id)
        if base:
            allow = list(base["tools_allow"] or [])
            deny = list(base["tools_deny"] or [])
    return allow, deny


async def _skills(conn, agent: Refreshable,
                  template_id: str | None) -> dict[str, str]:
    if template_id in NO_SKILL_TEMPLATES:
        return {}
    rows = await conn.fetch(
        "SELECT name, content FROM skills "
        " WHERE $1 = ANY(applicable_to) AND status = 'active' ORDER BY name",
        agent.agent_type)
    return {r["name"]: r["content"] for r in rows}


async def desired_for(conn, agent: Refreshable) -> Desired:
    """Render what this agent should be holding, right now."""
    notes: list[str] = []
    template_id = await _template_of(conn, agent)
    if template_id is None:
        notes.append(
            f"{agent.gateway_agent_id} resolves to no template, so there is "
            f"nothing to render; it was probably created from agent_files "
            f"alone")
    files = await _files(conn, agent, template_id, notes)
    allow, deny = await _tools(conn, agent, template_id, notes)
    skills = await _skills(conn, agent, template_id)
    return Desired(files=files, tools_allow=allow, tools_deny=deny,
                   skills=skills, notes=notes)
