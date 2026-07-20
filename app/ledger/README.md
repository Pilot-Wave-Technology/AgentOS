# app/ledger/

Append-only event store. Every state-mutating operation in AgentOS emits a ledger event before returning. The ledger is the authoritative record of what happened and when — other tables are derived from it, not the other way around.

---

## Design

Events form a SHA-256 hash chain: each event includes the hash of the previous event. This makes tampering detectable — a gap or reordered entry breaks the chain.

Each event has:

| Field | Type | Description |
|---|---|---|
| `event_type` | string | Dotted type identifier (e.g. `task.status_changed`) |
| `actor_id` | UUID | Who triggered the event (user, agent, or system) |
| `subject_id` | UUID | The primary entity the event is about |
| `subject_type` | string | Type of the subject (`task`, `goal`, `agent`, …) |
| `correlation_id` | UUID | Groups related events from the same operation |
| `causation_id` | UUID | The event that caused this one (for tracing chains) |
| `payload` | JSONB | Event-specific data |
| `previous_hash` | string | SHA-256 hash of the previous event |
| `hash` | string | SHA-256 hash of this event |

---

## Event types

Event types are dotted strings. New types require no schema change — just new string values.

Established types:

| Type | When emitted |
|---|---|
| `goal.created` | A goal is created |
| `goal.status_changed` | A goal transitions state |
| `story.created` | A story is added to a goal |
| `story.status_changed` | A story transitions state |
| `task.created` | A task is created |
| `task.status_changed` | A task transitions state |
| `task.completed` | A task reaches `done` |
| `task.failed` | A task reaches `failed` |
| `task.halted` | A task is force-killed |
| `iam.agent.provisioned` | An agent instance is created on the gateway |
| `iam.agent.destroyed` | An agent instance is removed from the gateway |
| `execution.paused` | A kill switch pause is applied |
| `execution.resumed` | A kill switch pause is lifted |
| `memory.saved` | A memory entry is written for an agent |

---

## Usage

Call `emit()` from anywhere in the application. Import from `app/ledger/emit.py`.

```python
from app.ledger.emit import emit
from app.ledger.schema import EventType

await emit(
    db=db,
    event_type=EventType.TASK_STATUS_CHANGED,
    actor_id=actor_id,
    subject_id=task_id,
    subject_type="task",
    correlation_id=correlation_id,
    payload={"from": old_status, "to": new_status},
)
```

`emit()` always writes to the database directly. It does not go through the task controller or any other intermediary.

---

## Actor provenance (`actors.py`)

Every event records **who acted** as two fields: `actor_id` (a UUID resolving to a
row in the `actors` registry) and `actor_type` (a text tag). The word "human" is a
*type tag* — the identity is always the real UUID (the specific user/agent/instance).

**Never hardcode a system UUID and never pass an unresolved/placeholder id.** Use a
resolver from `app/ledger/actors.py`, which returns `(actor_id, actor_type)` and
always falls back to the one **registered** system actor — never an orphan id:

| Resolver | Returns | Use for |
|---|---|---|
| `system_actor()` | `(SYSTEM_ACTOR_ID, "system")` | timers, cascades, the git engine, worktrees, memory flush |
| `human_actor(uid)` | the user's actor, `"human"` | browser/Slack/UI actions, human decisions |
| `agent_actor(agent_id)` | the agent-row actor + type | a GO (`go-<goal>`) or agent-template acting |
| `instance_actor(*, task_id/gateway_agent_id/instance_id)` | the instance actor + type | task/IA/RA work; on a task miss it falls back to the story's **SHCA instance** ("the assistant it runs on"), then system |
| `principal_actor(request)` | the calling agent from its JWT | agent-called internal endpoints |
| `request_actor(request)` | `(actor_id, actor_type, on_behalf_of)` | SHCA/IA internal endpoints — `on_behalf_of` is the instance owner (the human) |

`SYSTEM_ACTOR_ID` is defined **once** in `actors.py` (…0001) and imported everywhere
else. It is registered in `actors` by `migrations/ledger_system_actor.sql`; agent
instances are registered lazily by the resolvers and backfilled by
`migrations/backfill_instance_actors.sql`.

### Attribution model (decided 2026-07-15)

- **SHCA** phase/tool events → the SHCA **instance**, `on_behalf_of` the human owner.
- **IA / RA** task-flow → the **instance** (dispatch + planner-subtasks included).
- **GO** → the GO agent (`goal_orchestrator`).
- **Human** UI / board / Slack / approval decisions → the acting **user**.
- **Goal-review + Security councils** → *ledger-silent* by design: their provenance
  lives in `council_stages.instance_id` / `security_stages.instance_id` + `created_by`.
- **Platform** provisioning → the human who triggered it, else system (`human_actor(created_by_uid)`).
- **System** → timers, cascades, git engine, worktrees, memory flush, provisioning failures.

### Vocabulary + verification

The event `actor_type` is granular (`human`, `shca`, `independent_agent`,
`goal_orchestrator`, `system`) and must stay within the `ledger_events` CHECK. The
`actors` registry uses coarse types (`user`, `agent`, `system`). The
`ledger_events_resolved` **view** joins the two and flags problems:
`actor_registered = false` (dangling attribution) and `type_consistent = false`
(granular/coarse mismatch), plus `actor_display_name` / `on_behalf_of_display_name`.

Two guards keep this from regressing:
- **Emit-time audit** — `journal.append_event` logs a warning when an `actor_id`
  has no `actors` row (positive-cached, runs before the advisory lock; toggle with
  `LEDGER_ACTOR_AUDIT`).
- **CI guard** — `scripts/check_ledger_actors.sh` (a Cloud Build step) fails the
  build if app code reintroduces a hardcoded system UUID or an all-zero placeholder.

See `LEDGER_PROVENANCE_REVIEW.md` (repo root) for the full remediation history.

---

## Files

| File | Purpose |
|---|---|
| `journal.py` | Core hash-chaining logic — reads the last event hash, computes the new one, inserts |
| `emit.py` | `emit()` helper — wraps `journal.py` with a clean call signature |
| `actors.py` | Actor resolvers + the one canonical `SYSTEM_ACTOR_ID` (see above) |
| `schema.py` | `EventType` enum — the canonical list of known event type strings |
| `payloads/` | Payload builder functions, one per event type, for consistent payload shapes |

---

## Projection tables

Some tables are built entirely from ledger events rather than direct writes. `task_activity` is the primary example — it materialises a timeline of task state changes from `task.status_changed` events.

To query the history of a task, read `task_activity`. Do not reconstruct history by reading `tasks.status` — that column only holds the current state.

---

## Why append-only?

State machines have a known failure mode: the current-state column tells you where something is, but not how it got there, who moved it, or what else was happening at the same time. The ledger closes that gap.

Practical consequences:
- Debugging a stuck task: query the ledger by `subject_id` to see the full sequence of events.
- Auditing operator actions: filter by `actor_id` for a user to see every kill switch or approval they triggered.
- Diagnosing a cascade failure: use `causation_id` to follow the chain of events that led to multiple task failures.
