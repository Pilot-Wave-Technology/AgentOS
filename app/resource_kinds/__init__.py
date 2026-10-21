"""Resource-kind registry.

Single source of truth for `goal_resources.kind` values: what fields each
kind needs in `metadata`, what parent it requires (if any), and how to
validate. Used by:

  • `internal_register_resource` (server-side validation before INSERT)
  • `_create_gcp_resource_with_sas` (form-path validation, defence in depth)
  • Resource browser UI (rendering hints, icon dispatch)
  • Credential brokers (re-use of format constants like SA_EMAIL_RE)

Adding a new kind = adding one `ResourceKindSpec` to `RESOURCE_KINDS`.
No tool changes, no prompt changes, no UI changes.

GO currently learns kind requirements by reading the validation error
messages returned by `internal_register_resource`. Future work may add
gateway-level discovery tools (`search_resource_kinds`,
`resource_kind_schema`) that wrap `search_kinds()` and `get_schema()`
below — see `app/resource_kinds/README.md` for the rationale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# Format constants — kept here so brokers and the registry share them.
GCP_PROJECT_RE = r"^[a-z][a-z0-9\-]{4,28}[a-z0-9]$"
GCP_REGION_RE = r"^[a-z]+-[a-z]+\d+$"  # e.g. us-central1, europe-west2
SA_EMAIL_RE = (
    r"^[a-zA-Z][a-zA-Z0-9\-]+@[a-zA-Z0-9\-]+\.iam\.gserviceaccount\.com$"
)


@dataclass(frozen=True)
class FieldSpec:
    """One metadata field on a resource kind."""
    name: str
    description: str          # human-readable; used as GO's question prompt
    regex: Optional[str] = None  # if set, value must match
    required: bool = True


@dataclass(frozen=True)
class ResourceKindSpec:
    """Description of one `goal_resources.kind` value.

    Server-side validation reads this. GO reads a *minimal* projection of
    this via `list_resource_kinds`, and the full spec via
    `resource_kind_schema(kind)`.
    """
    kind: str
    description: str          # short, one-line — used in list and prompts
    metadata_fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    parent_kind: Optional[str] = None
    # Free-text plain-English description of any cross-field / parent
    # constraint that the broker's validate_registration enforces. Surfaced
    # to GO via the schema endpoint so it knows what to ask the user.
    parent_binding: Optional[str] = None
    # If True, the kind is human-only (e.g. repos must be added by humans
    # via the GitHub install flow, not by GO). register_resource
    # rejects it.
    human_only: bool = False
    # If True, GO must render a `cloud_sa_attach`-style confirmation card
    # rather than calling register_resource directly. Reserved for
    # the future card-based hallucination mitigation; defaults False until
    # we decide to flip it for credential-bearing kinds.
    requires_card_confirmation: bool = False


# ── Registry ─────────────────────────────────────────────────────────────

RESOURCE_KINDS: dict[str, ResourceKindSpec] = {
    # ── Git ────────────────────────────────────────────────────────────
    "repo": ResourceKindSpec(
        kind="repo",
        description="Git repository (human-added; requires GitHub App install).",
        human_only=True,
    ),
    "branch": ResourceKindSpec(
        kind="branch",
        description="A working branch of a git repository.",
        parent_kind="repo",
        # branch_name + repo_owner + repo_name + clone_url are stored in
        # top-level columns (not metadata) for historical reasons;
        # validation lives in goal_resources.py.
    ),

    # ── Documents / data ──────────────────────────────────────────────
    "document": ResourceKindSpec(
        kind="document",
        description="A document (PDF, markdown, etc.) attached to the goal.",
    ),
    "file": ResourceKindSpec(
        kind="file",
        description="A user-uploaded file stored in the resource bucket.",
    ),
    "dataset": ResourceKindSpec(
        kind="dataset",
        description="A BigQuery / data warehouse dataset.",
    ),

    # ── Misc ──────────────────────────────────────────────────────────
    "service": ResourceKindSpec(
        kind="service",
        description="A service or API endpoint the agent should know about.",
    ),
    "environment": ResourceKindSpec(
        kind="environment",
        description="A free-form environment description (legacy).",
    ),
    "config": ResourceKindSpec(
        kind="config",
        description="A configuration value or block (key/value pairs).",
    ),
    "other": ResourceKindSpec(
        kind="other",
        description="Catch-all for resources that don't fit another kind.",
    ),

    # ── GCP ───────────────────────────────────────────────────────────
    "gcp_environment": ResourceKindSpec(
        kind="gcp_environment",
        description="A GCP project + region context. Parent for gcp_*_sa kinds.",
        metadata_fields=(
            FieldSpec(
                "target_project_id",
                "Target GCP project ID (the customer's project, not the platform's)",
                regex=GCP_PROJECT_RE,
            ),
            FieldSpec(
                "target_region",
                "GCP region for the deliverable (e.g. us-central1)",
                regex=GCP_REGION_RE,
            ),
        ),
    ),
    "gcp_test_sa": ResourceKindSpec(
        kind="gcp_test_sa",
        description="Service account used by integration-tester. Attaches to a gcp_environment.",
        metadata_fields=(
            FieldSpec(
                "sa_email",
                "Service-account email (e.g. test-runner@<project>.iam.gserviceaccount.com)",
                regex=SA_EMAIL_RE,
            ),
        ),
        parent_kind="gcp_environment",
        parent_binding=(
            "sa_email's project portion (the part between '@' and "
            "'.iam.gserviceaccount.com') must equal parent's target_project_id"
        ),
    ),
    "gcp_deploy_sa": ResourceKindSpec(
        kind="gcp_deploy_sa",
        description="Service account used by deployment. Attaches to a gcp_environment.",
        metadata_fields=(
            FieldSpec(
                "sa_email",
                "Service-account email (e.g. deployer@<project>.iam.gserviceaccount.com)",
                regex=SA_EMAIL_RE,
            ),
        ),
        parent_kind="gcp_environment",
        parent_binding=(
            "sa_email's project portion must equal parent's target_project_id"
        ),
    ),
}


# ── Accessors ────────────────────────────────────────────────────────────

def get_spec(kind: str) -> Optional[ResourceKindSpec]:
    return RESOURCE_KINDS.get(kind)


def search_kinds(query: str, *, limit: int = 5) -> dict:
    """Targeted discovery for `search_resource_kinds`. Avoids dumping
    every kind into GO's context. Keyword/substring match against kind
    names + descriptions, returns the top `limit` matches.

    Returns:
      {
        "matches": [{kind, parent_kind, description}, ...],
        "hint":    "<operator-readable fallback message if no matches>"
      }

    GO's pattern: call this with a short context-derived query
    (e.g. "GCS service account", "deployment SA", "BigQuery dataset").
    Use the schema endpoint for whichever kind it picks. If `matches` is
    empty, surface the `hint` to the user instead of guessing.
    """
    q = (query or "").lower().strip()
    if not q:
        return {
            "matches": [],
            "hint": (
                "Search query was empty. Ask the user what kind of resource "
                "they need, or point them at the Resources panel to see what's "
                "already registered."
            ),
        }

    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    scored: list[tuple[int, ResourceKindSpec]] = []
    for spec in RESOURCE_KINDS.values():
        if spec.human_only:
            continue  # GO can't register these; don't suggest them
        haystack = f"{spec.kind} {spec.description}".lower()
        score = sum(1 for t in tokens if t in haystack)
        if score > 0:
            scored.append((score, spec))

    scored.sort(key=lambda pair: (-pair[0], pair[1].kind))
    matches = [
        {
            "kind": spec.kind,
            "parent_kind": spec.parent_kind,
            "description": spec.description,
        }
        for _, spec in scored[:limit]
    ]

    if not matches:
        return {
            "matches": [],
            "hint": (
                f"No resource kind matched {query!r}. Ask the user to clarify "
                f"what kind of resource they mean, or point them at the goal's "
                f"Resources panel (/goals/<id>/resources/browser) which lists "
                f"every supported kind directly — they can add one there if "
                f"this is a fresh setup."
            ),
        }
    return {"matches": matches, "hint": None}


def get_schema(kind: str) -> Optional[dict]:
    """Full schema for `resource_kind_schema(kind)`. Returns None if
    the kind is unknown."""
    spec = RESOURCE_KINDS.get(kind)
    if spec is None:
        return None
    return {
        "kind": spec.kind,
        "description": spec.description,
        "parent_kind": spec.parent_kind,
        "parent_binding": spec.parent_binding,
        "human_only": spec.human_only,
        "metadata_fields": [
            {
                "name": f.name,
                "description": f.description,
                "regex": f.regex,
                "required": f.required,
            }
            for f in spec.metadata_fields
        ],
    }


# ── Validation ───────────────────────────────────────────────────────────

def validate_registration(
    *,
    kind: str,
    metadata: dict,
    parent_metadata: Optional[dict] = None,
) -> Optional[str]:
    """Validate a registration request against the kind's spec.

    Returns:
      None  — all checks passed
      str   — error message (operator-readable; safe to surface to GO/agent)
    """
    spec = RESOURCE_KINDS.get(kind)
    if spec is None:
        return f"Unknown resource kind: {kind!r}"

    if spec.human_only:
        return (
            f"{kind!r} resources are human-only — they must be added through "
            f"the Resources UI / GitHub install flow, not via "
            f"register_resource."
        )

    if spec.parent_kind and parent_metadata is None:
        return (
            f"{kind!r} requires a parent of kind {spec.parent_kind!r} — "
            f"pass parent_id pointing to an existing {spec.parent_kind} resource."
        )

    for f in spec.metadata_fields:
        value = (metadata or {}).get(f.name)
        if not value:
            if f.required:
                return f"Missing required field {f.name!r} ({f.description})"
            continue
        if not isinstance(value, str):
            return f"Field {f.name!r} must be a string"
        if f.regex and not re.match(f.regex, value):
            return (
                f"Field {f.name!r} value {value!r} does not match expected "
                f"format ({f.description})"
            )

    # Per-kind cross-field / parent-binding checks.
    if kind in ("gcp_test_sa", "gcp_deploy_sa") and parent_metadata is not None:
        sa_email = (metadata or {}).get("sa_email", "")
        parent_project = (parent_metadata or {}).get("target_project_id", "")
        if "@" in sa_email and parent_project:
            sa_project = sa_email.split("@", 1)[1].split(".iam.", 1)[0]
            if sa_project != parent_project:
                return (
                    f"Service account project {sa_project!r} does not match "
                    f"parent environment's target_project_id "
                    f"{parent_project!r} — the SA must belong to the parent's "
                    f"project."
                )

    return None
