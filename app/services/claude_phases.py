"""Claude Code phase registry service.

Each row in `claude_phases` defines a subagent definition that gets
materialized into <story-workspace>/.claude/agents/<name>.md during
operator-mode workspace provisioning. The plan DAG references phases
by name; the task_controller dispatches each task with instructions
to invoke the corresponding subagent.

This module is the CRUD layer + the file-rendering helper.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


# Names match common filesystem and YAML conventions.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


async def list_phases(only_active: bool = False) -> list[dict]:
    """List all claude phases, ordered by name."""
    pool = get_pool()
    where = "WHERE status = 'active'" if only_active else ""
    rows = await pool.fetch(
        f"""SELECT name, description, system_prompt, allowed_tools,
                   denied_tools, model, status, created_by,
                   created_at, updated_at
              FROM claude_phases
              {where}
             ORDER BY name"""
    )
    return [dict(r) for r in rows]


async def get_phase(name: str) -> Optional[dict]:
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT name, description, system_prompt, allowed_tools,
                  denied_tools, model, status, created_by,
                  created_at, updated_at
             FROM claude_phases WHERE name = $1""",
        name,
    )
    return dict(row) if row else None


async def create_phase(
    name: str,
    description: str,
    system_prompt: str,
    allowed_tools: list[str],
    denied_tools: list[str],
    model: str = "",
    created_by: str = "system",
) -> None:
    """Create a new phase. Raises ValueError on bad input."""
    name = (name or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"phase name must match [a-z][a-z0-9_-]{{1,63}}: got {name!r}"
        )
    if not system_prompt or not system_prompt.strip():
        raise ValueError("system_prompt cannot be empty")

    pool = get_pool()
    existing = await pool.fetchval(
        "SELECT 1 FROM claude_phases WHERE name = $1", name,
    )
    if existing:
        raise ValueError(f"phase {name!r} already exists")

    await pool.execute(
        """INSERT INTO claude_phases (
               name, description, system_prompt,
               allowed_tools, denied_tools, model, created_by
           ) VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        name,
        (description or "").strip(),
        system_prompt,
        allowed_tools or [],
        denied_tools or [],
        (model or "").strip(),
        created_by,
    )


async def update_phase(
    name: str,
    description: Optional[str] = None,
    system_prompt: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    denied_tools: Optional[list[str]] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Patch an existing phase. Only non-None fields are updated."""
    pool = get_pool()

    sets: list[str] = []
    vals: list = []
    idx = 1

    if description is not None:
        sets.append(f"description = ${idx}")
        vals.append(description.strip())
        idx += 1
    if system_prompt is not None:
        if not system_prompt.strip():
            raise ValueError("system_prompt cannot be empty")
        sets.append(f"system_prompt = ${idx}")
        vals.append(system_prompt)
        idx += 1
    if allowed_tools is not None:
        sets.append(f"allowed_tools = ${idx}")
        vals.append(allowed_tools)
        idx += 1
    if denied_tools is not None:
        sets.append(f"denied_tools = ${idx}")
        vals.append(denied_tools)
        idx += 1
    if model is not None:
        sets.append(f"model = ${idx}")
        vals.append(model.strip())
        idx += 1
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValueError(f"status must be 'active' or 'disabled', got {status!r}")
        sets.append(f"status = ${idx}")
        vals.append(status)
        idx += 1

    if not sets:
        return  # nothing to update

    sets.append("updated_at = now()")
    vals.append(name)
    await pool.execute(
        f"UPDATE claude_phases SET {', '.join(sets)} WHERE name = ${idx}",
        *vals,
    )


async def delete_phase(name: str) -> None:
    """Soft-delete by setting status='disabled'. We never hard-delete because
    historical task rows may reference the phase name."""
    pool = get_pool()
    await pool.execute(
        "UPDATE claude_phases SET status = 'disabled', updated_at = now() "
        "WHERE name = $1",
        name,
    )


def render_phase_to_subagent_md(phase: dict) -> str:
    """Render a claude_phases row to the YAML-frontmatter markdown format
    Claude Code expects for `.claude/agents/<name>.md`.

    Schema reference: https://code.claude.com/docs/en/sub-agents
    """
    name = phase["name"]
    description = (phase.get("description") or "").replace("\n", " ").strip()
    system_prompt = (phase.get("system_prompt") or "").rstrip() + "\n"

    allowed = phase.get("allowed_tools") or []
    denied = phase.get("denied_tools") or []
    model = (phase.get("model") or "").strip()

    # YAML frontmatter. Tools list rendered as a YAML flow sequence so
    # multi-token entries like `Bash(pytest:*)` are inside one document
    # without needing per-line escaping.
    lines = ["---", f"name: {name}"]
    if description:
        # Escape quotes inside the description.
        safe = description.replace('"', '\\"')
        lines.append(f'description: "{safe}"')
    if allowed:
        lines.append("tools: " + ", ".join(allowed))
    if denied:
        # Custom field: Claude reads `tools` for allow; we keep `tools_denied`
        # as metadata for our own bookkeeping (the gateway-side restrict hook
        # honours the .claude/settings.json deny list separately).
        lines.append("tools_denied: " + ", ".join(denied))
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    lines.append(system_prompt)
    return "\n".join(lines)


async def render_all_active_phases() -> dict[str, str]:
    """Return {filename → file content} for every active phase, ready to
    drop into a story workspace's .claude/agents/ dir.

    Filename format: `<phase-name>.md` (e.g. `coder.md`).
    """
    phases = await list_phases(only_active=True)
    return {
        f"{p['name']}.md": render_phase_to_subagent_md(p)
        for p in phases
    }
