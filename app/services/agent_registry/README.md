# app/services/agent_registry

Canonical mutation path for agent lifecycle operations. New code must use the entry points below. Two older modules (`assistant_provisioning` and `go_agent`) write `agents` directly — they are sanctioned exceptions documented in `documentation/agent-registry/CONTRIBUTING.md`. All paths call `enroll_agent_principal` from `helpers.py` to write the actors, agent_configs, and agent_gateway_bindings rows consistently.

## Entry points

```python
from app.services.agent_registry import (
    provision_agent, update_agent, decommission_agent,
    soft_delete_agent, teardown_goal_agents,
)
```

| Function | Does |
|---|---|
| `provision_agent(pool, req, actor_id)` | Creates gateway workspace → writes DB (agent + actor + configs + bindings + files + ledger event) |
| `update_agent(pool, agent_id, patch, actor_id)` | Diffs against current → pushes delta to gateway → increments config_version → writes DB + ledger event |
| `decommission_agent(pool, agent_id, actor_id)` | Assistants: archive memory to GCS first; others: plain delete → soft-deletes DB row + removes binding + ledger event |
| `soft_delete_agent(pool, agent_id)` | DB-only soft-delete (ledger + agents row + binding). For paths that already handled the gateway side externally. |
| `teardown_goal_agents(pool, goal_id)` | Bulk goal teardown: destroy all agent_instances (Assistant archive, IA/RA plain), destroy GO, clean up agent_contexts and orphaned Assistant rows. Run as a background asyncio.Task after marking a goal deleted. |

## Shared write helpers (`helpers.py`)

`enroll_agent_principal(conn, *, actor_id, agent_id, ...)` writes the three subsidiary rows that every agents INSERT must be followed by: `actors`, `agent_configs` (v1 snapshot), and `agent_gateway_bindings`. All three use `ON CONFLICT DO NOTHING` — safe on idempotent re-provision paths. Call it inside an open transaction immediately after the agents INSERT.

`resolve_agent_tools(pool, agent_id, template_id, instance_allow, deny_list)` merges template profiles + instance overrides to produce the final (effective_allow, effective_deny) lists sent to the gateway. Called by both `provision_agent` and `update_agent` before every `agents.update(tools=...)` gateway call. Falls back to raw instance lists on tool_registry failure.

## Invariants this package enforces

1. **Gateway first, DB second.** The gateway workspace is the runtime; DB is the source of truth for config. On provision/update, gateway is called before the DB transaction commits — a gateway failure aborts cleanly. On delete, Assistant archive must succeed before DB is touched.
2. **Event-first within the DB transaction.** The ledger event is the first write inside each transaction. Projection tables (agents, agent_configs, agent_files, etc.) follow.
3. **config_version is the consistency marker.** Every mutation increments `agents.config_version`. Gateway sync records the version it synced as `agent_gateway_bindings.synced_config_version`. If they differ, the instance is out of date.

## Private helpers (`decommission.py`)

`_write_agent_deleted_rows(conn, agent_id, *, require_active)` is the shared two-write kernel inside both `soft_delete_agent` and `decommission_agent`: UPDATE agents SET status='deleted' + DELETE agent_gateway_bindings. Must run inside an open transaction. `require_active=True` guards the UPDATE with `AND status='active'` and returns a bool indicating whether the row changed.

## Adding new operations

Keep the same structure: gateway call → `append_event` inside a transaction → projection writes. Import from `app.openclaw.gateway_admin` for gateway calls, `app.ledger.journal.append_event` for events.

**Gateway injection:** all three entry points accept an optional `gateway=` keyword argument. Pass a real or mock `GatewayAdmin`-compatible object for tests; omit it in production and the module-global singleton is used.

```python
await provision_agent(pool, req, actor_id, gateway=fake_gateway)  # tests
await provision_agent(pool, req, actor_id)                         # production
```

**Critical:** `append_event` takes a `Connection`, not a `Pool`. Always acquire a connection and open a transaction first, then pass the connection — never pass the pool directly. The function uses `pg_advisory_xact_lock` which is transaction-scoped; passing a Pool breaks hash-chain serialisation.

Correct pattern (mirrors provision.py / update.py / decommission.py / model_change.py):
```python
async with pool.acquire() as conn:
    async with conn.transaction():
        await append_event(conn, event_type=..., payload=..., actor_id=...)
        await conn.execute("UPDATE ...", ...)   # projection writes
```

Full doc: `documentation/agent-registry/LIFECYCLE.md`
