# agentos-ledger-client

Canonical AgentOS State Ledger client, hash-chain verification, and content-addressed blob storage.

## Features

- **Advisory-Locked Journal Appends**: Uses canonical `GLOBAL_CHAIN_LOCK_ID = 0x4C454447` to serialize concurrent writers and eliminate silent chain forks.
- **SHA-256 Hash Chaining**: Verifiable hash chain where `integrity_hash = sha256(previous_hash + payload_json)`.
- **Envelope Digest**: Secondary actor-bound digest `sha256(event_type + actor_id + actor_type + on_behalf_of + payload_json)` ensuring tamper-evident provenance.
- **Content-Addressed Blob Storage**: Tiered binary blob storage with deduplication:
  - Payloads $\le$ 1MB stored inline (`BYTEA`).
  - Payloads $>$ 1MB stored in Google Cloud Storage (`gs://...`).
- **Complete Schema Definitions**: Canonical event namespace constants for tasks, goals, iam, approval, and goal workload events.
- **Lightweight Dependencies**: Standard library + `asyncpg`. Optional GCS client support.

## Installation

```bash
pip install agentos-ledger-client
```

That is the whole thing. It is on PyPI, so there is no index to
configure, no credential to obtain, and nothing to do differently on a
laptop, in CI, or inside an AgentOS execution sandbox — PyPI is already
reachable from all three.

Blobs over 1MB need Google Cloud Storage; at or under 1MB they are
stored inline as BYTEA and nothing external is required:

```bash
pip install "agentos-ledger-client[gcs]"
```

**As a dependency**, declare it the ordinary way and pin the series:

```toml
dependencies = [ "agentos-ledger-client>=0.1,<0.2" ]
```

### Versions

Releases are cut automatically and only when the code changes. The
build asks PyPI for the highest `0.1.x`, compares that release's
contents with the tree, and publishes `0.1.<n+1>` only if they differ —
so a version number always means the same bytes, and a run of builds
that did not touch this package produces no releases at all.

`>=0.1,<0.2` therefore resolves to the newest build of the current
series. The series moves to `0.2` only for a breaking change, which is
the one judgement nobody automates.

Published by the maintainers' release job, from one branch alone. No
other build publishes, so a version cannot mean two different things
depending on where it was built.

**If you are changing this package** — person or agent — change the
code and stop there. Do not set `__version__`: the publish job
writes it, after reading what PyPI already has, and a hand-set number
either collides with one that is taken or is overwritten. A PyPI
version is burned forever, so a collision is not recoverable.

Raise it explicitly if you break the public API, since moving the
series is the one decision here that is not automatic; say so in the
pull request so the maintainers can move it.


## Quick Start

```python
import asyncpg
from agentos_ledger_client import (
    chain_hash,
    GLOBAL_CHAIN_LOCK_ID,
    append_event,
    store_blob,
    read_blob,
)


async def example(conn: asyncpg.Connection):
    # Store blob
    ref = await store_blob(conn, b"example payload", content_type="text/plain")
    print(ref.sha256, ref.storage_backend)

    # Append ledger event
    res = await append_event(
        conn,
        event_type="task.created",
        payload={"title": "Test task", "blob_sha256": ref.sha256},
        actor_id=my_actor_id,
        actor_type="independent_agent",   # see the table below
    )
    print(res["event_id"], res["integrity_hash"])
```

### `actor_type`

The `CHECK` constraint admits seven values. Every event says who caused
it, and "who" is the question the chain exists to answer — an event
attributed to the wrong kind of actor is not a cosmetic error.

| value | the actor |
| --- | --- |
| `human` | a person, acting directly |
| `shca` | a person's assistant, acting for them |
| `goal_orchestrator` | the orchestrator that plans and drives a goal |
| `executor` | a sandboxed run doing the work |
| `verifier` | the checker that judges a run's criteria |
| `independent_agent` | an agent acting on its own account |
| `system` | no actor — scheduled work, reapers, migrations |

