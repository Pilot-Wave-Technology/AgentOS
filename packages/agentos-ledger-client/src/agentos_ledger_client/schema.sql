-- The ledger's own schema, shipped WITH the client.
--
-- WHY THIS FILE EXISTS. The package reads and writes exactly three tables
-- and, until now, created none of them -- so "install the client and call
-- it" was impossible outside agentos, which is the one thing the
-- extraction is for. Pointing a consumer at the control panel's migration
-- runner is not the answer either: that would drag in
-- `001_initial_schema.sql` and the whole control-plane schema, none of
-- which a consumer wants.
--
-- COPIED, NOT RE-DERIVED. Every statement below is lifted verbatim from
-- `migrations/001_initial_schema.sql` and `migrations/ledger_blobs.sql`,
-- with `IF NOT EXISTS` added so `init_schema` is idempotent. Two copies of
-- a schema drift, and for a hash-chained table the drift presents as a
-- chain that will not verify -- so
-- `tests/test_the_shipped_schema_matches_the_migrations.py` fails the build
-- if they stop agreeing. Change the migration first; this file follows.
--
-- NO EXTENSIONS REQUIRED. `gen_random_uuid()` has been in Postgres core
-- since 13, and nothing here needs anything else, so a bare database is
-- enough for a consumer's local tests.
--
-- NOT INCLUDED: the `agentos_worker` GRANT from `ledger_blobs.sql`. That
-- role belongs to the control plane's deployment; a consumer grants to
-- whatever role it runs as.

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id           UUID PRIMARY KEY,
    event_type         TEXT NOT NULL,

    -- Versioning (spec Section 3: envelope_version + event_version)
    envelope_version   INTEGER NOT NULL DEFAULT 1,
    event_version      INTEGER NOT NULL DEFAULT 1,

    -- Ordering
    timestamp          TIMESTAMPTZ NOT NULL DEFAULT now(),
    sequence_number    BIGSERIAL NOT NULL UNIQUE,

    -- Actor (spec Section 3: Actor Reference)
    actor_id           UUID NOT NULL,
    actor_type         TEXT NOT NULL DEFAULT 'human'
                       -- WIDENED TWICE SINCE 001, AND BOTH WERE MISSED HERE.
                       -- `014_goal_collaboration.sql` added 'goal_orchestrator';
                       -- `ledger_kernel_actors.sql` added 'executor' and
                       -- 'verifier'. This file was copied from 001 alone, so it
                       -- shipped the opening four. On a live deployment those
                       -- three account for 29% of all events -- a consumer that
                       -- bootstrapped from the narrow set would take a CHECK
                       -- violation on its first orchestrator event, long after
                       -- its tests had passed.
                       CHECK (actor_type IN ('human', 'shca', 'independent_agent',
                                             'system', 'goal_orchestrator',
                                             'executor', 'verifier')),
    on_behalf_of       UUID,

    -- Causality chain (spec Section 3)
    correlation_id     UUID,
    causation_ids      UUID[] NOT NULL DEFAULT '{}',

    -- Context (spec Section 3)
    context_task_id    TEXT NOT NULL DEFAULT '',
    context_goal_id    TEXT NOT NULL DEFAULT '',
    context_team_id    TEXT NOT NULL DEFAULT '',
    context_conversation_id TEXT NOT NULL DEFAULT '',

    -- Data classification (spec Section 3)
    classification     TEXT NOT NULL DEFAULT 'normal'
                       CHECK (classification IN ('normal', 'sensitive', 'restricted')),

    -- Integrity (spec Section 3: hash chaining)
    integrity_hash     TEXT NOT NULL,
    previous_hash      TEXT NOT NULL DEFAULT 'GENESIS',

    -- Payload + extensions (spec Section 2.2 + 3)
    payload            JSONB NOT NULL,
    extensions         JSONB NOT NULL DEFAULT '{}',

    -- Idempotency
    idempotency_key    TEXT
);

-- Partial unique index for idempotency (global, no stream)
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency ON ledger_events(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_ledger_type     ON ledger_events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_ledger_actor    ON ledger_events(actor_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ledger_corr     ON ledger_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_ledger_task     ON ledger_events(context_task_id) WHERE context_task_id != '';
CREATE INDEX IF NOT EXISTS idx_ledger_goal     ON ledger_events(context_goal_id) WHERE context_goal_id != '';
CREATE INDEX IF NOT EXISTS idx_ledger_sequence ON ledger_events(sequence_number);
CREATE INDEX IF NOT EXISTS idx_ledger_payload  ON ledger_events USING gin(payload);

CREATE TABLE IF NOT EXISTS chain_head (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id UUID NOT NULL REFERENCES ledger_events(event_id),
    last_hash     TEXT NOT NULL,
    event_count   BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- from migrations/ledger_blobs.sql
CREATE TABLE IF NOT EXISTS public.ledger_blobs (
    blob_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL,
    content_type TEXT NOT NULL,
    storage_backend TEXT NOT NULL CHECK (storage_backend IN ('inline', 'gcs')),
    data BYTEA,
    gcs_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
