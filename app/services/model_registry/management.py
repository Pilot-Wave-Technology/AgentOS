"""CRUD + validation for `models` / `model_providers` ("family B").

REMOVED 2026-08-06: the v1 CRUD surface (`create_model`, `update_model`,
`set_model_status`, `create_provider`, `update_provider`,
`set_provider_status`, plus their read helpers and private validators)
that used to write to `model_registry` / `model_provider_registry`
("family A", Phase A1, story `810717c2`) — confirmed via a repo-wide
grep to have zero live callers (only its own dedicated test file, which
was deleted alongside it). Family A's tables themselves are addressed
separately (schema-level removal, not this module's concern) — see
`documentation/MODEL_REGISTRY_CONTRACT.md` §0 for that history. Nothing
below touches family A anymore; this module is now entirely the `*_v2`
surface for family B (`models`/`model_providers`).

The module is deliberately transport-agnostic: it exposes plain async
functions returning plain `dict` rows, and raises typed
`ManagementError` subclasses carrying an `error_code` + HTTP `status`
hint. The router maps `error_code` / `status` to the wire format.

Nothing in this module imports from `app.llm_models` — the whole point
of the registry migration was to decouple the DB from the Python
whitelist, and the small `NATIVE_PROVIDERS` frozenset below is
deliberately duplicated rather than imported to keep that boundary
clean.
"""
import json
import logging
import re
from typing import Any

import asyncpg


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — every allowlist / regex / cap the validators consult.
# Kept as module-level frozensets so tests can assert on them directly and
# so a future refactor cannot accidentally widen the surface via ad-hoc
# string literals in the write functions.
# ---------------------------------------------------------------------------

# Provider prefixes registered natively by the OpenClaw fork. A model row
# whose provider portion is in this set does NOT need a matching row in
# `model_provider_registry` — the gateway resolves natives itself. Any
# other prefix is treated as a synthetic-context-window variant and
# requires a provider registration.
#
# This frozenset is the **repo-wide source of truth** for native-provider
# names, and every agentos consumer (tests, validators, docs) must
# import this constant rather than duplicating the literal — divergent
# local copies were the exact drift this constant was made public to end.
#
# `openai` and `codex-cli` are deliberately NOT in this set for now —
# deferred, not yet wanted as registered providers (see
# `migrations/models_hierarchy_backfill.sql`'s header). Add them back
# here (and seed a `model_providers` row) when that changes; nothing
# else needs updating since every consumer reads this constant.
NATIVE_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "google", "claude-cli"}
)

# Tier values as seen on `app/llm_models.py::ModelEntry` today. Kept
# duplicated here so this module doesn't import from `llm_models`.
_TIERS: frozenset[str] = frozenset(
    {"fast", "balanced", "premium", "test", "agentic"}
)

# Status values must match the CHECK constraint on both tables (see
# models_table.sql and model_providers_table.sql). Shared between the
# v2 base-model and provider validators below.
_STATUSES: frozenset[str] = frozenset({"active", "disabled"})

# Accepted `model_providers.api_type` values. Must stay in step with
# `emitter._GATEWAY_ACCEPTED_API_TYPES` (which mirrors the gateway's
# `ModelApiSchema` enum) plus `cli`, an AgentOS-only marker for CLI-backed
# providers that are never emitted to the gateway. See `_validate_api_type`
# for why an unvalidated value here used to break provider sync globally.
_VALID_API_TYPES: frozenset[str] = frozenset({
    "openai-completions",
    "openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "google-generative-ai",
    "github-copilot",
    "bedrock-converse-stream",
    "ollama",
    "cli",
})

# `id` shape: `<provider>/<model>`. Only the provider half is
# character-constrained (mixed-case alnum + dot/underscore/hyphen,
# matching `_MODEL_PROVIDER_ID_RE`) — that's the half every downstream
# consumer actually depends on being unambiguous, since it's what gets
# split off first. The model half is free text: any non-empty run of
# non-whitespace, non-control characters, INCLUDING further slashes.
#
# Why the model half doesn't need a single-slash limit or a character
# allowlist: every consumer that parses this id splits on the FIRST "/"
# only and keeps the remainder intact — `synthesizer.derive_base_id`
# (`variant_id.split("/", 1)`), this module's own
# `create_base_model_v2` (`model_id.split("/", 1)[1]`), and the OpenClaw
# fork's `parseModelRef`/`splitModelRef` (`indexOf("/")` + `slice`,
# fixed in the fork to match this exact behavior — see
# subagent-spawn.ts). None of them require exactly one slash; they only
# require the FIRST slash to correctly separate provider from model,
# which only depends on the provider half never containing one. That
# lets upstream providers with natively multi-segment model ids (e.g.
# Vertex's `publishers/google/models/gemini-pro`) be registered as-is,
# joined server-side from a separate `provider_id` + model-name input
# rather than hand-typed as one combined string.
#
# Whitespace/control characters are still excluded on the model half —
# not because anything downstream is known to break on them today, but
# because there's no real upstream model id that needs them, and
# keeping them out avoids relying on Jinja's auto-escaping as the only
# thing standing between a stray character and a broken admin page.
_MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*/[^\s\x00-\x1f\x7f]+$"
)

# Bounds — chosen well above any real-world value so accidental typos
# (negative, zero, or wildly oversized) fail loudly.
_MAX_CONTEXT_WINDOW = 10_000_000
_MAX_LABEL_LEN = 200
_MAX_PROVIDER_NAME_LEN = 100


# ---------------------------------------------------------------------------
# Domain exceptions.
# ---------------------------------------------------------------------------


