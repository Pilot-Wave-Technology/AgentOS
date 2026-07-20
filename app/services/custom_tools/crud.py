"""CRUD operations for custom tools and tool_credentials using the unified tools table.

Custom tools are rows in `tools` where `tier IS NOT NULL`. The `source` column
is always 'custom' for admin-defined tools. No separate mirror table is needed.
"""
import json
import logging
from typing import Any, Optional

from app.services.custom_tools.credential import encrypt_value, evict_cache

log = logging.getLogger(__name__)

# Source-code size cap, enforced uniformly here so both wizard generators
# and the typescript-paste path go through the same check. Sized to allow
# generated OpenAPI tools that embed per-operation schemas for 100+ ops
# (each ~1 KB), while still bounding plugin-cache memory and V8 parse cost.
_MAX_SOURCE_CODE_BYTES = 200_000  # 200 KB


def _decode_tool_row(row) -> dict:
    """Convert asyncpg Record to dict, deserialising JSONB columns and normalising UUID types.

    Stringifies BOTH `id` and `credential_id` so the result is fully
    JSON-serialisable — Starlette's JSONResponse can't handle uuid.UUID
    natively. The admin UI serialisers (_tool_summary / _tool_detail) drop
    `id`, but any future caller passing the row through directly is safe.
    """
    d = dict(row)
    for field in ("config", "parameters_schema", "manifest"):
        if isinstance(d.get(field), str):
            d[field] = json.loads(d[field])
    for uuid_field in ("id", "credential_id"):
        if d.get(uuid_field) is not None:
            d[uuid_field] = str(d[uuid_field])
    return d


# ── Custom Tools ──────────────────────────────────────────────────────────────

async def create_custom_tool(
    pool,
    name: str,
    label: str,
    description: str,
    tier: str,
    parameters_schema: dict,
    config: dict,
    source_code: Optional[str],
    created_by: Optional[str],
    credential_id: Optional[str] = None,
    manifest: Optional[dict] = None,
    is_core: bool = False,
) -> dict:
    """Create a custom-tier tool. `is_core=True` marks it as a platform-seeded
    tool (shipped with platform setup, e.g. Platform Council bootstrap) so it
    shows under Tool Registry's "System Tools" tab instead of "Custom Tools".
    Defaults to False for the custom-tool wizard and apply_add_tool (agent-
    created at runtime), which stay "Custom".
    """
    existing = await pool.fetchval("SELECT 1 FROM tools WHERE name = $1", name)
    if existing:
        raise ValueError(f"custom tool '{name}' already exists")

    # Enforce the source_code size cap here so wizard generators that bypass
    # the Pydantic CustomToolCreateRequest validator (api_openapi.generate,
    # api_mcp.generate calling create_custom_tool directly) can't sneak past it.
    if source_code is not None and len(source_code.encode()) > _MAX_SOURCE_CODE_BYTES:
        raise ValueError(
            f"source_code exceeds {_MAX_SOURCE_CODE_BYTES} bytes "
            f"({len(source_code.encode())} bytes)"
        )

    # Hoist credential_id from config if caller embedded it there; strip from config to avoid duplication
    if not credential_id and config.get("credential_id"):
        credential_id = config["credential_id"]
    clean_config = {k: v for k, v in config.items() if k != "credential_id"}

    row = await pool.fetchrow(
        """INSERT INTO tools
             (name, label, description, source, tier, parameters_schema, config,
              source_code, manifest, credential_id, created_by, is_core)
           VALUES ($1, $2, $3, 'custom', $4, $5, $6, $7, $8, $9::uuid, $10, $11)
           RETURNING *""",
        name, label, description, tier,
        json.dumps(parameters_schema), json.dumps(clean_config),
        source_code,
        json.dumps(manifest) if manifest is not None else None,
        credential_id or None, created_by, is_core,
    )
    log.info(
        "custom tool created: name=%s tier=%s manifest=%s by=%s",
        name, tier, bool(manifest), created_by,
    )
    return _decode_tool_row(row)


async def get_custom_tool(pool, name: str) -> Optional[dict]:
    """Look up a custom tool by name. Returns None if not found or if it is not a custom-tier tool."""
    row = await pool.fetchrow(
        "SELECT * FROM tools WHERE name = $1 AND tier IS NOT NULL", name
    )
    return _decode_tool_row(row) if row else None


