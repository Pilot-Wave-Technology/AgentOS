"""A cycle started after the first one has to take trunk in itself.

THE GAP THIS CLOSES. The catch-up sweep only offers a plan to a story at
`awaiting_merge`. Asking for more work moves the story OFF that state --
it is being worked again -- so a second cycle on a branch that is behind
trunk gets no offer, and nothing else would make it take trunk in. The
work lands on a stale base, and the first anyone knows is a conflict in
the pull request or, worse, a clean merge of code written against an API
that changed.

SO THE NEW CYCLE DOES IT AS PART OF ITS OWN SUBMIT (decision 6). The
extension is told to by `take_in_trunk` in `get-state`, and the submit
refuses until the commit being pushed actually CONTAINS the trunk sha.

ASKED OF GITHUB, NOT OF OUR OWN BOOKKEEPING. `git_nodes.last_synced_
trunk_sha` looks like the answer and is the wrong one twice over: the
ENGINE writes it, on a later tick, from its own compare -- so at submit
time it still says "behind", and gating on it would refuse the very
push that fixes it. And `git_repo_trunks.trunk_sha` is a detection
cache, not the live tip. The question "does this commit contain that
one" has exactly one authority.

NOT ON CYCLE 1. A first cycle has no catch-up route at all: the sweep
will not offer one to a story that is still being written, so refusing
it here would be a deadlock rather than a gate. A first cycle's base is
also the branch's cut point, which is as fresh as the story is.
"""

from __future__ import annotations

from app.services.lane_sync_state import BEHIND_TRUNK_SQL

import logging

_log = logging.getLogger(__name__)

# Which lanes count as behind: the same reading the catch-up sweep uses,
# minus the story-state conditions, because here we are asking about a
# story that is deliberately NOT at `awaiting_merge`.
BEHIND_LANES_SQL = f"""
    SELECT gr.owner, gr.name, gn.branch_name, gn.trunk_branch,
           t.trunk_sha
      FROM git_nodes gn
      JOIN git_repos gr ON gr.id = gn.git_repo_id
      JOIN git_node_stories gns ON gns.git_node_id = gn.id
      JOIN git_repo_trunks t ON t.git_repo_id = gn.git_repo_id
                            AND t.trunk_branch = gn.trunk_branch
     WHERE gns.story_id = $1
       AND gn.state NOT IN ('MERGED', 'ABANDONED')
       AND t.trunk_sha IS NOT NULL
       AND {BEHIND_TRUNK_SQL}
       AND t.trunk_sha_seen_at > gn.created_at
     ORDER BY gr.owner, gr.name, gn.branch_name
"""


async def behind_lanes(pool, story_id: str) -> list[dict]:
    rows = await pool.fetch(BEHIND_LANES_SQL, story_id)
    return [{"repo": f"{r['owner']}/{r['name']}", "owner": r["owner"],
             "name": r["name"], "branch": r["branch_name"],
             "lane": r["trunk_branch"], "sha": r["trunk_sha"]} for r in rows]


async def take_in_trunk(pool, story_id: str) -> bool:
    """Should the extension sync trunk before it pushes this cycle?

    ADVISORY, and the gate below is what enforces it. The flag exists so
    the person is not told about the requirement by a refusal -- the
    same reason `request_more_work` posts an instruction rather than
    letting the submit gate be the first anybody hears of the rule.
    """
    from app.services.local_cycles import current_cycle  # noqa: PLC0415

    if await current_cycle(pool, story_id) < 2:
        return False
    return bool(await behind_lanes(pool, story_id))


async def refusal(pool, story_id: str, commit_sha: str) -> dict | None:
    """Refuse a submit whose commit does not contain the trunk it is behind.

    FAILS OPEN ON A GITHUB FAILURE, deliberately. This is a staleness
    guard, not a security boundary: the pull request still exists and a
    person still merges it. An outage that blocked every local submit
    would be a worse failure than a cycle that landed on a base a few
    commits old.
    """
    if not commit_sha or not await take_in_trunk(pool, story_id):
        return None

    from app.services.git_graph import github_ops as gh  # noqa: PLC0415

    stale = []
    for lane in await behind_lanes(pool, story_id):
        try:
            token = await gh.mint_repo_token(lane["name"],
                                             {"contents": "read",
                                              "metadata": "read"})
            cmp = await gh.compare(token, lane["owner"], lane["name"],
                                   lane["sha"], commit_sha)
        except Exception:                                   # noqa: BLE001
            _log.exception("local trunk gate: could not compare %s for story %s",
                           lane["repo"], story_id)
            continue
        if (cmp or {}).get("behind_by", 0) > 0:
            stale.append(f"{lane['repo']} `{lane['lane']}` "
                         f"({str(lane['sha'])[:12]})")
    if not stale:
        return None
    return {
        "ok": False,
        "reason": "trunk_not_taken_in",
        "error": ("trunk has moved since this branch was cut and the commit "
                  "you are submitting does not contain it: "
                  + "; ".join(stale)
                  + ". Take trunk in, re-run the checks, and submit again."),
        "lanes": stale,
    }
