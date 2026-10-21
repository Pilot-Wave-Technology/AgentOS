"""Which trunk lane each writable mount of an execution merges from.

Extracted from `executor_tools.remote_sync` -- `sync_trunk`'s one piece
of real reasoning, and the piece that was wrong. Its own module so it
can be tested against rows directly, without an executor.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def lanes_for_mounts(conn, execution_id: int) -> dict:
    """`{mount.at: trunk lane}` for the repos THIS execution may write.

    WHAT THIS REPLACES, AND WHY IT BROKE. The lane map used to be built
    from a goal-wide query -- every `kind='branch' role='trunk'` row in
    the goal -- keyed by `repos/<owner>__<name>`. Three things go wrong
    with that, and all three are live on `next`:

      1. A LANE THE STORY DOES NOT HOLD still lands in the map, and
         `sync` is asked to merge into a directory the pod has never
         mounted. The pod answers that it cannot reach that path and
         the whole call fails as E_WORKSPACE_UNREACHABLE -- so one
         unrelated trunk lane anywhere in the goal disables `sync_trunk`
         for every story in it. Goal GOAL1 had three lanes and the pod
         one mount.

      2. A DELETED LANE counts, because the query never filtered on
         `status`. Afsheen's deleted `main` lane kept breaking syncs
         after it was gone.

      3. TWO LANES OF ONE REPOSITORY COLLAPSE, because the key has no
         lane in it: the dict comprehension kept whichever row came
         last and silently merged the wrong trunk into one of them. Six
         executions on `next` already have two mounts of one repo.

    Keying on `execution_mounts.at` fixes all three at once, because
    `at` is exactly what the pod mounted -- including the `…@<lane>`
    suffix `assign_subdirs` gives a second write lane.

    THE LANE RULE IS `lane_of` (app/services/story_clone_layout.py),
    restated here in SQL for the same reason `mounts.py` restates it: a
    kernel query and a control-plane resolver cannot share a function.
    role='trunk' means `branch_name` IS the lane; role='feature' means
    `base_branch` is the lane it merges into.

    EVERY WRITABLE GIT MOUNT GETS AN ENTRY OR IT IS AN ERROR. `op_sync`
    reports a writable mount with no lane as `no trunk lane for this
    mount`, so leaving one out on purpose invents a failure. A mount
    checked out ON its own lane is therefore included and not skipped:
    merging `origin/<lane>` into `<lane>` is a fast-forward, which is
    what that story wants anyway.
    """
    rows = await conn.fetch(
        """
        SELECT em.at, em.ref,
               gr.repo_owner, gr.repo_name,
               CASE WHEN gr.role = 'trunk' THEN gr.branch_name
                    ELSE gr.base_branch END AS lane
          FROM execution_mounts em
          JOIN goal_resources gr ON gr.id = em.resource_id
         WHERE em.execution_id = $1
           AND em.writable
           AND em.binding = 'git'
           AND gr.kind = 'branch'
           AND COALESCE(gr.status, 'active') = 'active'
         ORDER BY em.at
        """,
        execution_id)
    lanes: dict[str, str] = {}
    for r in rows:
        lane = (r["lane"] or "").strip()
        if not lane:
            continue
        lanes[r["at"]] = lane

    # THE BARE REPO GRANT STILL NEEDS A LANE. A story granted a
    # repository without a branch row -- the pre-lane shape, and still
    # what a hand-made grant produces -- has no `base_branch` to read,
    # so fall back to the goal's trunk lane FOR THAT REPOSITORY. Scoped
    # to repos this execution actually mounts, which is the whole point
    # of this function; the old code applied the same fallback to every
    # repo in the goal.
    bare = await conn.fetch(
        """
        SELECT em.at, em.ref, gr.repo_owner, gr.repo_name
          FROM execution_mounts em
          JOIN goal_resources gr ON gr.id = em.resource_id
         WHERE em.execution_id = $1 AND em.writable
           AND em.binding = 'git' AND gr.kind <> 'branch'
        """,
        execution_id)
    if not bare:
        return lanes
    defaults = await conn.fetch(
        """
        SELECT gr.repo_owner, gr.repo_name, gr.branch_name
          FROM goal_resources gr
          JOIN executions e ON e.goal_id = gr.goal_id
         WHERE e.id = $1 AND gr.kind = 'branch' AND gr.role = 'trunk'
           AND gr.branch_name IS NOT NULL
           AND COALESCE(gr.status, 'active') = 'active'
         ORDER BY gr.branch_name
        """,
        execution_id)
    # ONE LANE OR NONE -- a repository with several trunk lanes gives a
    # lane-less grant no answer, and guessing one would merge a story
    # into a trunk nobody chose for it. Left out of the map, which the
    # model reads as "this mount was not synced" rather than as a
    # silently wrong merge.
    by_repo: dict = {}
    for d in defaults:
        by_repo.setdefault((d["repo_owner"], d["repo_name"]),
                           set()).add(d["branch_name"])
    for r in bare:
        if r["at"] in lanes:
            continue
        found = by_repo.get((r["repo_owner"], r["repo_name"])) or set()
        if len(found) != 1:
            if found:
                logger.warning(
                    "execution %s: mount %r is a lane-less grant on a "
                    "repository with %d trunk lanes (%s) -- not synced, "
                    "because which one it merges into is not recorded",
                    execution_id, r["at"], len(found), ", ".join(sorted(found)))
            continue
        lanes[r["at"]] = next(iter(found))
    return lanes