class ManagementError(Exception):
    """Base class for every validation failure emitted by this module.

    Attributes:
        error_code: stable machine-readable identifier (e.g. `invalid_id`).
                    Callers map this to their wire format.
        status:     HTTP status hint (400 / 404 / 409). The router adopts
                    the hint directly so failure mapping stays in one
                    place — here.
    """

    def __init__(self, error_code: str, message: str, status: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.status = status


class ModelValidationError(ManagementError):
    """Raised when a `models` write violates a rule."""


class ProviderValidationError(ManagementError):
    """Raised when a `model_providers` write violates a rule."""


# ---------------------------------------------------------------------------
# Private validators. Each raises the appropriate typed error; each is
# unit-testable in isolation.
# ---------------------------------------------------------------------------


def _require_str(payload: dict, key: str, error_code: str) -> str:
    """Pull a required string field out of `payload` or raise.

    `error_code` selects the domain code. Missing / non-string / empty
    all map to the same code — the specific per-field validator below
    then adds narrower checks (regex, length, etc.).
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        # `name` errors are provider-domain; every other key is model-domain
        # at the callsites in this module today.
        if key == "name":
            raise ProviderValidationError(
                error_code, f"{key!r} is required and must be a non-empty string",
            )
        raise ModelValidationError(
            error_code, f"{key!r} is required and must be a non-empty string",
        )
    return value


def _validate_model_id(model_id: str) -> None:
    if not _MODEL_ID_RE.fullmatch(model_id):
        raise ModelValidationError(
            "invalid_id",
            f"id={model_id!r} must match '<provider>/<model>' — provider must "
            f"start with a letter/digit and contain only letters, digits, dot, "
            f"underscore, hyphen (no slash); model may be any non-empty text "
            f"with no whitespace or control characters (further slashes allowed)",
        )


def _validate_label(label: Any) -> None:
    if not isinstance(label, str) or not label.strip():
        raise ModelValidationError(
            "invalid_label", "label must be a non-empty string",
        )
    if len(label) > _MAX_LABEL_LEN:
        raise ModelValidationError(
            "invalid_label",
            f"label longer than {_MAX_LABEL_LEN} chars",
        )


def _validate_tier(tier: Any) -> None:
    if tier not in _TIERS:
        raise ModelValidationError(
            "invalid_tier",
            f"tier={tier!r} must be one of {sorted(_TIERS)}",
        )


def _validate_context_window(cw: Any) -> None:
    # `bool` is a subclass of `int` in Python; reject it explicitly so
    # `context_window=True` cannot slip through as `1`.
    if isinstance(cw, bool) or not isinstance(cw, int):
        raise ModelValidationError(
            "invalid_context_window",
            f"context_window must be a positive integer, got {cw!r}",
        )
    if cw <= 0 or cw > _MAX_CONTEXT_WINDOW:
        raise ModelValidationError(
            "invalid_context_window",
            f"context_window={cw!r} out of range (1..{_MAX_CONTEXT_WINDOW})",
        )


def _validate_status(status: Any) -> None:
    if status not in _STATUSES:
        raise ModelValidationError(
            "invalid_status",
            f"status={status!r} must be one of {sorted(_STATUSES)}",
        )


# =============================================================================
# v2 surface — cycle-01 of story 414f92d6.
#
# The `*_v2` functions and the two context-window helpers act on the
# cycle-01 tables (`model_providers`) and the Phase-A3 `models` table
# extended by `models_add_hierarchy_columns.sql`. This is now the ONLY
# surface in this module — the original v1 surface (family A,
# `model_registry`/`model_provider_registry`) was removed 2026-08-06
# after confirming zero live callers. See:
#   repo/stories/414f92d6/plans/cycle-01-hierarchical-data-model.md
# =============================================================================

# Local import of the pure-Python variant helper. `_to_size_suffix` is the
# same power-of-1000 validator the admin UI + tests use, so a tokens value
# that would fail there also fails here — one rule for wire-format sizes.
from app.services.model_registry.synthesizer import _to_size_suffix


# ---------------------------------------------------------------------------
# v2 constants — every allowlist / regex / cap the v2 validators consult.
# ---------------------------------------------------------------------------

# `runtime_status` values must match the CHECK constraint on model_providers.
# Inventory bookkeeping only — this field does NOT gate execution anywhere
# in the app. Anyone consuming it as an execution gate is a bug (T7
# code-reviewer greps for exactly that).
_RUNTIME_STATUSES: frozenset[str] = frozenset(
    {"provisioned", "not_provisioned"}
)

# Columns a PATCH request may target on each v2 table. Any patch key outside
# these sets raises BEFORE any SQL is constructed — the dynamic UPDATE
# builder only ever reads column names from these frozensets.
_PROVIDER_PATCH_COLS_V2: frozenset[str] = frozenset(
    {"label", "base_url", "api_key_source", "api_type",
     "runtime_status", "status",
     "secret_project", "secret_id", "secret_version"}
)
_BASE_MODEL_PATCH_COLS_V2: frozenset[str] = frozenset(
    {"label", "tier", "context_window", "provider_shorthand",
     "available_context_windows", "tags", "status"}
)

# `model_providers.id` shape: mixed-case alnum + dot / underscore / hyphen.
# Cycle-01 of story `5dafee43` relaxed the character class from
# lowercase+hyphen to allow upstream naming conventions like
# `Anthropic`, `azure.us`, `codex_cli`, etc. (The v1 provider-name
# regex this used to be compared against was removed 2026-08-06 along
# with the rest of family A.)
_MODEL_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# `models.provider_shorthand` shape: same alphabet as v2 provider id
# (e.g. 'flash', 'Sonnet5', 'opus48', 'preview.04-17'). Cap chosen well
# above any real shorthand.
_PROVIDER_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
_MAX_PROVIDER_SHORTHAND_LEN = 40

# Column list read for each v2 provider row. Explicit so a future column
# added to `model_providers` cannot silently leak into an admin response.
_PROVIDER_V2_COLS = (
    "id, label, base_url, api_key_source, api_type, "
    "runtime_status, status, created_at, updated_at, "
    "secret_project, secret_id, secret_version, "
    "credential_status, credential_error, credential_checked_at"
)

# Column list read for each v2 base-model row. Includes the Phase-A3
# generated `wire_id` (FK target for six other tables) and the
# cycle-01 hierarchy column (`provider_shorthand`). `max_output_tokens`/
# `gateway_params` and the array-form `available_context_windows` are
# not just omitted from this list, they no longer exist on `models` at
# all — removed 2026-08-06 (see `models_table.sql`/
# `models_add_hierarchy_columns.sql`'s headers) after confirming nothing
# ever read or wrote them.
#
# `available_context_windows` below is a correlated subquery against
# `model_context_windows` — the junction table added by
# `model_context_windows_table.sql` is the live read/write path (§7 item 5
# normalization) and always has been; this key name is reused for the
# subquery alias only because it matches the wire/admin-UI field name
# consumers expect, not because a column of that name still exists. The
# subquery references the bare `models` table name, which resolves
# correctly both in a plain `SELECT ... FROM models` and in an
# INSERT/UPDATE `RETURNING` clause on `models` (no alias needed in either
# context), so this one constant serves both read and write call sites
# unchanged.
#
# `tags` is likewise a correlated subquery against `model_tags` — see
# `migrations/model_tags_table.sql`. Two tag names are wire-meaningful
# ('reasoning', 'vision'); the emitter interprets them into the wire
# contract's `reasoning`/`input` fields (`emitter.build_models_payload`).
_BASE_MODEL_V2_COLS = (
    "id, provider_id, model_name, wire_id, label, tier, context_window, "
    "provider_shorthand, "
    "(SELECT COALESCE(array_agg(cw.tokens ORDER BY cw.tokens), '{}'::integer[]) "
    "   FROM model_context_windows cw WHERE cw.model_id = models.id) "
    "  AS available_context_windows, "
    "(SELECT COALESCE(array_agg(mt.tag_name ORDER BY mt.tag_name), '{}'::text[]) "
    "   FROM model_tags mt WHERE mt.model_id = models.id) "
    "  AS tags, "
    "status, created_at, updated_at, gateway_reachable, gateway_checked_at"
)


# ---------------------------------------------------------------------------
# v2 read helpers — admin-facing surface for `model_providers` and the
# extended `models` table.
# ---------------------------------------------------------------------------


async def list_providers_v2(
    pool, *, include_disabled: bool = True,
) -> list[dict]:
    """Return every model_providers row as a plain dict.

    If `include_disabled` is False, only `status='active'` rows are
    returned. Ordered by `id` for stable output.
    """
    if include_disabled:
        rows = await pool.fetch(
            f"SELECT {_PROVIDER_V2_COLS} "
            "  FROM model_providers "
            " ORDER BY id"
        )
    else:
        rows = await pool.fetch(
            f"SELECT {_PROVIDER_V2_COLS} "
            "  FROM model_providers "
            " WHERE status = 'active' "
            " ORDER BY id"
        )
    return [dict(r) for r in rows]


async def get_provider_v2(pool, id: str) -> dict | None:
    """Fetch one model_providers row by id, or None if absent."""
    row = await pool.fetchrow(
        f"SELECT {_PROVIDER_V2_COLS} "
        "  FROM model_providers "
        " WHERE id = $1",
        id,
    )
    return dict(row) if row else None


async def list_base_models_v2(
    pool, *, include_disabled: bool = True,
) -> list[dict]:
    """Return every models row (extended v2 view) as a plain dict.

    If `include_disabled` is False, only `status='active'` rows are
    returned. Ordered by `id` for stable output.
    """
    if include_disabled:
        rows = await pool.fetch(
            f"SELECT {_BASE_MODEL_V2_COLS} "
            "  FROM models "
            " ORDER BY id"
        )
    else:
        rows = await pool.fetch(
            f"SELECT {_BASE_MODEL_V2_COLS} "
            "  FROM models "
            " WHERE status = 'active' "
            " ORDER BY id"
        )
    return [dict(r) for r in rows]


_BASE_MODEL_WITH_PROVIDER_V2_COLS = (
    "models.id, models.provider_id, models.model_name, models.wire_id, models.label, "
    "models.tier, models.context_window, models.provider_shorthand, "
    "(SELECT COALESCE(array_agg(cw.tokens ORDER BY cw.tokens), '{}'::integer[]) "
    "   FROM model_context_windows cw WHERE cw.model_id = models.id) "
    "  AS available_context_windows, "
    "(SELECT COALESCE(array_agg(mt.tag_name ORDER BY mt.tag_name), '{}'::text[]) "
    "   FROM model_tags mt WHERE mt.model_id = models.id) "
    "  AS tags, "
    "models.status, models.created_at, models.updated_at, "
    "models.gateway_reachable, models.gateway_checked_at, "
    "model_providers.runtime_status AS provider_runtime_status, "
    "model_providers.status AS provider_status, "
    "model_providers.credential_status AS provider_credential_status, "
    "model_providers.credential_error AS provider_credential_error"
)


async def list_base_models_with_provider_v2(pool, *, include_disabled: bool = False) -> list[dict]:
    """Same rows as `list_base_models_v2`, joined to each model's owning
    `model_providers` row for the provider-level status fields
    (`provider_runtime_status`/`provider_status`/`provider_credential_status`/
    `provider_credential_error`) that `availability.is_model_selectable`/
    `model_credential_warning` need.

    A dedicated function rather than extending `list_base_models_v2`
    itself: that function is also read by the admin page and
    `platform_council/inventory.py`, neither of which needs (or should
    silently start depending on) the joined shape — this keeps "one
    more field" scoped to the one caller that actually needs it
    (`dropdowns.py`, and everything that goes through it:
    `build_go_model_tree_v2`, `list_model_ids_for_dropdown_v2`,
    `is_model_registered_v2` — the latter is what gates
    `change_agent_model`/`create_goal`, so this is also how the
    Agent Registry's model-selection paths get the same provider-status
    awareness as the dropdowns).

    INNER JOIN, not LEFT — every `models.provider_id` is required to
    resolve to a `model_providers` row at write time
    (`create_base_model_v2`'s application-layer FK check), so a model
    with no matching provider row would indicate a data-integrity bug,
    not a valid state to render around.
    """
    where = "" if include_disabled else "WHERE models.status = 'active'"
    rows = await pool.fetch(
        f"SELECT {_BASE_MODEL_WITH_PROVIDER_V2_COLS} "
        "  FROM models "
        "  JOIN model_providers ON model_providers.id = models.provider_id "
        f" {where} "
        " ORDER BY models.id"
    )
    return [dict(r) for r in rows]


async def get_base_model_v2(pool, model_id: str) -> dict | None:
    """Fetch one models row by id (extended v2 view), or None if absent."""
    row = await pool.fetchrow(
        f"SELECT {_BASE_MODEL_V2_COLS} "
        "  FROM models "
        " WHERE id = $1",
        model_id,
    )
    return dict(row) if row else None


async def list_tags_v2(pool) -> list[dict]:
    """Every row in the `tags` catalogue — for the admin UI's tag picker.

    Returns `[{"name": ..., "description": ...}, ...]` ordered by name.
    Unlike `model_tags` (per-model assignments, read via the
    `_BASE_MODEL_V2_COLS` subquery), this is the full set of tag names
    available to assign.
    """
    rows = await pool.fetch(
        "SELECT name, description FROM tags ORDER BY name"
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# v2 provider writes.
# ---------------------------------------------------------------------------


async def create_provider_v2(pool, payload: dict) -> dict:
    """Insert a new model_providers row.

    Required payload keys: `id`, `label`.
    Optional: `base_url`, `api_key_source`, `api_type`, `runtime_status`
    (default 'provisioned'), `secret_project`, `secret_id`,
    `secret_version` (default 'latest' — the DB column default applies
    when omitted).

    `secret_project`/`secret_id` are the Secret Manager reference the
    gateway resolves itself — see
    research/model_registry_secret_management_design.md. Never a secret
    value; nothing here ever touches Secret Manager from AgentOS's side.

    Raises `ProviderValidationError` on validation failure or duplicate
    PK. `status` is always 'active' on create — use
    `set_provider_status_v2` to disable a provider afterwards.
    """
    provider_id = payload.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise ProviderValidationError(
            "invalid_id",
            "'id' is required and must be a non-empty string",
        )
    _validate_model_provider_id(provider_id)

    _validate_provider_label(payload.get("label"))
    label = payload["label"]

    runtime_status = payload.get("runtime_status")
    if runtime_status is None:
        runtime_status = "provisioned"
    else:
        _validate_runtime_status(runtime_status)

    base_url = _optional_str(payload, "base_url")
    api_key_source = _optional_str(payload, "api_key_source")
    api_type = _optional_str(payload, "api_type")
    _validate_api_type(api_type)
    secret_project = _optional_str(payload, "secret_project")
    secret_id = _optional_str(payload, "secret_id")
    secret_version = _optional_str(payload, "secret_version") or "latest"

    try:
        row = await pool.fetchrow(
            "INSERT INTO model_providers "
            "  (id, label, base_url, api_key_source, api_type, runtime_status, "
            "   secret_project, secret_id, secret_version) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            f"RETURNING {_PROVIDER_V2_COLS}",
            provider_id, label, base_url, api_key_source, api_type,
            runtime_status, secret_project, secret_id, secret_version,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise ProviderValidationError(
            "duplicate_id",
            f"model_providers row already exists for id={provider_id!r}",
            status=409,
        ) from exc

    logger.info(
        "model_providers: created provider id=%s runtime_status=%s",
        provider_id, runtime_status,
    )
    return dict(row)


async def update_provider_v2(pool, id: str, patch: dict) -> dict:
    """Partial update of one model_providers row.

    `patch` keys drawn from `_PROVIDER_PATCH_COLS_V2`. Empty patch or
    any unknown key raises before SQL is constructed. `base_url`,
    `api_key_source`, and `api_type` accept a string (new value) or
    `None` (clear the column). All other keys are enum-validated.
    """
    if not isinstance(patch, dict) or not patch:
        raise ProviderValidationError(
            "empty_patch",
            "patch body must contain at least one field",
            status=400,
        )

    unknown = set(patch) - _PROVIDER_PATCH_COLS_V2
    if unknown:
        raise ProviderValidationError(
            "invalid_patch_field",
            f"unknown patch field(s): {sorted(unknown)}",
            status=400,
        )

    if "label" in patch:
        _validate_provider_label(patch["label"])
    if "runtime_status" in patch:
        _validate_runtime_status(patch["runtime_status"])
    if "status" in patch:
        _validate_status(patch["status"])
    for k in ("base_url", "api_key_source", "api_type",
              "secret_project", "secret_id"):
        if k in patch and patch[k] is not None and not isinstance(patch[k], str):
            raise ProviderValidationError(
                "invalid_patch_field",
                f"{k!r} must be a string or None, got {type(patch[k]).__name__}",
            )
    if "api_type" in patch:
        _validate_api_type(patch["api_type"])
    # secret_version is NOT NULL (defaults to 'latest') — unlike the fields
    # above, None is not a valid patch value here.
    if "secret_version" in patch and not (
        isinstance(patch["secret_version"], str) and patch["secret_version"]
    ):
        raise ProviderValidationError(
            "invalid_patch_field",
            "'secret_version' must be a non-empty string",
        )

    ordered_keys = [k for k in (
        "label", "base_url", "api_key_source", "api_type",
        "runtime_status", "status",
        "secret_project", "secret_id", "secret_version",
    ) if k in patch]

    set_fragments: list[str] = []
    values: list[Any] = []
    for key in ordered_keys:
        values.append(patch[key])
        set_fragments.append(f"{key} = ${len(values)}")
    if "secret_id" in patch:
        # Rotating to a (possibly different) secret invalidates whatever
        # credential_status was recorded for the OLD one — including a
        # sticky 'revoked' mark, which must not silently survive onto a
        # brand-new secret the admin just pointed this provider at.
        # Literal values, not bound params: fixed reset constants, not
        # user input, so no injection surface from inlining them.
        set_fragments.append("credential_status = 'unknown'")
        set_fragments.append("credential_error = NULL")
        set_fragments.append("credential_checked_at = NULL")
    set_fragments.append("updated_at = now()")
    values.append(id)  # WHERE clause bind

    sql = (
        f"UPDATE model_providers SET {', '.join(set_fragments)} "
        f"WHERE id = ${len(values)} "
        f"RETURNING {_PROVIDER_V2_COLS}"
    )
    row = await pool.fetchrow(sql, *values)
    if row is None:
        raise ProviderValidationError(
            "provider_not_found",
            f"no model_providers row with id={id!r}",
            status=404,
        )

    logger.info(
        "model_providers: updated provider id=%s fields=%s",
        id, ordered_keys,
    )
    return dict(row)


async def set_provider_credential_status_v2(
    pool, id: str, *, healthy: bool, error: str | None = None,
) -> None:
    """Record the gateway's report of whether it could resolve this
    provider's credential — NOT an admin-editable field (no route ever
    accepts these values from a form; only `refresh_provider_credentials`
    in `app/routers/model_registry.py`, parsing a `models.reload` RPC
    response, calls this). AgentOS never resolves the credential itself,
    it only relays what the gateway reports.

    STICKY GUARD: never overwrites a `credential_status='revoked'` row.
    'revoked' is admin-asserted (via `revoke_provider_credential_v2`),
    not gateway-reported, and Secret Manager has no idea a key is dead
    on the provider's own console — it will keep resolving the same
    secret value successfully forever. Without this guard, the very
    next periodic health-report tick (every 5 min, see `main.py`'s
    self-heal loop) would silently flip an admin's "revoked" mark back
    to "healthy", making the revoke action meaningless within minutes.
    The `WHERE` clause below is the actual mechanism — not a Python
    if-check — so there's no read-then-write race against a concurrent
    revoke call.

    No-op (logs a warning) if `id` doesn't exist, OR if it's currently
    'revoked' (the sticky guard firing as intended, not an error) — the
    gateway's report could theoretically arrive for a provider deleted
    since the request was sent either way; don't raise and break the
    caller's response handling over either race.
    """
    result = await pool.execute(
        """UPDATE model_providers
              SET credential_status = $2, credential_error = $3,
                  credential_checked_at = now()
            WHERE id = $1 AND credential_status != 'revoked'""",
        id, "healthy" if healthy else "unhealthy", error,
    )
    if result == "UPDATE 0":
        logger.warning(
            "set_provider_credential_status_v2: no model_providers row for id=%s, "
            "or its credential_status is 'revoked' (sticky guard) — not overwriting", id,
        )


async def revoke_provider_credential_v2(pool, id: str) -> dict:
    """Admin-asserted: mark this provider's credential 'revoked'. The
    ONLY writer of this value — the gateway health-report path
    (`set_provider_credential_status_v2`) can never set or clear it,
    by the sticky guard in that function's WHERE clause.

    Does NOT call Secret Manager's `DisableSecretVersion` (see the
    module-level design doc reference in `migrations/model_providers_secret_refs.sql`
    for why: the gateway's service account only has `secretAccessor`,
    deliberately, and there's no dedicated admin client). This only
    updates AgentOS's own bookkeeping and — via the caller telling the
    gateway to re-sync (`sync_gateway_model_catalog`) — stops the
    resolved value from being injected into the gateway's own auth env
    going forward (see `emitter.build_provider_credentials_payload`,
    which excludes 'revoked' providers from the payload the gateway
    resolves from).

    Raises `ProviderValidationError` if `id` doesn't exist.
    """
    row = await pool.fetchrow(
        """UPDATE model_providers
              SET credential_status = 'revoked', credential_error = NULL,
                  credential_checked_at = now()
            WHERE id = $1
        RETURNING id, credential_status, credential_checked_at""",
        id,
    )
    if row is None:
        raise ProviderValidationError(
            "not_found", f"no provider row for id={id!r}", status=404,
        )
    return dict(row)


async def set_models_gateway_reachability_v2(
    pool, reachability: dict[str, bool],
) -> None:
    """Record the gateway's per-model list-models probe results — same
    "relay only, never admin-editable" rule as
    `set_provider_credential_status_v2`. `reachability` is `{model_id:
    bool}`, keyed by `models.id` (the base-model row id, e.g.
    "anthropic/claude-opus-5" — the probe covers base models; a
    synthesized context-window variant isn't a separate registry row to
    mark, see `management.add_context_window_v2`'s docstring).

    Unknown model ids are skipped with a debug log rather than raising —
    same race-tolerance rationale as the provider-level function above.
    """
    for model_id, reachable in reachability.items():
        result = await pool.execute(
            """UPDATE models
                  SET gateway_reachable = $2, gateway_checked_at = now()
                WHERE id = $1""",
            model_id, reachable,
        )
        if result == "UPDATE 0":
            logger.debug(
                "set_models_gateway_reachability_v2: no models row for id=%s", model_id,
            )


async def set_provider_status_v2(pool, id: str, status: str) -> dict:
    """Toggle provider status with an orphan-model guard on disable.

    Disabling a provider while any active `models` row still references
    it via `provider_id` would leave a dangling reference at the app
    layer — there is no DB-level FK to catch it (Phase A3 trade-off).
    The check runs inside a transaction with `SELECT FOR UPDATE` on the
    provider row to close the race window between the check and the
    UPDATE.

    Enabling is unrestricted.
    """
    _validate_status(status)

    async with pool.acquire() as conn:
        async with conn.transaction():
            provider_row = await conn.fetchrow(
                f"SELECT {_PROVIDER_V2_COLS} "
                "  FROM model_providers "
                " WHERE id = $1 "
                " FOR UPDATE",
                id,
            )
            if provider_row is None:
                raise ProviderValidationError(
                    "provider_not_found",
                    f"no model_providers row with id={id!r}",
                    status=404,
                )

            if status == "disabled":
                orphan = await conn.fetchval(
                    "SELECT 1 FROM models "
                    " WHERE provider_id = $1 "
                    "   AND status = 'active' "
                    " LIMIT 1",
                    id,
                )
                if orphan is not None:
                    raise ProviderValidationError(
                        "provider_in_use",
                        f"cannot disable provider {id!r}: active models rows "
                        f"still reference it",
                        status=409,
                    )

            row = await conn.fetchrow(
                "UPDATE model_providers "
                "   SET status = $1, updated_at = now() "
                " WHERE id = $2 "
                f"RETURNING {_PROVIDER_V2_COLS}",
                status, id,
            )

    logger.info(
        "model_providers: set status id=%s status=%s", id, status,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# v2 base-model writes.
# ---------------------------------------------------------------------------


async def create_base_model_v2(pool, payload: dict) -> dict:
    """Insert a new models row (extended v2 view).

    Required payload keys: `id`, `provider_id`, `label`, `tier`,
    `context_window`.
    Optional: `provider_shorthand`, `available_context_windows`, `tags`
    (list of tag names already present in the `tags` table —
    `unknown_tag` if any name doesn't exist).

    `model_name` is DERIVED server-side from `id.split('/')[1]` — it
    must NOT appear in the payload and is ignored if it does. Mirrors
    the v1 `create_model` pattern that also derives its provider column.

    `provider_id` must resolve to an active `model_providers` row —
    application-layer FK enforcement, since `models.provider_id` has
    no DB-level FK (Phase A3 trade-off preserved).

    A non-empty `available_context_windows` implies a non-null
    `provider_shorthand`, and `shorthand_required` is raised here when it
    isn't. This is now the ONLY enforcement of that rule: the
    `models_shorthand_required_when_windows_nonempty` CHECK that used to
    back it was dropped 2026-08-06 along with the
    `available_context_windows` array column it was written against, and
    a CHECK cannot be re-expressed against the `model_context_windows`
    junction table (a CHECK may not reference another table). Keep this
    check and `add_context_window_v2`'s matching one in step — between
    them they are the whole guarantee.
    """
    model_id = payload.get("id")
    if not isinstance(model_id, str) or not model_id:
        raise ModelValidationError(
            "invalid_id",
            "'id' is required and must be a non-empty string",
        )
    _validate_model_id(model_id)
    model_name = model_id.split("/", 1)[1]  # derived, never from payload

    provider_id = payload.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        raise ModelValidationError(
            "invalid_provider_id",
            "'provider_id' is required and must be a non-empty string",
        )
    if not _MODEL_PROVIDER_ID_RE.fullmatch(provider_id):
        raise ModelValidationError(
            "invalid_provider_id",
            f"provider_id={provider_id!r} must match "
            f"'^[A-Za-z0-9][A-Za-z0-9._-]*$'",
        )

    label = _require_str(payload, "label", "invalid_label")
    _validate_label(label)

    tier = _require_str(payload, "tier", "invalid_tier")
    _validate_tier(tier)

    context_window = payload.get("context_window")
    _validate_context_window(context_window)

    provider_shorthand = payload.get("provider_shorthand")
    if provider_shorthand is not None:
        _validate_provider_shorthand(provider_shorthand)

    windows_raw = payload.get("available_context_windows")
    if windows_raw is None:
        windows: list[int] = []
    else:
        windows = _validate_available_context_windows(windows_raw)
    if windows and provider_shorthand is None:
        raise ModelValidationError(
            "shorthand_required",
            "available_context_windows is non-empty but provider_shorthand "
            "is null — set provider_shorthand first",
        )
    if windows and max(windows) >= context_window:
        raise ModelValidationError(
            "context_window_exceeds_base",
            f"available_context_windows contains {max(windows)}, which "
            f"is >= this model's own context_window={context_window!r} — "
            f"a context-window variant must be strictly smaller than the "
            f"model's native max",
        )

    tags_raw = payload.get("tags")
    tags = _validate_tags(tags_raw) if tags_raw is not None else []

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _check_provider_registered_v2(conn, provider_id)
            try:
                await conn.execute(
                    "INSERT INTO models "
                    "  (id, provider_id, model_name, label, tier, "
                    "   context_window, provider_shorthand) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    model_id, provider_id, model_name, label, tier,
                    context_window, provider_shorthand,
                )
            except asyncpg.exceptions.UniqueViolationError as exc:
                raise ModelValidationError(
                    "duplicate_id",
                    f"models row already exists for id={model_id!r}",
                    status=409,
                ) from exc
            # `model_context_windows`/`model_tags` FK-reference `models.id`,
            # so these inserts must follow the models row, not accompany
            # it — that's also why the final row re-fetch (below) can't
            # reuse a RETURNING on the INSERT above: at that point in the
            # transaction the junction rows don't exist yet.
            for tokens in windows:
                await conn.execute(
                    "INSERT INTO model_context_windows (model_id, tokens) "
                    "VALUES ($1, $2)",
                    model_id, tokens,
                )
            for tag_name in tags:
                try:
                    await conn.execute(
                        "INSERT INTO model_tags (model_id, tag_name) "
                        "VALUES ($1, $2)",
                        model_id, tag_name,
                    )
                except asyncpg.exceptions.ForeignKeyViolationError as exc:
                    raise ModelValidationError(
                        "unknown_tag",
                        f"tag_name={tag_name!r} does not exist in `tags` — "
                        f"create it first (or fix the typo)",
                    ) from exc
            row = await conn.fetchrow(
                f"SELECT {_BASE_MODEL_V2_COLS} FROM models WHERE id = $1",
                model_id,
            )

    logger.info(
        "models: created base model id=%s provider_id=%s tier=%s cw=%d",
        model_id, provider_id, tier, context_window,
    )
    return dict(row)


async def update_base_model_v2(pool, model_id: str, patch: dict) -> dict:
    """Partial update of one models row (extended v2 view).

    `patch` keys drawn from `_BASE_MODEL_PATCH_COLS_V2`. `id`,
    `provider_id`, and `model_name` are immutable — moving a model
    between providers is out of scope for cycle-01.

    Enforces the invariant `available_context_windows non-empty ⇒
    provider_shorthand IS NOT NULL` against the MERGED post-patch state
    (patch value + current row value). Runs inside a transaction with
    `SELECT FOR UPDATE` on the row so a concurrent update cannot slip
    the invariant past the check.
    """
    if not isinstance(patch, dict) or not patch:
        raise ModelValidationError(
            "empty_patch",
            "patch body must contain at least one field",
            status=400,
        )

    unknown = set(patch) - _BASE_MODEL_PATCH_COLS_V2
    if unknown:
        raise ModelValidationError(
            "invalid_patch_field",
            f"unknown patch field(s): {sorted(unknown)}",
            status=400,
        )

    if "label" in patch:
        _validate_label(patch["label"])
    if "tier" in patch:
        _validate_tier(patch["tier"])
    if "context_window" in patch:
        _validate_context_window(patch["context_window"])
    if "status" in patch:
        _validate_status(patch["status"])
    if "provider_shorthand" in patch and patch["provider_shorthand"] is not None:
        _validate_provider_shorthand(patch["provider_shorthand"])
    if "available_context_windows" in patch:
        # Replace patch value with the canonical sorted form so the SQL
        # binds a normalised INTEGER[] and stored arrays are always in
        # ascending order regardless of what the caller passed.
        patch["available_context_windows"] = _validate_available_context_windows(
            patch["available_context_windows"]
        )
    if "tags" in patch:
        patch["tags"] = _validate_tags(patch["tags"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Peek at the current row so we can compute the post-patch
            # state of (provider_shorthand, available_context_windows) and
            # enforce the CHECK invariant BEFORE the UPDATE fires it as a
            # raw constraint violation. FOR UPDATE closes the race window.
            # `available_context_windows` is read via the same junction-
            # table subquery `_BASE_MODEL_V2_COLS` uses elsewhere.
            current = await conn.fetchrow(
                "SELECT provider_shorthand, context_window, "
                "  (SELECT COALESCE(array_agg(cw.tokens ORDER BY cw.tokens), "
                "                   '{}'::integer[]) "
                "     FROM model_context_windows cw "
                "    WHERE cw.model_id = models.id) AS available_context_windows "
                "  FROM models "
                " WHERE id = $1 "
                " FOR UPDATE",
                model_id,
            )
            if current is None:
                raise ModelValidationError(
                    "model_not_found",
                    f"no models row with id={model_id!r}",
                    status=404,
                )

            eff_shorthand = (
                patch["provider_shorthand"]
                if "provider_shorthand" in patch
                else current["provider_shorthand"]
            )
            eff_windows = (
                patch["available_context_windows"]
                if "available_context_windows" in patch
                else list(current["available_context_windows"] or [])
            )
            eff_context_window = patch.get("context_window", current["context_window"])
            if eff_windows and eff_shorthand is None:
                raise ModelValidationError(
                    "shorthand_required",
                    "post-patch state would have non-empty "
                    "available_context_windows and null provider_shorthand",
                )
            # Only enforced when the patch actually touches one of the two
            # operands. Evaluating it unconditionally made a pre-existing
            # violation permanently un-editable: the check re-reads the CURRENT
            # windows and context_window whenever the patch omits them, so a
            # label-only, tier-only, tags-only or status-only patch was judged
            # against data it wasn't changing and rejected.
            #
            # That was not hypothetical — the seed migrations insert into
            # `model_context_windows` directly and bypass
            # `add_context_window_v2`'s equivalent guard, so four seeded base
            # models (claude-sonnet-4-5, claude-opus-4-6, gemini-2.5-flash,
            # gemini-2.5-pro) each carry a window >= their own native size.
            # Every admin edit of those four failed with a 400, and because
            # `set_base_model_status_v2` routes through here, so did disabling
            # or re-enabling them. Verified against the live feature data
            # 2026-08-13: a no-op label patch on gemini-2.5-pro was rejected.
            #
            # Both intended rejections survive: patching IN an oversized window
            # (`available_context_windows` present) and SHRINKING the base below
            # an existing variant (`context_window` present) are still caught.
            if (
                ("context_window" in patch or "available_context_windows" in patch)
                and eff_windows
                and max(eff_windows) >= eff_context_window
            ):
                raise ModelValidationError(
                    "context_window_exceeds_base",
                    f"post-patch state would have available_context_windows "
                    f"containing {max(eff_windows)}, which is >= "
                    f"context_window={eff_context_window!r} — a context-window "
                    f"variant must be strictly smaller than the model's "
                    f"native max",
                )

            # `available_context_windows` lives in model_context_windows,
            # not a `models` column — it's excluded from the dynamic SET
            # builder below and instead replaced (delete-then-insert) in
            # its own junction-table statements, run BEFORE the final
            # SELECT so that query's subquery sees the new state.
            # NOTE: this generic patch path does NOT materialize a `models`
            # row for each window the way `add_context_window_v2` does
            # (see that function's docstring for why materialization is
            # required). It isn't reachable from the admin UI today — the
            # UI only calls the dedicated `/context-windows` routes — but
            # any other caller replacing `available_context_windows`
            # wholesale through this path will create windows a client
            # can select (`is_model_registered_v2` returns True) with no
            # backing `models` row, reproducing the bug
            # `add_context_window_v2` fixes. Route new callers through
            # `add_context_window_v2`/`remove_context_window_v2` instead.
            if "available_context_windows" in patch:
                await conn.execute(
                    "DELETE FROM model_context_windows WHERE model_id = $1",
                    model_id,
                )
                for tokens in patch["available_context_windows"]:
                    await conn.execute(
                        "INSERT INTO model_context_windows (model_id, tokens) "
                        "VALUES ($1, $2)",
                        model_id, tokens,
                    )

            # `tags` lives in model_tags, not a `models` column — same
            # delete-then-insert treatment as available_context_windows,
            # also run before the final SELECT.
            if "tags" in patch:
                await conn.execute(
                    "DELETE FROM model_tags WHERE model_id = $1",
                    model_id,
                )
                for tag_name in patch["tags"]:
                    try:
                        await conn.execute(
                            "INSERT INTO model_tags (model_id, tag_name) "
                            "VALUES ($1, $2)",
                            model_id, tag_name,
                        )
                    except asyncpg.exceptions.ForeignKeyViolationError as exc:
                        raise ModelValidationError(
                            "unknown_tag",
                            f"tag_name={tag_name!r} does not exist in "
                            f"`tags` — create it first (or fix the typo)",
                        ) from exc

            ordered_keys = [k for k in (
                "label", "tier", "context_window", "provider_shorthand",
                "status",
            ) if k in patch]

            set_fragments: list[str] = []
            values: list[Any] = []
            for key in ordered_keys:
                values.append(patch[key])
                set_fragments.append(f"{key} = ${len(values)}")
            set_fragments.append("updated_at = now()")
            values.append(model_id)  # WHERE clause bind

            sql = (
                f"UPDATE models SET {', '.join(set_fragments)} "
                f"WHERE id = ${len(values)} "
                f"RETURNING {_BASE_MODEL_V2_COLS}"
            )
            row = await conn.fetchrow(sql, *values)

    logger.info(
        "models: updated base model id=%s fields=%s",
        model_id, ordered_keys,
    )
    return dict(row)


async def set_base_model_status_v2(
    pool, model_id: str, status: str,
) -> dict:
    """Thin wrapper — factored so `/disable` and `/enable` share validation."""
    _validate_status(status)
    return await update_base_model_v2(pool, model_id, {"status": status})


# ---------------------------------------------------------------------------
# Context-window helpers — dedicated single-element operations for the
# guided admin add-flow. Wrapping these on top of `update_base_model_v2`
# would work but muddies the error semantics (`duplicate_window` /
# `window_not_present` vs a generic `invalid_available_context_windows`),
# so they get their own tx-locked paths.
# ---------------------------------------------------------------------------


async def add_context_window_v2(
    pool, model_id: str, tokens: int,
) -> dict:
    """Add one context-window value to a base-model row.

    A context-window variant is not a separate model — it's the same
    upstream model at a different size, fully described by this base
    row's `available_context_windows`. This function only ever writes to
    `model_context_windows`; it does NOT create a `models` or
    `model_providers` row for the variant. The gateway wire contract's
    per-variant mapping (`{upstream, upstreamId, contextWindow}`) is
    computed on the fly by the emitter (`synthesizer.synthesize_variant`)
    at request time, not read from a stored row — see
    `documentation/MODEL_REGISTRY_CONTRACT.md`.

    (An earlier version of this function DID materialize a `models` row
    per variant, to satisfy `agents.model_id`'s FK for a selected
    variant. That's the wrong fix: `resolve_model_id` now resolves a
    variant id to its BASE model's row instead, so `model_id` still
    means something without needing one row per size — see that
    function's docstring.)

    `tokens` is validated via the shared `_to_size_suffix` helper so a
    value that would fail there also fails here — one rule for wire-
    format sizes. Opens a transaction with `SELECT FOR UPDATE` on the
    base row so a concurrent add cannot slip past the duplicate guard.

    Errors:
      * `invalid_tokens`     (400) if `tokens` is not a power-of-1000 size.
      * `model_not_found`    (404) if no row exists for `model_id`.
      * `shorthand_required` (400) if the row's `provider_shorthand` is
                                    null. Without a short name the
                                    synthesizer cannot build a variant id
                                    for this window, so the row would be
                                    written but unusable. This check is
                                    the real enforcement — the DB CHECK
                                    that once backed it was dropped with
                                    the `available_context_windows`
                                    column (see `create_base_model_v2`).
      * `context_window_exceeds_base` (400) if `tokens` is >= the base
                                    row's own `context_window` — a
                                    context-window variant is a strictly
                                    *smaller* slice of the model's native
                                    max, never equal to or larger than it.
                                    (Equal is rejected, not just greater:
                                    the native size is already always
                                    offered as its own selectable entry —
                                    see `dropdowns.build_go_model_tree_v2`
                                    — so a same-sized variant would only
                                    add an indistinguishable duplicate
                                    option in the size picker.)
      * `duplicate_window`   (409) if `tokens` is already present.
    """
    try:
        _to_size_suffix(tokens)
    except ValueError as exc:
        raise ModelValidationError(
            "invalid_tokens",
            f"tokens={tokens!r} is not a valid power-of-1000 size: {exc}",
            status=400,
        ) from exc

    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT provider_shorthand, context_window FROM models "
                " WHERE id = $1 "
                " FOR UPDATE",
                model_id,
            )
            if current is None:
                raise ModelValidationError(
                    "model_not_found",
                    f"no models row with id={model_id!r}",
                    status=404,
                )
            if current["provider_shorthand"] is None:
                raise ModelValidationError(
                    "shorthand_required",
                    f"cannot add a context window to {model_id!r}: "
                    f"provider_shorthand is null — set it via "
                    f"update_base_model_v2 first",
                )
            if tokens >= current["context_window"]:
                raise ModelValidationError(
                    "context_window_exceeds_base",
                    f"tokens={tokens} must be strictly smaller than "
                    f"{model_id!r}'s own context_window="
                    f"{current['context_window']!r} — a context-window "
                    f"variant cannot equal or exceed the model's native "
                    f"max (the native size is already offered on its "
                    f"own; see build_go_model_tree_v2)",
                )
            # The junction table's PRIMARY KEY (model_id, tokens) is the
            # duplicate guard — the FOR UPDATE lock on the base row above
            # still serializes concurrent adds for the same model.
            try:
                await conn.execute(
                    "INSERT INTO model_context_windows (model_id, tokens) "
                    "VALUES ($1, $2)",
                    model_id, tokens,
                )
            except asyncpg.exceptions.UniqueViolationError as exc:
                raise ModelValidationError(
                    "duplicate_window",
                    f"tokens={tokens} already present in "
                    f"available_context_windows for id={model_id!r}",
                    status=409,
                ) from exc

            row = await conn.fetchrow(
                f"SELECT {_BASE_MODEL_V2_COLS} FROM models WHERE id = $1",
                model_id,
            )

    logger.info(
        "models: added context window id=%s tokens=%d", model_id, tokens,
    )
    return dict(row)


async def remove_context_window_v2(
    pool, model_id: str, tokens: int,
) -> dict:
    """Remove one context-window value from a base-model row.

    Only ever touches `model_context_windows` — see
    `add_context_window_v2`'s docstring for why no `models`/
    `model_providers` row exists for a variant to disable in the first
    place.

    `tokens` need not pass `_to_size_suffix` — removing an
    already-stored value should always be possible even if the value
    pre-dates a stricter future rule. Opens a tx with `SELECT FOR
    UPDATE` so concurrent removals see a coherent view.

    Errors:
      * `model_not_found`    (404) if no row exists for `model_id`.
      * `window_not_present` (404) if `tokens` isn't in the row's array.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id FROM models WHERE id = $1 FOR UPDATE",
                model_id,
            )
            if current is None:
                raise ModelValidationError(
                    "model_not_found",
                    f"no models row with id={model_id!r}",
                    status=404,
                )
            deleted = await conn.fetchval(
                "DELETE FROM model_context_windows "
                " WHERE model_id = $1 AND tokens = $2 "
                "RETURNING tokens",
                model_id, tokens,
            )
            if deleted is None:
                raise ModelValidationError(
                    "window_not_present",
                    f"tokens={tokens} is not in available_context_windows "
                    f"for id={model_id!r}",
                    status=404,
                )

            row = await conn.fetchrow(
                f"SELECT {_BASE_MODEL_V2_COLS} FROM models WHERE id = $1",
                model_id,
            )

    logger.info(
        "models: removed context window id=%s tokens=%d", model_id, tokens,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# v2 private validators.
# ---------------------------------------------------------------------------


def _optional_str(payload: dict, key: str) -> Any:
    """Return payload[key] if it's a string, or None if absent/None/empty.

    Used for the trio of nullable string columns on `model_providers`
    (`base_url`, `api_key_source`, `api_type`) that share the same
    "string or NULL" shape. Non-string values raise
    ProviderValidationError.
    """
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderValidationError(
            "invalid_patch_field",
            f"{key!r} must be a string or None, got {type(value).__name__}",
        )
    return value


def _validate_provider_label(label: Any) -> None:
    """Provider-domain label validator.

    Same rules as `_validate_label` but raises `ProviderValidationError`
    so the exception domain matches the payload domain being validated.
    """
    if not isinstance(label, str) or not label.strip():
        raise ProviderValidationError(
            "invalid_label", "label must be a non-empty string",
        )
    if len(label) > _MAX_LABEL_LEN:
        raise ProviderValidationError(
            "invalid_label",
            f"label longer than {_MAX_LABEL_LEN} chars",
        )


def _validate_model_provider_id(provider_id: Any) -> None:
    """Shape check for `model_providers.id`."""
    if not isinstance(provider_id, str) or not _MODEL_PROVIDER_ID_RE.fullmatch(provider_id):
        raise ProviderValidationError(
            "invalid_id",
            f"id={provider_id!r} must match "
            f"'^[A-Za-z0-9][A-Za-z0-9._-]*$'",
        )
    if len(provider_id) > _MAX_PROVIDER_NAME_LEN:
        raise ProviderValidationError(
            "invalid_id",
            f"provider id longer than {_MAX_PROVIDER_NAME_LEN} chars",
        )


def _validate_provider_shorthand(shorthand: Any) -> None:
    """Shape check for `models.provider_shorthand`.

    e.g. 'flash', 'pro', 'sonnet', 'sonnet5', 'opus', 'opus48'. Empty
    string is rejected — callers who want to CLEAR the column pass
    `None`, which bypasses this validator upstream.
    """
    if not isinstance(shorthand, str) or not _PROVIDER_SHORTHAND_RE.fullmatch(shorthand):
        raise ModelValidationError(
            "invalid_shorthand",
            f"provider_shorthand={shorthand!r} must match "
            f"'^[A-Za-z0-9][A-Za-z0-9._-]*$'",
        )
    if len(shorthand) > _MAX_PROVIDER_SHORTHAND_LEN:
        raise ModelValidationError(
            "invalid_shorthand",
            f"provider_shorthand longer than "
            f"{_MAX_PROVIDER_SHORTHAND_LEN} chars",
        )


def _validate_api_type(api_type: Any) -> None:
    """Reject an `api_type` the gateway would refuse.

    Added 2026-08-09 after an admin-created provider carrying an api type
    the gateway's schema doesn't recognise broke provider sync PLATFORM-WIDE:
    the sync writes the whole config document in one `config.set`, so one
    unrecognised value fails validation for every provider at once, and the
    soft-fail around it swallows the error. It stayed broken, silently, until
    someone read the logs.

    `_EMITTABLE_API_TYPES` mirrors the gateway's closed `ModelApiSchema` enum
    — see `emitter._GATEWAY_ACCEPTED_API_TYPES`, which is the same list and
    the reason it exists. `cli` is accepted here but NOT there on purpose:
    AgentOS uses it to mark CLI-backed providers, which are never emitted to
    the gateway (they have no `base_url`), so it is meaningful to us and
    meaningless to it.

    None passes: `api_type` is nullable, and a provider without one simply
    isn't emitted.
    """
    if api_type is None:
        return
    if api_type not in _VALID_API_TYPES:
        raise ProviderValidationError(
            "invalid_api_type",
            f"api_type={api_type!r} is not one of {sorted(_VALID_API_TYPES)}",
        )


def _validate_runtime_status(runtime_status: Any) -> None:
    if runtime_status not in _RUNTIME_STATUSES:
        raise ProviderValidationError(
            "invalid_runtime_status",
            f"runtime_status={runtime_status!r} must be one of "
            f"{sorted(_RUNTIME_STATUSES)}",
        )


def _validate_available_context_windows(windows: Any) -> list[int]:
    """Validate and return a canonical sorted copy of the windows array.

    Rules (mirrored by the DB shape but enforced here for clean errors):
      * Must be a list.
      * Every element is a positive integer <= _MAX_CONTEXT_WINDOW.
        `bool` is rejected explicitly since `isinstance(True, int)` is
        True in Python.
      * No duplicates.

    An empty list is valid (means 'no synthetic variants'). Returns a
    new list sorted ascending; callers write THIS value to the DB so
    the stored array is always in canonical form.

    The cross-field rule (non-empty ⇒ provider_shorthand not null) is
    enforced at the callsite, not here — this validator does not know
    the value of shorthand.
    """
    if not isinstance(windows, list):
        raise ModelValidationError(
            "invalid_available_context_windows",
            f"available_context_windows must be a list, got "
            f"{type(windows).__name__}",
        )
    seen: set[int] = set()
    for w in windows:
        if isinstance(w, bool) or not isinstance(w, int):
            raise ModelValidationError(
                "invalid_available_context_windows",
                f"available_context_windows entries must be positive "
                f"integers, got {w!r}",
            )
        if w <= 0 or w > _MAX_CONTEXT_WINDOW:
            raise ModelValidationError(
                "invalid_available_context_windows",
                f"available_context_windows entry {w!r} out of range "
                f"(1..{_MAX_CONTEXT_WINDOW})",
            )
        if w in seen:
            raise ModelValidationError(
                "invalid_available_context_windows",
                f"available_context_windows contains duplicate entry {w!r}",
            )
        seen.add(w)
    return sorted(seen)


def _validate_tags(tags: Any) -> list[str]:
    """Validate and return a canonical sorted, deduplicated copy of a tag
    list. Existence of each name in the `tags` table is NOT checked here
    (no pool access at this layer) — the caller's INSERT INTO model_tags
    relies on the FK and translates a violation into `unknown_tag`.
    """
    if not isinstance(tags, list):
        raise ModelValidationError(
            "invalid_tags",
            f"tags must be a list, got {type(tags).__name__}",
        )
    seen: set[str] = set()
    for t in tags:
        if not isinstance(t, str) or not t:
            raise ModelValidationError(
                "invalid_tags",
                f"tags entries must be non-empty strings, got {t!r}",
            )
        seen.add(t)
    return sorted(seen)


async def _check_provider_registered_v2(conn, provider_id: str) -> None:
    """Pre-INSERT provider check — stricter than the DB FK alone.

    `models_provider_id_fk.sql` added a real
    `FOREIGN KEY (provider_id) REFERENCES model_providers (id)` (closing
    the trade-off `models_table.sql`'s header comment originally
    deferred), so an unregistered `provider_id` is now rejected by
    Postgres regardless of caller. This helper still earns its keep on
    two counts: it additionally requires `status = 'active'` (the FK
    alone would happily accept a *disabled* provider, which shouldn't
    gain new models), and it raises `ModelValidationError` with a clear
    message instead of a raw `asyncpg.ForeignKeyViolationError` bubbling
    up from the INSERT.
    """
    hit = await conn.fetchval(
        "SELECT 1 FROM model_providers "
        " WHERE id = $1 AND status = 'active' "
        " LIMIT 1",
        provider_id,
    )
    if hit is None:
        raise ModelValidationError(
            "provider_not_registered",
            f"provider_id={provider_id!r} does not resolve to an active "
            f"model_providers row — create the provider before adding "
            f"models under it",
            status=409,
        )
