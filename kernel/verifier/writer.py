"""F2 — recording what the Verifier found, as the Verifier.

The one place in the system that writes `criterion_results`, and it
raises the role to do it. F1 revoked INSERT from `agentos_worker`, so a
caller that forgets this gets `InsufficientPrivilegeError` rather than
quietly writing a verdict from the wrong side of the boundary.

`SET LOCAL ROLE` inside the transaction, so the elevation lasts exactly
as long as the write and cannot leak into whatever the connection does
next. That matters here more than usual: this runs on the worker's own
pool, whose connections are `agentos_worker` for everything else.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def record(conn, execution_id: int, claim_seq: int,
                 results: list, written_by: str = "verifier",
                 run_index: int = 0) -> int:
    """Write one row per criterion. Returns how many were written.

    Append-only: a re-verification of the same claim is a new
    `run_index`, never an update. F3's quorum reads exactly that.
    """
    written = 0
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE agentos_verifier")
        for criterion, outcome in results:
            # ONE ROW PER RUN, not one per criterion. F1 built
            # `run_index` and a unique index across it so this table
            # could hold the EVIDENCE while `verdicts` holds the
            # decision — and F3's quorum votes in memory, so without
            # writing each run the reason a verdict said `flake` would
            # be underivable an hour later.
            #
            # A criterion evaluated before the quorum existed, or one
            # that never ran, has no `_runs`: it records as itself, at
            # the index the caller asked for.
            runs = (outcome.detail or {}).get("_runs")
            if not runs:
                runs = [{"passes": outcome.passes,
                         **{k: v for k, v in (outcome.detail or {}).items()
                            if k != "_runs"}}]
                base = run_index
            else:
                base = 0
            for offset, observation in enumerate(runs):
                await conn.execute(
                    """
                    INSERT INTO criterion_results
                        (criterion_id, execution_id, claim_seq, passes,
                         detail, run_index, written_by)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                    -- REPLAYABLE. The unique index means "one result per
                    -- run of a criterion in a claim", and re-verifying
                    -- the SAME claim writes the same coordinates again.
                    -- Without this the insert raised and the adjudicator
                    -- died before writing any verdict -- so the retry hit
                    -- the rows the dead attempt left behind and died the
                    -- same way, for ever. A run was wedged in VERIFYING
                    -- by nothing worse than a worker restart landing
                    -- mid-adjudication.
                    --
                    -- DO NOTHING, not DO UPDATE: this table is APPEND-ONLY
                    -- evidence and agentos_verifier holds INSERT without
                    -- UPDATE on purpose -- a verifier that can rewrite its
                    -- own past observations is not evidence. Nothing is
                    -- lost by keeping the first: the verdict is computed
                    -- from the in-memory results, never re-read from here.
                    ON CONFLICT (criterion_id, execution_id, claim_seq,
                                 run_index) DO NOTHING
                    """,
                    criterion["id"], execution_id, claim_seq,
                    observation.get("passes", outcome.passes),
                    json.dumps({**observation, "settled": outcome.passes}),
                    base + offset, written_by)
                written += 1
    logger.info("verifier recorded %d result(s) for execution %s claim %s",
                written, execution_id, claim_seq)
    return written


async def load_contract(conn, execution_id: int):
    """The contract and its criteria, or (None, []) if there is none.

    F2b refuses to adjudicate a claim with no contract rather than
    inventing a default: an execution with no definition of done cannot
    be done, and a default would be this component deciding what the
    work was for.
    """
    row = await conn.fetchrow(
        "SELECT c.* FROM contracts c JOIN executions e ON e.contract_id = c.id "
        "WHERE e.id = $1", execution_id)
    if row is None:
        return None, []
    contract = dict(row)
    # asyncpg hands jsonb back as text. Decoded here, once, so every
    # reader downstream sees a dict — the services block and the secrets
    # list both live in workspace_spec, and two decoders is two chances
    # to disagree about whether it was decoded yet.
    for field in ("workspace_spec", "tool_allow", "budget"):
        raw = contract.get(field)
        if isinstance(raw, (str, bytes)):
            try:
                contract[field] = json.loads(raw)
            except (TypeError, ValueError):
                contract[field] = {}
    criteria = await conn.fetch(
        "SELECT id, kind, spec::text, held_out FROM criteria "
        "WHERE contract_id = $1 ORDER BY id", row["id"])
    return contract, [
        {"id": c["id"], "kind": c["kind"], "spec": json.loads(c["spec"]),
         "held_out": c["held_out"]}
        for c in criteria]