async def list_custom_tools(pool, status: Optional[str] = None) -> list[dict]:
    if status:
        rows = await pool.fetch(
            "SELECT * FROM tools WHERE tier IS NOT NULL AND status = $1 ORDER BY created_at DESC",
            status,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM tools WHERE tier IS NOT NULL ORDER BY created_at DESC"
        )
    return [_decode_tool_row(r) for r in rows]


async def count_custom_tools(pool, q: str = "", tier: str = "") -> int:
    """Count custom tools matching a search term and/or exact tier, for the
    paginated "Custom Tools" tab in the `/settings/tools` UI.

    Excludes is_core tools (platform-seeded, shown under "System Tools"
    instead). Kept separate from `list_custom_tools` (used by the OpenClaw
    plugin sync and internal/admin APIs, which must keep resolving every
    custom-tier tool regardless of is_core) so those callers keep getting the
    full, unpaginated set regardless of UI search/pagination/tier-filter state.
    """
    conditions = ["tier IS NOT NULL", "is_core = false"]
    params: list[Any] = []
    if q:
        params.append(f"%{q}%")
        conditions.append(f"(name ILIKE ${len(params)} OR label ILIKE ${len(params)} OR tier ILIKE ${len(params)} OR status ILIKE ${len(params)})")
    if tier:
        params.append(tier)
        conditions.append(f"tier = ${len(params)}")
    count = await pool.fetchval(f"SELECT COUNT(*) FROM tools WHERE {' AND '.join(conditions)}", *params)
    log.debug("count_custom_tools: q=%r tier=%r -> %d", q, tier, count)
    return count


async def list_custom_tools_page(
    pool, q: str = "", limit: int = 10, offset: int = 0, tier: str = "",
) -> list[dict]:
    """Paginated, searchable, tier-filterable custom tools list for the
    "Custom Tools" tab in the `/settings/tools` UI.

    See `count_custom_tools` docstring for why this excludes is_core tools
    and is separate from `list_custom_tools`.
    """
    conditions = ["tier IS NOT NULL", "is_core = false"]
    params: list[Any] = []
    if q:
        params.append(f"%{q}%")
        conditions.append(f"(name ILIKE ${len(params)} OR label ILIKE ${len(params)} OR tier ILIKE ${len(params)} OR status ILIKE ${len(params)})")
    if tier:
        params.append(tier)
        conditions.append(f"tier = ${len(params)}")
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""SELECT * FROM tools WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
        *params,
    )
    log.debug(
        "list_custom_tools_page: q=%r tier=%r limit=%d offset=%d -> %d rows",
        q, tier, limit, offset, len(rows),
    )
    return [_decode_tool_row(r) for r in rows]


async def update_custom_tool(pool, tool_name: str, updates: dict[str, Any]) -> dict:
    allowed = {"label", "description", "parameters_schema", "config", "source_code", "manifest", "credential_id"}
    # credential_id=None is a valid explicit clear (set FK to NULL); all other None values are skipped.
    fields = {k: v for k, v in updates.items() if k in allowed and (v is not None or k == "credential_id")}

    # Same size cap as create_custom_tool — applies whether the update came
    # from the regenerate endpoint, the typescript edit form, or a direct PATCH.
    if isinstance(fields.get("source_code"), str):
        sz = len(fields["source_code"].encode())
        if sz > _MAX_SOURCE_CODE_BYTES:
            raise ValueError(
                f"source_code exceeds {_MAX_SOURCE_CODE_BYTES} bytes ({sz} bytes)"
            )

    # Hoist credential_id out of config if caller embedded it; strip from config to avoid duplication
    if "config" in fields and isinstance(fields["config"], dict):
        if not fields.get("credential_id") and fields["config"].get("credential_id"):
            fields["credential_id"] = fields["config"]["credential_id"]
        fields["config"] = {k: v for k, v in fields["config"].items() if k != "credential_id"}

    # JSONB fields must be serialised to string for asyncpg
    for jsonb_col in ("parameters_schema", "config", "manifest"):
        if jsonb_col in fields and isinstance(fields[jsonb_col], dict):
            fields[jsonb_col] = json.dumps(fields[jsonb_col])

    if not fields:
        row = await pool.fetchrow(
            "SELECT * FROM tools WHERE name = $1 AND tier IS NOT NULL", tool_name
        )
        if not row:
            raise ValueError(f"custom tool {tool_name!r} not found")
        return _decode_tool_row(row)

    set_clauses = []
    values = [tool_name]
    for i, (field, value) in enumerate(fields.items(), start=2):
        if field == "credential_id":
            set_clauses.append(f"credential_id = ${i}::uuid")
        else:
            set_clauses.append(f"{field} = ${i}")
        values.append(value)
    set_clauses.append("updated_at = NOW()")

    sql = (
        f"UPDATE tools SET {', '.join(set_clauses)}"
        " WHERE name = $1 AND tier IS NOT NULL RETURNING *"
    )
    row = await pool.fetchrow(sql, *values)
    if not row:
        raise ValueError(f"custom tool {tool_name!r} not found")

    log.info("custom tool updated: name=%s fields=%s", tool_name, list(fields.keys()))
    return _decode_tool_row(row)


