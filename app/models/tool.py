"""Pydantic models for the Tool Registry.

Tools are the atomic units passed to OpenClaw's tools.allow list.
Profiles are named, reusable bundles of tools assigned to agent templates.
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class Tool(BaseModel):
    name: str
    label: str = ""
    description: str = ""
    group_name: str = ""
    tags: list[str] = []
    source: str = "core"
    status: Literal["active", "deprecated"] = "active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ToolCreateRequest(BaseModel):
    name: str
    label: str = ""
    description: str = ""
    group_name: str = ""
    tags: list[str] = []
    source: str = "core"


class ToolUpdateRequest(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    group_name: Optional[str] = None
    tags: Optional[list[str]] = None
    source: Optional[str] = None


class ToolProfile(BaseModel):
    id: str
    name: str
    description: str = ""
    status: Literal["active", "deprecated"] = "active"
    tool_names: list[str] = []  # populated by JOIN query in service layer
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ToolProfileCreateRequest(BaseModel):
    id: str  # slug, e.g. "story-orchestrator"
    name: str
    description: str = ""
    tool_names: list[str] = []


class ToolProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tool_names: Optional[list[str]] = None  # None = no change; [] = clear all tools
