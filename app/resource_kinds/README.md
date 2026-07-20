# `app/resource_kinds/` — resource-kind registry

Single source of truth for `goal_resources.kind` values. Every place
that needs to know "what is a `gcp_test_sa`?" reads from `RESOURCE_KINDS`
in `__init__.py`.

## Why this exists

Before this registry, each kind's metadata layout / validation / parent
constraints lived in scattered places: the dropdown UI form, the
`internal_register_resource` endpoint, the credential broker, GO's
prompt, Assistant's prompt, etc. Adding a new kind meant editing all of
those — easy to miss one, harder to keep them consistent.

The registry makes adding a new kind a **one-file change** for the
common case. Tools, endpoints, and prompts read structure from here at
runtime; they don't carry per-kind knowledge.

## Where the registry is read

| Caller | What it does with the spec |
|---|---|
| `app/routers/orchestrator.py:internal_register_resource` | Calls `validate_registration()` before inserting a row. Rejects unknown kinds, missing required fields, malformed values, project-binding violations. |
| `app/routers/goal_resources.py:_create_gcp_resource_with_sas` | Form-path equivalent: same `validate_registration()` call as the agent path, for defence in depth on operator typos. |
| `app/services/credential_brokers/gcp.py` (and future broker modules) | Use the same `SA_EMAIL_RE`, `GCP_PROJECT_RE` constants from this module. The broker handles credential lifecycle (provision/revoke); the registry handles structural validation. |
| `app/templates/resources/browser.html` and resource detail templates | Read `description` for tooltips, render parent_kind nesting (e.g. SAs under their env). |
| GO's prompt (`app/services/go_agent.py`) | Mentions a few kinds by name but no per-kind detail — GO learns required fields from the validation error messages this registry produces. |

## How to add a new kind

1. **Decide on a `kind` string.** Follow the naming convention:
   - Cloud kinds: `<provider>_<role>` (e.g. `gcp_test_sa`, `aws_deploy_role`).
   - Other kinds: short, lowercase, hyphen-free (e.g. `repo`, `branch`).
2. **Add a `ResourceKindSpec` entry** to `RESOURCE_KINDS` in `__init__.py`:

   ```python
   "aws_test_role": ResourceKindSpec(
       kind="aws_test_role",
       description="IAM role used by integration-tester. Attaches to an aws_environment.",
       metadata_fields=(
           FieldSpec("role_arn",    "Full IAM role ARN (arn:aws:iam::...)", regex=AWS_ROLE_ARN_RE),
           FieldSpec("external_id", "External ID for sts:AssumeRole",       regex=EXT_ID_RE),
       ),
       parent_kind="aws_environment",
       parent_binding="role_arn's account portion must equal parent's account_id",
   ),
   ```

3. **Add format constants** (e.g. `AWS_ROLE_ARN_RE`) at the top of `__init__.py`
   alongside the existing `GCP_PROJECT_RE`, `SA_EMAIL_RE`. Brokers can re-use these.
4. **Add a parent-binding check** (if needed) to `validate_registration()` —
   the existing GCP block at the bottom of that function is the template.
   Cross-field constraints that the regex can't express go here.
5. **Add a credential broker** if the kind carries minted credentials —
   see `app/services/credential_brokers/` for the protocol. Pure-data
   kinds (e.g. a bucket reference) don't need a broker.
6. **(Optional) Update the dropdown UI** — `app/templates/resources/browser.html`
   currently has hard-coded GCP form fields. If the new kind needs a form
   path for proactive operator setup, add a section. If it's only added
   via GO (which is fine — GO uses the validation error messages to learn
   the shape), the UI can stay as-is.

That's the change. No tool changes, no GO prompt rewrites, no per-kind
endpoints. Adding AWS = adding 3 entries here.

## Validation philosophy

Validation runs in **two places**, reading from the same registry:

- **Server-side at the endpoint** — `validate_registration()` is the
  authoritative gate. Rejects malformed registrations before any
  `INSERT`. Returns operator-readable error strings.
- **Re-checked from the form path** — `_create_gcp_resource_with_sas`
  calls the same validator. Defence in depth: catches operator typos
  the same way it catches agent hallucinations.

There is no client-side validation. Don't add it — the regex / parent-
binding rules are server-side truth, and the error message tells the
caller what's wrong.

## Anti-patterns to avoid

- **Don't hardcode kind knowledge anywhere outside the registry.** If
  you find yourself writing `if kind == "gcp_test_sa": ...` in a router
  or template, move that branch into the registry as a flag on
  `ResourceKindSpec` or a method on the corresponding broker.
- **Don't expose `RESOURCE_KINDS` directly to agents.** Agents discover
  kinds either through the validation errors they get back, or through
  the future `search_resource_kinds` / `resource_kind_schema`
  gateway tools when those land. The Python dict is for internal
  callers only.
- **Don't denormalise parent metadata onto children.** A `gcp_test_sa`
  metadata should store only `sa_email`, not `target_project_id` — the
  project comes from the parent env on demand. Denormalisation drifts
  if the parent is ever updated.

## Related docs

- `documentation/CREDENTIAL_BROKERING_PLAN.md` — broader credential-handling
  roadmap, including the hallucination defence layers and the deferred
  card-based confirmation pattern.
- `app/services/credential_brokers/README.md` — credential broker
  Protocol and how brokers integrate with this registry.