async def set_custom_tool_status(pool, tool_name: str, status: str) -> dict:
    row = await pool.fetchrow(
        "UPDATE tools SET status=$2, updated_at=NOW() WHERE name=$1 AND tier IS NOT NULL RETURNING *",
        tool_name, status,
    )
    if not row:
        raise ValueError(f"custom tool {tool_name!r} not found")

    log.info("custom tool status: name=%s status=%s", tool_name, status)
    return _decode_tool_row(row)


async def bulk_set_credential(
    pool,
    tool_names: list[str],
    credential_id: Optional[str],
) -> list[str]:
    """Set (or clear) credential_id on multiple tools at once. Returns list of updated names."""
    rows = await pool.fetch(
        """UPDATE tools
           SET credential_id = $1::uuid, updated_at = NOW()
           WHERE name = ANY($2) AND tier IS NOT NULL
           RETURNING name""",
        credential_id or None,
        tool_names,
    )
    updated = [r["name"] for r in rows]
    log.info("bulk credential set: credential_id=%s tools=%s", credential_id, updated)
    return updated


# ── Tool Credentials ──────────────────────────────────────────────────────────

def _decode_credential_row(row) -> dict:
    """Convert asyncpg Record to a dict shared by both HTML templates and JSON APIs.

    The credential id column is a PG uuid which asyncpg returns as a
    `uuid.UUID` object — not directly JSON-serialisable. Convert to string
    here so every consumer (admin UI, runtime secrets capability, list
    endpoints) gets the same plain-string shape.

    Timestamp fields (`created_at`, `rotated_at`, `updated_at`) are left as
    native `datetime` objects — this dict is rendered directly by Jinja2
    templates (which call `.strftime()` on them). JSON API routes convert
    them to ISO strings themselves via `response_model=ToolCredential`
    (see `app/routers/custom_tools/api_credentials.py`).
    """
    d = dict(row)
    if d.get("id") is not None:
        d["id"] = str(d["id"])
    return d


async def create_tool_credential(
    pool,
    name: str,
    credential_type: str,
    secret_value: str,
    header_name: Optional[str],
    created_by: Optional[str],
) -> dict:
    existing = await pool.fetchval("SELECT 1 FROM tool_credentials WHERE name = $1", name)
    if existing:
        raise ValueError(f"credential '{name}' already exists")

    encrypted = encrypt_value(secret_value)
    row = await pool.fetchrow(
        """INSERT INTO tool_credentials
             (name, credential_type, encrypted_value, header_name, created_by)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, name, credential_type, header_name, status, rotated_at, created_by, created_at, updated_at""",
        name, credential_type, encrypted, header_name, created_by,
    )
    log.info("tool credential created: name=%s type=%s", name, credential_type)
    return _decode_credential_row(row)


async def get_tool_credential(pool, credential_id: str) -> Optional[dict]:
    """Returns the credential row INCLUDING encrypted_value (for decryption in executor)."""
    row = await pool.fetchrow("SELECT * FROM tool_credentials WHERE id = $1::uuid", credential_id)
    return _decode_credential_row(row) if row else None


