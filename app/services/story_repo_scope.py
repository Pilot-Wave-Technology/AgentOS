"""How many repositories a story targets, and whether it is claimed locally.

Sole owner of the two predicates behind the **multi-repo × local-execution
interlock** (see `research/LOCAL_EXEC_MULTIREPO_INTEGRATION.md`, decision D3).
Both are asked from two different enforcement points, so the SQL lives here once
rather than being copied into each:

  * `app/services/local_checkout.py`     — refuse a local checkout of a multi-repo story
  * `app/services/resource_allocation.py` — refuse a 2nd repo grant on a locally-claimed story

**Why the interlock exists.** Local execution resolves "which repo is this story
in" as a single value (`local_hydrate._repo_ref`, `ORDER BY updated_at DESC
LIMIT 1`), and that one resolver independently feeds the clone, the read token
and the push token. With N repos those calls can disagree, so a write token can
be minted for a repo the user never cloned. Landing then compounds it:
`diff_review.decide_diff_review` short-circuits on `is_local` and reports
`pushed = True` with no per-repo verification, so a one-repo submission approves
as though every repo landed. The failure is silent, which is why it is refused
rather than warned about.

**Why not fix it properly instead.** The multi-repo plan explicitly places the
local-execution path out of scope, multi-repo has never executed end to end, and
local execution is in production and ungated. Refusing the pairing is the
behaviour-preserving move; giving local execution a real N-repo contract is a
separate project (Stage 2 in the audit doc).

Nothing here writes. No new schema — both predicates read existing tables, so
the interlock adds no migration and no denormalised "which repo" column.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def count_story_repos(pool, story_id: str) -> int:
    """Number of DISTINCT repositories granted to this story.

    Counts `owner/name` pairs rather than `goal_resources` rows, because one repo
    is commonly granted twice — once as a `repo` and once as a `branch` resource —
    and that is a single repository, not two. Mirrors the identity expression the
    legacy one-repo-per-story rule uses in `resource_allocation`, so the two
    agree on what "a different repo" means.

    Returns 0 for a story with no repo grants; callers treat <= 1 as safe.
    """
    count = await pool.fetchval(
        """SELECT count(DISTINCT lower(coalesce(gr.repo_owner, '') || '/' || gr.repo_name))
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.status = 'active'
              AND gr.kind IN ('repo', 'branch')
              AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''""",
        story_id,
    )
    return int(count or 0)


async def granted_repo_scope(pool, story_id: str) -> list[dict]:
    """Every repository this story is GRANTED, and whether it may write to it.

    THE SAME ROW SHAPE as `story_workspace_layout.resolve_workspace_clones`
    (`repo_owner`, `repo_name`, `is_write`) so the credential broker consumes
    either source through one loop, and the two can never disagree about what
    the keys mean.

    WHY THIS EXISTS. The broker proved a story's repo scope from
    `story_workspace_nodes` — a record of what was cloned into the ASSISTANT's
    gateway workspace. That worked only while the assistant's workspace and the
    working tree were the same thing. They are not any more: a kernel assistant
    delegates, its workspace holds documents and no checkout
    (`assistant_kind.WORKSPACE_DOCS`), and the clone that actually happens is
    the EXECUTOR's, in its own sandbox. So provisioning correctly recorded no
    nodes, and the broker correctly refused a scope it could no longer prove —
    404 on every clone, for every code story the kernel assistant runs. Observed
    on KER15: two admitted runs, `repos/` empty, `stall_reason` = "clone
    failed", retrying for ever because nothing about it was transient.

    NOT A WIDENING. The grant IS the authorisation the endpoint's docstring
    asks for; the workspace-node table was only ever a derivative of it, one
    step further from the truth. Reading the grant directly proves the same
    boundary at its source, and a story with no grants still resolves to
    nothing — which is the genuinely unprovable case, and still refused.

    `bool_or` over the relations, so a repo held BOTH read-only and writable
    (GO grants `needs`, the git engine adds `produces` for the branch it cut)
    resolves to write. The alternative — first row wins — would mint a
    read-only token for a story that must push, and fail at `git push` long
    after anyone was watching.

    Identity is the case-folded `owner/name` pair, matching
    `count_story_repos`: one repository granted as both a `repo` and a `branch`
    resource is one repository. Write repos first, mirroring the resolver this
    stands in for.
    """
    rows = await pool.fetch(
        """SELECT min(coalesce(gr.repo_owner, '')) AS repo_owner,
                  min(gr.repo_name)                AS repo_name,
                  bool_or(sr.relation IN ('produces', 'modifies')) AS is_write
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND coalesce(gr.status, 'active') = 'active'
              AND gr.kind IN ('repo', 'branch')
              AND coalesce(gr.repo_name, '') <> ''
         GROUP BY lower(coalesce(gr.repo_owner, '')), lower(gr.repo_name)
         ORDER BY 3 DESC, 2""",
        story_id,
    )
    return [dict(r) for r in rows]


async def count_story_write_repos(pool, story_id: str) -> int:
    """Distinct repositories this story may WRITE to.

    The same shape as `count_story_repos`, restricted to write relations. Read-only
    (`needs`) context repos are excluded ON PURPOSE, and the distinction is not
    cosmetic: only a write repo ever gets a branch cut for it, so any comparison
    between "granted" and "has a live branch" must count the same population on both
    sides. Counting read-only grants against live branches makes the two permanently
    unequal, which reads as "still cutting" forever rather than "complete".

    An ABSENT relation counts as write, matching `is_write_relation` — it means the row
    came from the single-repo-goal fallback, which is the story's target repo.
    """
    count = await pool.fetchval(
        """SELECT count(DISTINCT lower(coalesce(gr.repo_owner, '') || '/' || gr.repo_name))
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.status = 'active'
              AND gr.kind IN ('repo', 'branch')
              AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
              AND (sr.relation IS NULL OR sr.relation = ''
                   OR lower(btrim(sr.relation)) IN ('produces', 'modifies'))""",
        story_id,
    )
    return int(count or 0)


async def has_live_local_checkout(pool, story_id: str) -> bool:
    """Is this story currently claimed by a local editor session?

    A checkout past its expiry is reclaimable and therefore NOT live — matching
    `local_checkout.reap_local_checkouts`, so a stale lock cannot permanently
    block a repo grant. Expiry is compared in SQL against `NOW()` to stay on the
    database clock, since the CP and the DB need not agree.
    """
    row = await pool.fetchrow(
        """SELECT checked_out_to_uid, checkout_expires_at
             FROM stories
            WHERE id = $1
              AND checked_out_to_uid IS NOT NULL
              AND (checkout_expires_at IS NULL OR checkout_expires_at > NOW())""",
        story_id,
    )
    if row is None:
        return False
    _log.debug(
        "story_repo_scope: story=%s has a live local checkout (uid=%s expires=%s)",
        story_id, row["checked_out_to_uid"], row["checkout_expires_at"],
    )
    return True


def multi_repo_local_refusal_message(repo_count: int) -> str:
    """The user-facing refusal, shared so both enforcement points say the same thing.

    Names the concrete consequence — one repo pushed, whole story reported landed
    — because "unsupported" alone reads as an arbitrary limit and invites a retry.
    Points at the two real ways forward, matching the guidance the legacy
    one-repo-per-story rule already gives.
    """
    return (
        f"This story targets {repo_count} repositories, and local execution works one "
        f"repository at a time. Running it locally would clone and push only one of them "
        f"while reporting the whole story as landed. Either work this story in the cloud, "
        f"or split it into one story per repository with a dependency between them."
    )
