"""Which admitted runs are stranded without the workspace they were promised.

ONE QUERY, ON ITS OWN, for two reasons.

It is the EXACT COMPLEMENT of the guard in `dispatch_pass`: that one refuses to
hand a model an execution whose mounts never materialised (`workspace_path IS
NULL` with git mounts recorded), and this one names the same rows so something
can do about it. Two predicates that must agree are worth one place to read them
side by side.

And it is a leaf. `kernel.worker.runner` imports the conductor, which imports the
model adapters, which import `anthropic` — so a test of this query, living there,
could only run where a model SDK is installed. The rule it protects has nothing to
do with models.

WHY THE RULE EXISTS. `dispatch_pass` prepared workspaces for PENDING rows only,
and admitted rows unconditionally in the same pass. An execution whose clone
failed was therefore admitted anyway, held back by the guard for exactly the
right reason, and never offered to the only loop that could have re-tried it.
Permanent, in a legitimate-looking state, behind a stall string nobody reads.

Observed on KER15: two stories admitted with `repos/` empty and still empty
hours later. The trigger was a 404 from the credential broker, fixed separately —
but fixing a trigger does not move a run that nothing will ask about again.
"""

from __future__ import annotations

# Mirrors `dispatch_pass`'s `ready` filter, negated on the workspace clause.
# Kept as one string so the two can be diffed by eye.
_STRANDED = """
    SELECT e.id, e.kind, e.domain, e.goal_id, e.state
      FROM executions e
     WHERE ($1::text IS NULL OR e.goal_id = $1)
       AND e.state = 'ADMITTED'
       AND e.container_id IS NULL
       AND e.workspace_path IS NULL
       AND EXISTS (SELECT 1 FROM execution_mounts m
                    WHERE m.execution_id = e.id)
     ORDER BY e.id LIMIT $2
"""


async def workspace_incomplete(conn, goal_id, limit: int) -> list[dict]:
    """Admitted runs holding mounts they never managed to establish.

    `container_id IS NULL` keeps a live run out: a sandbox already working has a
    workspace by definition, and re-acquiring underneath it is the one thing
    `GitDriver.acquire`'s adopt-don't-clobber rule exists to prevent.

    A run with NO mounts is deliberately absent. An errand establishes nothing,
    so a null `workspace_path` is not a failure for it — and the dispatch guard
    lets it through on the same reasoning. Including it here would retry every
    errand for ever instead of running it.
    """
    return [dict(r) for r in await conn.fetch(_STRANDED, goal_id, limit)]
