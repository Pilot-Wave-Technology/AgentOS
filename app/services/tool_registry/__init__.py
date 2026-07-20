"""Tool Registry service package.

Three sub-modules with single responsibilities:
  tool_crud    — CRUD for the tools catalog
  profile_crud — CRUD for profiles and template-profile assignments
  resolver     — expands profiles + direct tools to a flat allowlist

Import from this package, not from sub-modules directly.
"""
from .tool_crud import (
    create_tool,
    get_tool,
    list_tools,
    transition_tool,
    update_tool,
)
from .profile_crud import (
    create_profile,
    deprecate_profile,
    get_profile,
    get_profile_templates,
    get_template_profiles,
    list_profiles,
    set_profile_templates,
    set_template_profiles,
    update_profile,
)
from .resolver import resolve_tool_allowlist

__all__ = [
    "create_tool",
    "get_tool",
    "list_tools",
    "transition_tool",
    "update_tool",
    "create_profile",
    "deprecate_profile",
    "get_profile",
    "get_profile_templates",
    "get_template_profiles",
    "list_profiles",
    "set_profile_templates",
    "set_template_profiles",
    "update_profile",
    "resolve_tool_allowlist",
]
