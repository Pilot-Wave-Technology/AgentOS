"""Model registry service — DB-backed catalog + CRUD.

The v1 re-exports this file used to carry (`create_model`, `list_models`,
etc., writing to the frozen `model_registry`/`model_provider_registry`
"family A" tables) were removed 2026-08-06 after confirming zero live
callers anywhere in the app — not even their own test file used the
package-root import, it went through `management` directly. See
`documentation/MODEL_REGISTRY_CONTRACT.md` §0 for the family A history.

Use `app.services.model_registry.management`'s `*_v2` functions
(family B — `models`/`model_providers`) directly instead of importing
from this package root.
"""
