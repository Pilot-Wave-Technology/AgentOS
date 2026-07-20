from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel


ResourceKind = Literal[
    "repo", "branch", "file", "document", "service",
    "environment", "dataset", "api_key", "design", "config", "other",
]


class ResourceCreate(BaseModel):
    kind: ResourceKind
    name: str
    uri: str = ""
    parent_id: Optional[str] = None
    human_context: str = ""
    metadata: dict = {}


class Resource(ResourceCreate):
    id: str
    goal_id: str
    source_task_id: str = ""
    created_by: str
    locked_by: Optional[str] = None
    locked_for: Optional[str] = None
    locked_at: Optional[datetime] = None
    status: Literal["active", "consumed", "archived", "deleted"] = "active"
    manifest: dict = {}
    last_scanned_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ResourceLockRequest(BaseModel):
    for_task: str   # task_id


class TaskResourceDep(BaseModel):
    task_id: str
    resource_id: str
    dep_type: Literal["requires", "produces", "locks"] = "requires"
    notes: str = ""