async def list_tool_credentials(pool) -> list[dict]:
    """Returns all credentials WITHOUT encrypted_value. `status` distinguishes
    'pending' (scaffolded, secret not yet entered) from 'active'."""
    rows = await pool.fetch(
        """SELECT id, name, credential_type, header_name, status, rotated_at,
                  created_by, created_at, updated_at
           FROM tool_credentials ORDER BY created_at DESC"""
    )
    return [_decode_credential_row(r) for r in rows]


async def scaffold_tool_credential(
    pool, name: str, credential_type: str, header_name: Optional[str], created_by: Optional[str],
) -> dict:
    """Create a credential SHELL with NO secret (status='pending'). An agent uses
    this to declare that a tool needs a credential; a human fills the secret later
    via rotate_tool_credential (Settings → Credentials), which flips it to 'active'.
    The executor refuses a 'pending' credential, so the tool fails safe until filled."""
    existing = await pool.fetchval("SELECT 1 FROM tool_credentials WHERE name = $1", name)
    if existing:
        raise ValueError(f"credential '{name}' already exists")
    row = await pool.fetchrow(
        """INSERT INTO tool_credentials
             (name, credential_type, encrypted_value, header_name, status, created_by)
           VALUES ($1, $2, '', $3, 'pending', $4)
           RETURNING id, name, credential_type, header_name, status, rotated_at, created_by, created_at, updated_at""",
        name, credential_type, header_name, created_by,
    )
    log.info("credential scaffolded (pending): name=%s type=%s by=%s", name, credential_type, created_by)
    return _decode_credential_row(row)


async def rotate_tool_credential(pool, credential_id: str, new_secret_value: str) -> dict:
    encrypted = encrypt_value(new_secret_value)
    # Setting a secret also ACTIVATES a pending (scaffolded) credential.
    row = await pool.fetchrow(
        """UPDATE tool_credentials
           SET encrypted_value=$2, status='active', rotated_at=NOW(), updated_at=NOW()
           WHERE id=$1::uuid
           RETURNING id, name, credential_type, header_name, status, rotated_at, created_by, created_at, updated_at""",
        credential_id, encrypted,
    )
    if not row:
        raise ValueError(f"credential {credential_id!r} not found")

    evict_cache(credential_id)
    log.info("tool credential rotated: id=%s", credential_id)
    return _decode_credential_row(row)


async def delete_tool_credential(pool, credential_id: str) -> None:
    """Delete a credential. Raises ValueError if any enabled custom tool references it."""
    in_use = await pool.fetchval(
        """SELECT name FROM tools
           WHERE credential_id = $1::uuid AND status = 'enabled' AND tier IS NOT NULL
           LIMIT 1""",
        credential_id,
    )
    if in_use:
        raise ValueError(
            f"credential is used by enabled tool '{in_use}' — disable the tool first"
        )

    deleted = await pool.fetchval(
        "DELETE FROM tool_credentials WHERE id=$1::uuid RETURNING id", credential_id
    )
    if not deleted:
        raise ValueError(f"credential {credential_id!r} not found")

    evict_cache(credential_id)
    log.info("tool credential deleted: id=%s", credential_id)


async def count_tools_using_credential(pool, credential_id: str) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM tools WHERE credential_id = $1::uuid AND tier IS NOT NULL",
        credential_id,
    ) or 0


# ── OpenAPI Specs ─────────────────────────────────────────────────────────────

def _decode_openapi_spec_row(row) -> dict:
    """Stringify UUID columns so the row is JSON-serialisable."""
    d = dict(row)
    for uuid_field in ("id", "credential_id"):
        if d.get(uuid_field) is not None:
            d[uuid_field] = str(d[uuid_field])
    return d


async def create_openapi_spec(
    pool,
    name: str,
    spec_content: dict,
    base_url: Optional[str],
    credential_id: Optional[str],
    spec_url: Optional[str],
) -> dict:
    row = await pool.fetchrow(
        """INSERT INTO openapi_specs (name, spec_url, spec_content, base_url, credential_id)
           VALUES ($1, $2, $3, $4, $5::uuid)
           RETURNING *""",
        name, spec_url, json.dumps(spec_content), base_url or None, credential_id or None,
    )
    log.info("openapi spec stored: name=%s", name)
    return _decode_openapi_spec_row(row)
