# Transitions

This package defines valid state transitions for each entity type in the system.

## Pattern

Each sub-module owns one entity and exports:
- `<ENTITY>_TRANSITIONS` — dict mapping `current_status → set of valid target statuses`
- `assert_<entity>_transition(current, target)` — raises `ValueError` if the transition is invalid

Call the assert function at the top of any status-change endpoint or service before writing
to the database. Nothing should update an entity's status without going through it.

## Entity: Tool (`tools.py`)

Three meaningful states — `enabled` / `disabled` / `deprecated`. Admin-reachable transitions:

```
enabled  ──→ disabled          (admin turns it off)
disabled ──→ enabled           (admin turns it back on)
deprecated ──→ enabled         (admin recovery)
```

`deprecated` is **system-owned**: only the catalog sweep sets it (via a raw UPDATE in
`catalog_sync`, which bypasses this map), and it auto-restores to `enabled` when the tool
reappears in the catalog. Admins can recover from it but cannot set it — so `deprecated`
reliably means "the system removed this" vs `disabled` = "an admin removed this" (sticky).

`beta` and `unavailable` are **retired** (folded into `enabled` / `deprecated` by
`retire_beta_unavailable_statuses.sql`); they are no longer produced. They remain in the
`tools_status_check` CHECK only because that constraint cannot shed values under the
additive-migration rule.

`TOOL_ACTIVE_STATUSES = {"enabled", "beta"}` — statuses visible in profile editors and
included in resolver output. `beta` is kept here (harmless, never produced) so the deployed
`tools_native_active_only` CHECK, which references it, stays consistent.

## Adding a new entity

1. Create `<entity>.py` with `<ENTITY>_TRANSITIONS`, `<ENTITY>_ACTIVE_STATUSES`, and `assert_<entity>_transition`.
2. Export from `__init__.py`.
3. Add a section here.
