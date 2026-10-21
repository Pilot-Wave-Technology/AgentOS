"""The evidence half of a story's record: a join, not a claim.

Everything here is already a row somewhere. Which branches the work ran
on, whether they reached trunk, what changed, what was produced, what the
story was granted, whether the checks passed — all of it exists, and none
of it needs a model to retype it.

WHY IT IS DERIVED RATHER THAN REPORTED. A model asked "which branches did
this land on" will answer, confidently, from whatever it remembers of a
conversation. The rows know. And the two halves sit in the same record
with different provenance — `observed` here, `unproven` for what the
executor said — so a planner reading it can tell a branch that merged
from a framework somebody claimed to use.

WHY IT RUNS AT THE REPORT. That is the moment the story is finished with
and the git engine has had its say. Running earlier would record a
landing that had not happened yet, which is the one thing this record
must never do — `done` is not `landed`, and the whole system is careful
about that distinction.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_PATHS = 60


async def derive_evidence(conn, goal_id: str, story_id: str) -> dict:
    """What can be known about this story's work without asking anyone.

    Never raises: the caller is a completion report, and a story must not
    fail to be reported because a join came back empty.
    """
    out: dict = {}
    try:
        nodes = await conn.fetch(
            """SELECT gn.branch_name, gn.state, gn.trunk_branch,
                      gn.pr_number, r.owner, r.name AS repo
                 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                 LEFT JOIN git_repos r ON r.id = gn.git_repo_id
                WHERE gns.story_id = $1""",
            story_id)
        if nodes:
            out["branches"] = [
                {"branch": n["branch_name"],
                 "repo": (f"{n['owner']}/{n['repo']}"
                          if n["owner"] and n["repo"] else None),
                 "state": n["state"],
                 "pr": n["pr_number"]}
                for n in nodes]
            # `landed_on` is the trunk of a node that actually MERGED.
            # Not "the trunk it targets" — that is an intention, and
            # reporting an intention as a landing is how done gets
            # announced as delivered.
            out["landed_on"] = sorted({
                n["trunk_branch"] for n in nodes
                if n["state"] == "MERGED" and n["trunk_branch"]})
    except Exception as exc:                                # noqa: BLE001
        logger.debug("evidence: git nodes unavailable for %s: %s", story_id, exc)

    try:
        repos = await conn.fetch(
            """SELECT repo_owner, repo_name, branch_name, subdir, is_write
                 FROM story_workspace_nodes WHERE story_id = $1""",
            story_id)
        if repos:
            out["repos"] = [
                {"repo": f"{r['repo_owner']}/{r['repo_name']}",
                 "branch": r["branch_name"], "writable": r["is_write"]}
                for r in repos]
    except Exception as exc:                                # noqa: BLE001
        logger.debug("evidence: workspace nodes unavailable for %s: %s", story_id, exc)

    try:
        grants = await conn.fetch(
            """SELECT gr.id, gr.kind, gr.name, sr.relation
                 FROM story_resources sr
                 JOIN goal_resources gr ON gr.id = sr.resource_id
                WHERE sr.story_id = $1""",
            story_id)
        if grants:
            # The retrieval key. "What do we know about the vendor
            # integration" is answered by findings from the stories that
            # held that resource — which is why this is recorded rather
            # than left to be re-joined: the grant can be revoked later
            # and the finding would stop being findable.
            out["resources"] = [
                {"id": g["id"], "kind": g["kind"], "name": g["name"],
                 "relation": g["relation"]} for g in grants]
    except Exception as exc:                                # noqa: BLE001
        logger.debug("evidence: grants unavailable for %s: %s", story_id, exc)

    try:
        checks = await conn.fetchrow(
            # RESULTS live in `criterion_results`, not `criteria`.
            # `criteria` is the contract's definition of done: it has no
            # `execution_id` to join on and no `passes` to count, so
            # this query raised on both and the except below swallowed
            # it -- `out["checks"]` has never been populated.
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE cr.passes = 'pass') AS passed
                 FROM criterion_results cr
                 JOIN executions e ON e.id = cr.execution_id
                WHERE e.story_id = $1""",
            story_id)
        if checks and checks["total"]:
            out["checks"] = {"total": checks["total"],
                             "passed": checks["passed"]}
    except Exception as exc:                                # noqa: BLE001
        logger.debug("evidence: checks unavailable for %s: %s", story_id, exc)

    try:
        arts = await conn.fetch(
            """SELECT DISTINCT sj.result_ref, sj.tool
                 FROM step_journal sj
                 JOIN executions e ON e.id = sj.execution_id
                WHERE e.story_id = $1 AND sj.result_ref IS NOT NULL
                LIMIT 20""",
            story_id)
        if arts:
            out["artifacts"] = [{"id": a["result_ref"], "tool": a["tool"]}
                                for a in arts]
    except Exception as exc:                                # noqa: BLE001
        logger.debug("evidence: artifacts unavailable for %s: %s", story_id, exc)

    return out
