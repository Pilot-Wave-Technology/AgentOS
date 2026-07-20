"""Pydantic models for custom tools — admin-defined tools in the unified tools table."""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator, model_validator


TierType = Literal["http_action", "openapi", "typescript", "mcp_server", "mcp_tool"]
CredentialType = Literal["bearer", "api_key", "basic"]
StatusType = Literal["enabled", "disabled", "deprecated"]
CredentialStatusType = Literal["pending", "active"]


# ── Custom Tool ───────────────────────────────────────────────────────────────

class CustomTool(BaseModel):
    name: str
    label: str
    description: str
    tier: TierType
    parameters_schema: dict[str, Any] = {"type": "object", "properties": {}}
    config: dict[str, Any] = {}
    source_code: Optional[str] = None
    # Declared capabilities + limits for the OpenClaw plugin's capability
    # runtime. Tools with both `source_code` and `manifest` execute via the
    # runtime locally on the VM; tools without manifest fall back to the
    # legacy AgentOS executor (deprecated, removed in phase 4).
    manifest: Optional[dict[str, Any]] = None
    credential_id: Optional[str] = None
    status: StatusType = "enabled"
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CustomToolCreateRequest(BaseModel):
    name: str
    label: str
    description: str
    tier: TierType
    parameters_schema: dict[str, Any] = {"type": "object", "properties": {}}
    config: dict[str, Any] = {}
    source_code: Optional[str] = None
    manifest: Optional[dict[str, Any]] = None
    credential_id: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", v):
            raise ValueError("name must match ^[a-z][a-z0-9_]{0,63}$")
        return v

    @field_validator("source_code")
    @classmethod
    def validate_source_code_size(cls, v: Optional[str]) -> Optional[str]:
        # Generators that aggregate many operations (OpenAPI / MCP) can emit
        # tool source well above the original 50 KB ceiling. The single source
        # of truth for this limit lives in crud._MAX_SOURCE_CODE_BYTES (200 KB);
        # keep this Pydantic check at the same bound so request validation
        # rejects oversize bodies before they hit the DB layer.
        if v and len(v.encode()) > 200_000:
            raise ValueError("source_code exceeds 200 KB limit")
        return v


class CustomToolUpdateRequest(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    parameters_schema: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None
    source_code: Optional[str] = None
    manifest: Optional[dict[str, Any]] = None
    credential_id: Optional[str] = None

    @field_validator("source_code")
    @classmethod
    def validate_source_code_size(cls, v: Optional[str]) -> Optional[str]:
        # Generators that aggregate many operations (OpenAPI / MCP) can emit
        # tool source well above the original 50 KB ceiling. The single source
        # of truth for this limit lives in crud._MAX_SOURCE_CODE_BYTES (200 KB);
        # keep this Pydantic check at the same bound so request validation
        # rejects oversize bodies before they hit the DB layer.
        if v and len(v.encode()) > 200_000:
            raise ValueError("source_code exceeds 200 KB limit")
        return v


class CustomToolTestRequest(BaseModel):
    args: dict[str, Any] = {}


class BulkCredentialRequest(BaseModel):
    tool_names: list[str]
    credential_id: Optional[str] = None  # None = clear credential on all selected tools


# ── Tool Credential ───────────────────────────────────────────────────────────

class ToolCredential(BaseModel):
    id: str
    name: str
    credential_type: CredentialType
    header_name: Optional[str] = None  # api_key only
    # 'pending' = scaffolded by an agent, secret not yet entered by a human;
    # 'active' = usable. The executor refuses 'pending' credentials.
    status: CredentialStatusType = "active"
    rotated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # encrypted_value intentionally omitted — never returned to callers


class ToolCredentialCreateRequest(BaseModel):
    name: str
    credential_type: CredentialType
    secret_value: str           # plaintext; encrypted before storage; never persisted raw
    header_name: Optional[str] = None

    @model_validator(mode="after")
    def header_name_required_for_api_key(self) -> "ToolCredentialCreateRequest":
        if self.credential_type == "api_key" and not self.header_name:
            raise ValueError("header_name is required for api_key credentials (e.g. 'X-API-Key')")
        return self


class ToolCredentialRotateRequest(BaseModel):
    secret_value: str           # new plaintext value


# ── OpenAPI Spec ──────────────────────────────────────────────────────────────

class OpenAPISpec(BaseModel):
    id: str
    name: str
    spec_url: Optional[str] = None
    base_url: Optional[str] = None
    credential_id: Optional[str] = None
    imported_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # spec_content omitted — can be large


class OpenAPISpecImportRequest(BaseModel):
    spec_url: Optional[str] = None
    spec_content: Optional[str] = None  # raw JSON/YAML; mutually exclusive with spec_url
    base_url: Optional[str] = None
    credential_id: Optional[str] = None


class OpenAPIOperationSelection(BaseModel):
    operation_id: str
    method: str
    path: str
    description: str = ""
    # Per-operation argument schema (JSON Schema). Optional — when present,
    # the capability runtime validates arguments against it before dispatch
    # so the agent gets a clear error instead of an upstream 400.
    parameters_schema: Optional[dict[str, Any]] = None


class OpenAPIToolsGenerateRequest(BaseModel):
    spec_id: str
    tool_name: str
    tool_label: Optional[str] = None
    tool_description: Optional[str] = None
    operations: list[OpenAPIOperationSelection]

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", v):
            raise ValueError("tool_name must match ^[a-z][a-z0-9_]{0,63}$")
        return v
