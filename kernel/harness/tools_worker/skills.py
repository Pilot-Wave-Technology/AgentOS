"""`skill` — load one skill's body.

Worker-side because the PROFILE lives here, and the profile is what
says which skills exist. A sandbox that could resolve a skill name
would need the profile, and the profile is the tool surface: handing it
across the bridge hands the model the thing that bounds it.
"""
from __future__ import annotations

from kernel.harness.models.types import ToolResult
from kernel.profiles.skills import discover, load_body

def skill(profile, tool_args: dict, tool_call_id: str) -> ToolResult:
    """Load one skill's body. Worker-side: the profile lives here.

    Resolved by the discovered skill NAME, never by joining the model's
    string onto a path — the argument arrives from the model, and a
    path join would make "../../etc/passwd" a skill.
    """
    name = (tool_args.get("name") or "").strip()
    if not name:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="skill: name is required", exit_code=1)
    if profile is None:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="skill: no profile is bound to this execution",
            exit_code=1)

    body = load_body(profile.skill_dirs, name)
    if body is None:
        available = ", ".join(s.name for s in discover(profile.skill_dirs))
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"skill: no skill named {name!r}. "
                  f"Available: {available or 'none'}",
            exit_code=1)
    return ToolResult(tool_call_id=tool_call_id, output=body,
                      error=None, exit_code=0)
