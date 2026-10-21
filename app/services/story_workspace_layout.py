"""Canonical READ side of `story_workspace_nodes` — where a story's repos are on disk.

The write side has one owner each (`operator_provisioning._record_story_workspace_nodes`
for the gateway, `local_workspace_nodes` for local execution). The read side had none:
six call sites each ran their own query and each did something different when there were
no rows — the resources doc wrote single-repo wording, `dag_ingest` skipped repo
validation, `diff_review` counted 0, `primary_write_subdir` returned `'repo'`,
`story_state` omitted the snapshot key, and `git_credential` raised **HTTP 404**.

That divergence was tolerable while an empty result was an incidental fallback. It is
not tolerable now that **absence is a meaningful flag**: no rows means "this workspace
was created before we recorded layouts, so it uses the legacy `repo/` directory and
finishes on the legacy path". A flag that six readers interpret six ways is not a flag.

This module is the one place that answers it, so the legacy branch is stated once
instead of re-derived. It is the read-side counterpart of the rule
`_clone_descriptors` already states for the write side:

    "ONE place computes the on-disk layout, because three consumers must agree on it
     exactly ... A disagreement between any two of them sends the assistant to a
     directory that isn't there."

Returns the ROWS rather than a bare boolean on purpose: five of the six readers want
the clone list anyway, and making them ask twice — once "is it legacy?", once "what are
the repos?" — is how the six interpretations drifted apart to begin with.

Read-only. Nothing here writes.
"""
from __future__ import annotations

import logging

import asyncpg

_log = logging.getLogger(__name__)


async def resolve_workspace_clones(
    pool, story_id: str, *, strict: bool = False,
) -> list[dict]:
    """Every repo recorded as cloned into this story's workspace.

    An EMPTY list means a legacy workspace — see the module docstring. Empty is an
    ANSWER, not an error; the one caller that refuses on empty (`git_credential`) does so
    for a security reason documented at its own site, not because empty is invalid.

    Ordering is `is_primary DESC` (the sticky, persisted decision — see
    `_record_story_workspace_nodes`) then write-repos-first, then `created_at` as a
    last-resort tiebreak. `is_primary` is load-bearing, not cosmetic: the primary
    write repo is what `primary_write_subdir` reports as the story's plan directory,
    so disagreeing about it silently relocates plans — which is exactly what happened
    before this fix (2026-08-20, §4.8 of the N-repo plan): first `subdir ASC`
    (alphabetical), then `created_at ASC`, were both tried as "the" order and both
    disagreed with each other and with what a genuinely earlier checkout had already
    decided, because a multi-write story's repos are created in ONE batched
    transaction and tie on `created_at` down to the microsecond — no sort key was
    ever going to be stable here. `is_primary` is decided exactly once (first
    provisioning call for a story) and preserved on every later call regardless of
    what order the caller's `clones` arrive in, closing the instability at its root
    instead of hoping a different sort key ties in the right direction.

    ``strict`` decides what an UNEXPECTED failure means, and callers genuinely differ:

      * ``strict=False`` (default) — degrade to the legacy answer. Right for readers whose
        legacy behaviour is harmless: the resources document, the state snapshot, the plan
        directory, DAG alias validation.
      * ``strict=True`` — re-raise. Required by `diff_review`, where quietly answering
        "one repo" would diff and commit only the primary repo while the human approves
        what they believe is the whole change, silently landing a partial multi-repo
        story. There, a visible error the human can retry is strictly better than a
        plausible wrong answer.

    A missing TABLE is never an unexpected failure — an un-migrated database legitimately
    has no layouts, and the legacy answer is correct for every caller including the strict
    ones. Only genuine faults (renamed column, type change, bug) are affected by
    ``strict``, and they log at `warning` when degrading: "silently degrade at debug
    level" is precisely how two NameErrors survived in production long enough to break
    every merge gate.
    """
    try:
        rows = await pool.fetch(
            """SELECT git_repo_id, branch_name, subdir, is_write, repo_owner, repo_name
                 FROM story_workspace_nodes
                WHERE story_id = $1
                ORDER BY is_primary DESC, is_write DESC, created_at ASC""",
            story_id,
        )
    except asyncpg.UndefinedTableError:
        _log.debug(
            "story_workspace_layout: story_workspace_nodes does not exist (story=%s) — "
            "treating as a legacy workspace", story_id,
        )
        return []
    except Exception:
        if strict:
            _log.exception(
                "story_workspace_layout: workspace-node read FAILED for story=%s and "
                "the caller cannot tolerate a guess — raising", story_id,
            )
            raise
        _log.warning(
            "story_workspace_layout: workspace-node read FAILED for story=%s — falling "
            "back to the legacy layout, which is WRONG if this story has recorded repos",
            story_id, exc_info=True,
        )
        return []
    return [dict(r) for r in rows]


def write_clones(clones: list[dict]) -> list[dict]:
    """Only the repos the story may COMMIT to.

    Read-only context clones ('needs' grants) get no branch, node or PR, so anything
    that diffs, commits, pushes or scopes a plan must exclude them — a task cannot be
    scoped to a clone it could never push from.
    """
    return [c for c in clones if c["is_write"]]


async def is_legacy_workspace(pool, story_id: str) -> bool:
    """True when this story predates workspace-layout recording.

    Derived from `resolve_workspace_clones` so the two can never disagree. Prefer
    calling the resolver directly when you also need the repos — this exists for the
    callers that genuinely only want the flag.
    """
    return not await resolve_workspace_clones(pool, story_id)


def to_bundle_repos(clones: list[dict]) -> list[dict]:
    """The clone list in the shape clients already understand.

    Byte-identical in structure to the gateway's `story_state` snapshot
    (`{name, path, branch, write}`), so a client can consume either source with one
    code path. `path` is the workspace-relative directory and is the whole point of
    publishing this: it lets the control plane own the layout instead of the client
    hardcoding a directory name it can never be talked out of.
    """
    return [
        {
            "name": f"{c['repo_owner']}/{c['repo_name']}".strip("/"),
            "path": c["subdir"],
            "branch": c["branch_name"],
            "write": bool(c["is_write"]),
        }
        for c in clones
    ]