Reach for `system` last. It is the value that means *nobody is
accountable for this*, and it is easy to default to when the real actor
is simply inconvenient to thread through. On a live deployment the
distribution is roughly `goal_orchestrator` 28%, `system` 28%, `human`
23%, `shca` 16%, the rest a long tail — and some of that `system` share
is events whose real actor was known and dropped.

This README previously listed only the first two and the last two,
which was the state of the constraint when the package was extracted
and has been wrong since. `goal_orchestrator`, the most common value of
all, was missing from the documentation while being the most written to
the table.


`actor_id` is `UUID NOT NULL` and `actor_type` is constrained to the four
values above — a consumer whose actors are strings (`"ecommerce-sim"`)
needs a stable UUID per actor and must pick one of the four types.

## Creating the tables

```python
from agentos_ledger_client import init_schema

await init_schema(conn)   # idempotent; safe in a test fixture

```

## Reading events and chain verification

```python
from agentos_ledger_client import read_events, verify_chain

# Stream events in order oldest first by sequence_number
async for event in read_events(conn, event_type="task.created", limit=100):
    print(event.sequence_number, event.timestamp, event.payload)

# Verify chain hash integrity and linkage from genesis or a starting point
verification = await verify_chain(conn)
if not verification.ok:
    print(
        f"Chain broken at event {verification.bad_event_id}: "
        f"expected {verification.expected_hash}, got {verification.actual_hash}"
    )
```

## Non-finite float serialization (NaN and Infinity)

Postgres `jsonb` columns reject non-standard JSON literals (`NaN`, `Infinity`, `-Infinity`). To support reinforcement learning and numerical workloads without losing divergence signals or corrupting hash chains:

- `payload_json` recursively encodes non-finite floats into the canonical string representations `"NaN"`, `"Infinity"`, and `"-Infinity"` across nested dictionaries and lists.
- Other types (e.g., `datetime`, `UUID`, `Decimal`) remain untouched and continue to be stringified by `default=str`, preserving byte-identity for all previously stored events and hashes.
- Python's standard `float()` accepts these exact strings back (e.g. `float(event.payload["reward"])` reconstructs `float('nan')` or `float('inf')`).
- **Accepted Trade-off**: A payload field containing the literal string `"NaN"` is indistinguishable on read from an encoded non-finite float. This is an intentional design choice: a sentinel object wrapper would disambiguate but would force every consumer to unpack scalar fields from dictionary envelopes.

## Checking it against your own database

The suite that verifies this client ships *inside* it, so you can run it
where it matters — against the Postgres you intend to use, on your
Python, without cloning the AgentOS repository.

```bash
pip install "agentos-ledger-client[test]"
pytest --pyargs agentos_ledger_client
```

**No database setup is required.** The `[test]` extra brings `pgserver`,
a real PostgreSQL shipped as a wheel, and the fixtures start one and
tear it down. To point the suite at a server you already have, set
`DATABASE_URL` instead — it is used when reachable and ignored when not,
so a stale value degrades to a skip rather than a confusing error.

**Why bother.** What you share with AgentOS is a *hash-chained* table. A
schema disagreement does not announce itself as "the schema differs" —
it surfaces later as a chain that will not verify, by which point the
rows exist. This suite builds the schema from the shipped `schema.sql`
on an empty database, appends a chained event, and verifies it.

It is not a hypothetical check. `schema.sql` shipped a `CHECK` on
`actor_type` that refused `goal_orchestrator`, `executor` and
`verifier` — 29% of the events on a live deployment — because it was
copied from the first migration and two later ones widened it. AgentOS
never noticed: its own tables come from `migrations/`, not from this
file. The first person to execute it would have been a consumer.

Requires Python 3.12: `pgserver` publishes cp312 wheels and no sdist.
The library itself has no such constraint at runtime, but the project
declares 3.12 because that is the only version it is tested on.
