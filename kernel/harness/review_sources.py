"""What a reviewer needs to know, read from the database.

NOT FROM A WORKSPACE, and that is the rule the last reviewer broke in
three places at once. The gateway story reviewer read `SCOPE.md` and its
cycle history through `shca_instance_id`, which only the gateway-agent
execution path ever writes — so for a pod-executed story those documents
came back empty, silently, and the reviewer judged a description against
nothing. Workspaces are pod-local and reaped; the database outlives
every substrate change.

EVERY QUERY HERE IS STORY-SCOPED, which sounds obvious and is the second
thing that was wrong. `park_read.answered_since` and
`intake.standing_steers` both exist and neither is usable here: they are
hot-path prompt builders, scoped to ONE execution and returning the
single most recent row. A story can have several executions — a reopen
starts another — and a review wants the history, including the steers
that were declined.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# A park answer or a steer is a sentence, not a document. Bounded so one
# very long answer cannot crowd out the diff the reviewer is here for.
MAX_ITEM_CHARS = 1200
MAX_ITEMS = 40


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"


async def story_row(conn, story_id: str):
    return await conn.fetchrow(
        "SELECT id, title, description, acceptance_criteria, goal_id, "
        "       story_kind, work_kind, created_at, completed_at "
        "FROM stories WHERE id = $1", story_id)


async def answered_questions(conn, story_id: str) -> list[dict]:
    """Every park a person answered, across every execution of this story.

    THE INPUT THAT MAKES A REVIEWER CONFIDENTLY WRONG when it is absent.
    When the model asked "X or Y" and a person said Y, that is a
    DECISION. A reviewer without it reads Y in the diff, judges it
    against a plan that said X, and reopens a settled question — with
    the authority of a review. Being merely incomplete is the better
    failure of the two.
    """
    rows = await conn.fetch(
        """
        SELECT q.kind, q.question_text, q.answer_text, q.answered_by,
               q.declined, q.answered_at
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE e.story_id = $1
           AND q.status = 'ANSWERED'
           AND q.kind <> 'authorization'
         ORDER BY q.answered_at NULLS LAST, q.id
         LIMIT $2
        """, story_id, MAX_ITEMS)
    return [{"kind": r["kind"], "asked": _clip(r["question_text"]),
             "answer": _clip(r["answer_text"]),
             "by": r["answered_by"] or "", "declined": bool(r["declined"])}
            for r in rows]


async def steers(conn, story_id: str) -> list[dict]:
    """Standing amendments, including the ones that were NOT accepted.

    `intake.standing_steers` returns only ACCEPTED steers because that is
    what a running leg must obey. A review wants the others too: a steer
    somebody raised and the run declined is a thing a person may want to
    know happened.
    """
    rows = await conn.fetch(
        """
        SELECT p.payload->>'instruction' AS text, p.actor_id, p.status
          FROM kernel.proposals p
          JOIN kernel.executions e ON e.id = p.execution_id
         WHERE e.story_id = $1 AND p.kind = 'steer'
         ORDER BY p.id
         LIMIT $2
        """, story_id, MAX_ITEMS)
    return [{"text": _clip(r["text"]), "by": r["actor_id"] or "",
             "status": r["status"]} for r in rows if r["text"]]


async def completion_claims(conn, story_id: str) -> list[dict]:
    """What the executor claimed, and what the adjudication said.

    BOTH HALVES. A claim without its verdict is half the story: a run
    that claimed, was refused, and claimed again is exactly the shape a
    reviewer should look at hardest, and the refusal names why.

    The four test counts and the baseline are PROSE inside these
    strings, not columns — the tool asks for "NUMBERS, NOT PROSE … AND
    COMPARE IT TO THE BASELINE", and what lands is whatever the model
    wrote. Reading them is the reviewer's job, which is why the prompt
    makes it a numbered step.
    """
    rows = await conn.fetch(
        """
        SELECT p.id, p.reason_set::text AS reason_set, p.created_at,
               v.passed, v.refusal::text AS refusal
          FROM kernel.proposals p
          JOIN kernel.executions e ON e.id = p.execution_id
          LEFT JOIN kernel.verdicts v ON v.proposal_id = p.id
         WHERE e.story_id = $1 AND p.kind = 'completion'
         ORDER BY p.id
        """, story_id)
    out = []
    for r in rows:
        try:
            reasons = [str(x[0]) if isinstance(x, list) and x else str(x)
                       for x in json.loads(r["reason_set"] or "[]")]
        except Exception:                                   # noqa: BLE001
            reasons = [str(r["reason_set"] or "")]
        out.append({"reasons": reasons, "passed": r["passed"],
                    "refusal": r["refusal"]})
    return out


async def knowledge(conn, story_id: str) -> list[dict]:
    """`techniques` / `learned` / `abandoned`, per cycle.

    NOT ON THE PROPOSAL. The completion tool takes these alongside
    `reason_set` and they land somewhere else entirely —
    `stories.knowledge`, written by `record_cycle` on a best-effort path
    that only runs when the self-check gate PASSES. So a story with
    refused claims has fewer knowledge entries than claims, and the gap
    is itself informative.
    """
    raw = await conn.fetchval(
        "SELECT COALESCE(knowledge, '[]')::text FROM stories WHERE id = $1",
        story_id)
    try:
        return json.loads(raw or "[]")
    except Exception:                                       # noqa: BLE001
        return []


async def branches(conn, story_id: str) -> list[dict]:
    """Every repository and branch this story touched."""
    rows = await conn.fetch(
        """
        SELECT r.owner, r.name, n.branch_name, n.state, n.trunk_branch
          FROM git_node_stories gns
          JOIN git_nodes n ON n.id = gns.git_node_id
          JOIN git_repos r ON r.id = n.git_repo_id
         WHERE gns.story_id = $1
         ORDER BY gns.seq
        """, story_id)
    return [{"repo": f"{r['owner']}/{r['name']}", "branch": r["branch_name"],
             "state": r["state"], "trunk": r["trunk_branch"] or ""}
            for r in rows]


async def goal_context(conn, goal_id: str) -> dict:
    """Where the goal is — WITHOUT GO's memory, which is unreachable.

    The gateway design listed "GO's accumulated knowledge" as an input.
    It is not retrievable from the database: the only reader is a
    gateway workspace call, `agent_workspace_files` is empty because the
    memory-v2 snapshot flags are off, and `memory_writes` keeps a
    200-character summary. These four are the substitutes that actually
    exist, and a section built from something unreachable would have
    rendered empty in exactly the way this whole rebuild is about.
    """
    row = await conn.fetchrow(
        "SELECT objective, brief FROM goals WHERE id = $1", goal_id)
    summary = await conn.fetchval(
        "SELECT summary FROM goal_summary WHERE goal_id = $1", goal_id)
    decisions = await conn.fetch(
        "SELECT decision, rationale FROM goal_decisions "
        "WHERE goal_id = $1 AND retired_at IS NULL ORDER BY id LIMIT $2",
        goal_id, MAX_ITEMS)
    siblings = await conn.fetch(
        "SELECT id, title, state, result_summary FROM stories "
        "WHERE goal_id = $1 AND state IN ('done', 'reviewed') "
        "ORDER BY created_at LIMIT $2", goal_id, MAX_ITEMS)
    return {
        "objective": _clip(row["objective"] if row else "", 2000),
        "brief": _clip(row["brief"] if row else "", 4000),
        "summary": _clip(summary or "", 4000),
        "decisions": [{"what": _clip(d["decision"]),
                       "why": _clip(d["rationale"] or "")} for d in decisions],
        "siblings": [{"story": s["id"], "title": s["title"],
                      "state": s["state"],
                      "delivered": _clip(s["result_summary"] or "", 600)}
                     for s in siblings],
    }

# ── what this story changed ──────────────────────────────────────────

# A CHANGED-FILE LIST, NOT A PATCH, AND NOT A TOOL.
#
# The reviewer has the whole workspace and `read`/`glob`/`grep`. Final
# state answers "is this correct?" better than a diff does -- whole
# files show the function a change sits in and its callers, which is the
# argument for reading them in the first place.
#
# So a diff earns exactly ONE thing: SCOPE. The claim says "0 existing
# files modified" and nothing in the final tree can confirm or refute
# that -- a story that quietly edited `app/operations.py` while writing
# a doc is invisible to `read`. Checking the executor's word is what an
# independent review is FOR.
#
# It was a tool (`story_diff`) and that was the mistake. Measured on
# execution 246: nineteen calls across three legs, most of them `show`,
# using a diff tool as a file reader because its base detection was
# wrong. A fact computed once in code that knows the trunk cannot be
# browsed with, cannot guess, and cannot be got wrong by the model.

MAX_DIFF_CHARS = 60_000


def _git(path: str, *args: str) -> str:
    import subprocess

    try:
        out = subprocess.run(["git", *args], cwd=path,
                             capture_output=True, timeout=60)
    except Exception:                                       # noqa: BLE001
        return ""
    return out.stdout.decode(errors="replace") if out.returncode == 0 else ""


async def changed_files(conn, story_id: str, workspace: str,
                        mounts) -> list[dict]:
    """Per repository: the trunk it merges into, and what moved.

    THE BASE COMES FROM THE DATABASE, not from guessing. `story_diff`
    tried `origin/main`, `origin/master`... and after the executor runs
    `sync_trunk` the merge-base with any of them sits BEFORE the trunk
    merge, so the diff showed the entire trunk instead of one file.
    `git_nodes.trunk_branch` is the answer and it is one query away.
    """
    import os

    # KEYED BY THE CHECKOUT, WHICH MAY CARRY THE LANE. A story writing
    # more than one lane of one repository gets one checkout per lane, at
    # `repos/<owner>__<name>@<lane>`; a single-lane story keeps the bare
    # name. This map was built with the bare name only, so on a two-lane
    # story every lookup missed, `trunk` came back empty, and the brief
    # told the reviewer "this repository has no recorded trunk" for BOTH
    # checkouts -- which is what story 40aec728 came back as, a
    # `code_not_reviewed` over code that was in fact fine.
    #
    # And the bare key alone could not have worked anyway: two lanes of
    # one repository write it twice, so the second silently overwrote the
    # first and one lane would have been diffed against the other's
    # trunk.
    #
    # Registering BOTH names rather than deciding which one applies: the
    # suffixed key is unique per lane and matches a lane-qualified
    # checkout, the bare key matches a single-lane one, and neither
    # restates the rule in `story_clone_layout` about WHEN a checkout is
    # suffixed -- so the two cannot drift.
    lanes = {}
    for r in await conn.fetch(
            "SELECT r.owner, r.name, n.trunk_branch "
            "FROM git_node_stories gns "
            "JOIN git_nodes n ON n.id = gns.git_node_id "
            "JOIN git_repos r ON r.id = n.git_repo_id "
            "WHERE gns.story_id = $1", story_id):
        base = f"repos/{r['owner']}__{r['name']}"
        trunk = r["trunk_branch"] or ""
        # `setdefault`, so a two-lane story's bare key is at least stable
        # rather than last-write-wins. Nothing reads it in that case --
        # both mounts are suffixed -- but a deterministic wrong answer is
        # easier to recognise than an arbitrary one.
        lanes.setdefault(base, trunk)
        if trunk:
            lanes[f"{base}@{trunk}"] = trunk

    out = []
    for m in (mounts or ()):
        if getattr(m, "binding", "") != "git":
            continue
        at = m.at or ""
        path = os.path.join(workspace, at) if at else workspace
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        trunk = lanes.get(at, "")
        # TWO DOTS, NOT THREE. `a...b` diffs from the merge-base, which
        # after a `sync_trunk` is before the merge and drags the whole
        # trunk in. `a..b` compares the two trees, so what comes back is
        # what this branch has that the trunk does not -- the story's
        # own work, whether or not it synced.
        ref = f"origin/{trunk}" if trunk else ""
        stat = _git(path, "diff", "--stat", f"{ref}..HEAD") if ref else ""
        patch = _git(path, "diff", f"{ref}..HEAD") if ref else ""
        out.append({
            "at": at or "<root>", "trunk": trunk,
            "stat": (stat or "").strip(),
            "patch": _clip(patch, MAX_DIFF_CHARS),
            "readable": bool(ref),
        })
    return out
