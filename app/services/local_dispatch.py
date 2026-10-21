"""Local Execution Mode — next-phase selection (M4).

The gateway's `phase_dispatcher.find_next_dispatchable_task` deliberately EXCLUDES
local/checked-out stories, so the extension's phase walker drives the DAG itself.
This mirrors that selection + CAS-claim for ONE story, and reuses the UNCHANGED
`build_dispatch_message` so a local phase prompt is byte-identical to a gateway one
(same phase role, same sentinel mandate). Status reporting flows back through
`/internal/local/report-phase` → the shared sentinel machinery.
"""
from __future__ import annotations

from typing import Optional

from app.database import get_pool
from app.services import claude_phases as cp_svc
from app.services.phase_dispatcher import build_dispatch_message


# THE GATEWAY'S OWN NUMBERS. `agent_os_openclaw`'s
# `DEFAULT_MEMORY_FILE_MAX_CHARS` / `_TARGET_CHARS`, restated here because
# the control plane cannot import the gateway -- and pinned by a test that
# reads the gateway source, so the restatement cannot quietly drift.
MEMORY_FILE_MAX_CHARS = 30000
MEMORY_FILE_TARGET_CHARS = 24000


async def _landing(pool, story_id: str) -> dict:
    """The lanes this story lands on, and whether they are all in.

    EVERY LANE, INCLUDING THE MERGED ONES. The panel shows a row per
    pull request and a person wants to see the ones already in as much
    as the ones outstanding -- a list that empties as they merge reads
    as work disappearing rather than work finishing.

    `all_merged` is false for a story with NO lanes, deliberately: it
    means "every lane is in", and a story with nothing to land has not
    landed anything. Mark Done is not gated on it (a docs story must
    still finish) -- `story_done_blocker` is the gate, and it has the
    no-nodes case right.
    """
    rows = await pool.fetch(
        "SELECT gn.branch_name, gn.trunk_branch, gn.state, gn.pr_number, "
        "       gn.last_error, r.owner, r.name "
        "  FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        "  JOIN git_repos r ON r.id = gn.git_repo_id "
        " WHERE gns.story_id = $1 ORDER BY r.owner, r.name, gn.branch_name",
        story_id)
    from app.services.local_integration_targets import (  # noqa: PLC0415
        lane_mounts, mount_for)

    mounts = await lane_mounts(pool, story_id)
    prs = [{
        "repo": f"{r['owner']}/{r['name']}",
        "lane": r["trunk_branch"] or "",
        # WHERE THIS LANE LIVES IN THE WORKSPACE. The boundary hook's
        # `integrating` rule allows writes in the lanes being merged and
        # refuses them elsewhere, and it may not make a network call --
        # it runs on every write. So the mount rides in the snapshot the
        # hook already caches.
        #
        # READ FROM THE RECORDED CLONE, because two lanes of one repo
        # are two directories: deriving it from the repo name gave both
        # rows the same mount, and the hook then allowed writes in one
        # clone for work belonging to the other.
        "mount": mount_for(mounts, f"{r['owner']}/{r['name']}",
                           r["trunk_branch"] or ""),
        "branch": r["branch_name"],
        "number": r["pr_number"],
        # The URL is composed rather than stored: `git_nodes` keeps the
        # number, and a PR's url is a function of the repo and it.
        "url": (f"https://github.com/{r['owner']}/{r['name']}/pull/{r['pr_number']}"
                if r["pr_number"] else ""),
        "state": r["state"],
        # CLOSED IS NOT OPEN. Without this the panel says "merge it on
        # GitHub" about a pull request somebody has already closed.
        "pr_closed": (r["last_error"] or "").startswith("PR closed unmerged"),
    } for r in rows]
    live = [p for p in prs if p["state"] not in ("MERGED", "ABANDONED")]
    return {"prs": prs, "all_merged": bool(prs) and not live,
            "waiting_on": [p["branch"] for p in live]}


async def _integration(pool, story_id: str, story) -> dict:
    """The lanes to merge and the commit to merge, or an empty block.

    ONLY WHILE `integrating`. Outside that phase there is nothing to
    take in, and a non-empty block would invite the extension to merge
    trunk into a branch somebody is still writing.
    """
    if (story["current_phase"] or "") != "integrating":
        return {"lanes": []}
    from app.services.local_integration_targets import (  # noqa: PLC0415
        lane_mounts, mount_for)
    from app.services.local_trunk_gate import behind_lanes  # noqa: PLC0415

    mounts = await lane_mounts(pool, story_id)
    return {"lanes": [{
        "repo": lane["repo"],
        "lane": lane["lane"],
        "branch": lane["branch"],
        "sha": lane["sha"],
        # PER LANE, not per repo. Two lanes of one repo are two clones
        # in two directories, and naming one of them twice tells the
        # extension to merge both trunks into the same working tree.
        "mount": mount_for(mounts, lane["repo"], lane["lane"]),
    } for lane in await behind_lanes(pool, story_id)]}


async def _take_in_trunk(pool, story_id: str) -> bool:
    from app.services.local_trunk_gate import take_in_trunk  # noqa: PLC0415
    return await take_in_trunk(pool, story_id)


async def _cycle_state(pool, story_id: str) -> dict:
    """`local_cycles.cycle_state`, reached through the module that owns it."""
    from app.services.local_cycles import cycle_state  # noqa: PLC0415
    return await cycle_state(pool, story_id)


async def story_done_blocker(story_id: str) -> Optional[str]:
    """A reason a local story may not be marked done, or None.

    ONE CONDITION NOW: an approved plan. What went was the DAG half -- every
    `claude-code-phase` task `done`, none failed, the parent in a completion
    state -- and going is the point rather than a casualty.

    THIS IS A DELIBERATE LOOSENING, SO IT IS WORTH NAMING. That clause was
    the only thing standing between a local story and being marked done with
    nothing executed. A pod has a self-check against the plan's own checks
    before it may claim completion; a local run has no equivalent and is not
    getting one, because the person IS the check -- they are self-operating
    the story in their own editor, they can see the tree, and "done" is their
    click. Keeping a graph-shaped gate over a lane that dispatches no graph
    would be enforcing the shape of the old mechanism rather than anything
    anybody wanted.

    THE PLAN REQUIREMENT STAYS, and for a different reason than gating: a
    story marked done with no plan leaves nothing on the board saying what
    was meant to happen, and the plan is most of what the GO reads
    afterwards.

    AND AN ERRAND IS GATED BY NOTHING AT ALL. Abhishek, 2026-10-14: "for an
    errand Mark done can be available all the time but the user can choose
    to do more work." Both gates below are wrong for one. It has no plan --
    nothing in the local lane writes one for an errand and the plan card is
    not even offered -- so the plan gate parks every errand permanently on a
    condition it cannot meet. And its branch never lands, so the merge gate
    would park it too. The errand's own exit is the person deciding they
    have their answer; if they have not, they ask for more work instead of
    clicking done, and that is a choice rather than a gate.
    """
    pool = get_pool()
    from app.services.local_diff import _is_errand  # noqa: PLC0415
    if await _is_errand(pool, story_id):
        return None

    has_plan = await pool.fetchval(
        "SELECT 1 FROM plan_versions WHERE story_id = $1 "
        "AND status IN ('approved', 'executed') LIMIT 1",
        story_id,
    )
    if not has_plan:
        return ("no approved plan — write a plan and approve it before "
                "marking the story done.")

    # AND THE WORK HAS TO HAVE LANDED. `done` means the story is finished,
    # and a story whose branch is still open is not: the pull request can
    # still be closed, the lane can still be abandoned, and a person reading
    # `done` on the board would be reading something untrue.
    #
    # THE CLOUD LANE ASKS THIS EXACT QUESTION, so it is imported rather than
    # re-derived. `has_unlanded_work` already carries two answers that are
    # not obvious and would be re-earned badly here: a story with NO nodes
    # (a docs story, an abandoned one) has nothing to land and must finish
    # normally, and an ERRAND has a branch that never lands -- `reconcile`
    # cuts a workstream for one like any other story. The errand case is
    # answered above as well, and deliberately twice: this function returns
    # early for one, and would still be right if that line were ever lost.
    # AND A CYCLE THAT WAS AGREED AND NEVER DELIVERED.
    #
    # THE MERGE GATE BELOW READS GIT NODES, and an unpushed cycle never
    # touches one. On 9519fc57 cycle 2 was approved, worked, and its
    # submit refused by the checks -- so the README work sat committed
    # in the clone, unpushed, while the node still said MERGED from
    # cycle 1. Mark Done sailed through and the work would have gone
    # with the checkout.
    #
    # An APPROVED, UNSPENT plan is the record of exactly that: work a
    # person agreed to that never reached a submit. `spend_cycle` marks
    # it `executed` the moment it is delivered, so this asks the
    # question the nodes cannot.
    # ONLY FOR A STORY THAT HAS A BRANCH, and that is the trap avoided.
    # A story with no git node -- a docs story, an investigation -- never
    # submits at all: there is nothing to push, so its cycle is never
    # spent, and an unconditional rule here would block Mark Done on it
    # for ever. The same shape as `has_unlanded_work`'s no-nodes case,
    # which exists for the same story.
    from app.services.local_cycles import cycle_is_open  # noqa: PLC0415
    has_branch = await pool.fetchval(
        "SELECT 1 FROM git_node_stories WHERE story_id = $1 LIMIT 1", story_id)
    if has_branch and await cycle_is_open(pool, story_id):
        return ("there is an approved plan that has not been submitted — its "
                "work is agreed and not delivered. Submit it, or ask for the "
                "plan to be withdrawn, before marking the story done.")

    from app.services.story_landing import has_unlanded_work  # noqa: PLC0415
    async with pool.acquire() as conn:
        if await has_unlanded_work(conn, story_id):
            open_lanes = await conn.fetch(
                "SELECT gn.branch_name, gn.pr_number, gn.state, gn.last_error "
                "  FROM git_node_stories gns "
                "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
                " WHERE gns.story_id = $1 "
                "   AND gn.state NOT IN ('MERGED', 'ABANDONED') "
                " ORDER BY gn.branch_name", story_id)
            # A CLOSED PULL REQUEST IS NOT ONE WAITING TO BE MERGED, and
            # telling somebody to "merge the pull request first" about one
            # that a person deliberately closed sends them to GitHub to
            # look for a button that is not there.
            closed = [r for r in open_lanes
                      if (r["last_error"] or "").startswith("PR closed unmerged")]
            if closed and len(closed) == len(open_lanes):
                names = ", ".join(r["branch_name"] for r in closed)
                return ("the pull request was closed without being merged "
                        f"({names}), so this work has not landed. Ask for "
                        "more work to carry on — the same branch is used and "
                        "the next submit opens a fresh pull request.")
            named = ", ".join(
                f"{r['branch_name']}"
                + (f" (PR #{r['pr_number']})" if r["pr_number"] else " (no PR yet)")
                for r in open_lanes) or "its branch"
            return ("the work has not landed yet — merge the pull request "
                    f"first: {named}. Marking it done now would say the story "
                    "is finished while its branch is still open.")
    return None


# ONE ACTION PER PHASE, and `executing` is why this had to change.
#
# `executing` mapped to "complete" -- a DAG-era safety net, reached only when
# every task was terminal. There is no DAG: `approve_local_plan` sets
# `executing` the moment a person approves, so the very next snapshot told the
# session "complete" and the kickoff then asked it to summarise work it had
# not started. The one transition that should say GO said STOP.
#
# `awaiting_review` and `awaiting_next` also both said "complete", which is why
# a hint existed to tell them apart -- disambiguating in prose what the value
# itself could have carried. They are distinct now and the hints explain rather
# than correct.
_LIFECYCLE_ACTION = {
    "discovery": "discovery",
    "planning": "planning",
    "awaiting_plan_approval": "awaiting_approval",  # plan parked; human deciding
    "executing": "execute",                         # plan approved; do the work
    "submitting": "submitting",                     # claim in; the extension pushes
    "awaiting_merge": "awaiting_merge",             # pushed, PR open; human merges
    "awaiting_closeout": "awaiting_closeout",       # all merged; human marks done
    # WORK, NOT WAIT. `integrating` used to mean "the extension is taking
    # the trunk in"; it now means "a catch-up plan is approved and you
    # are working it", which is the same action as `executing` and must
    # read as such -- an agent that is told `integrating` and left to
    # infer stands by, which is exactly what it did.
    "integrating": "execute",                       # catch-up plan approved
}


def _lifecycle_action(current_phase: str | None) -> str:
    return _LIFECYCLE_ACTION.get((current_phase or "").strip(), "planning")


# Human-readable disambiguation carried in the LIVE snapshot (injected each turn).
# awaiting_review and awaiting_next BOTH surface next_action='complete', so the
# agent can't tell them apart from next_action alone — this line does, and it
# reaches even existing checkouts (whose baked prompt files can't be updated).
_PHASE_HINT = {
    "discovery": (
        "Scope first. Ask the person what this should do and where it belongs, "
        "then plan. You have not been approved to write code yet."
    ),
    "planning": (
        "Plan, do not build. Work out the steps and the checks that will show "
        "it worked, then register them with `register_plan` and tell the person "
        "it is ready. A boundary hook refuses source edits until they approve."
    ),
    "awaiting_plan_approval": (
        "Your plan is registered and the person is deciding. Do not start "
        "work. You MAY change the plan while they are deciding: if they ask "
        "for something different, call `register_plan` again with the whole "
        "revised plan and it replaces the one on the card -- same cycle, same "
        "plan file, no rejection needed. Do not re-register to say the same "
        "thing twice."
    ),
    "executing": (
        "The plan is APPROVED: do the work. Commit coherent changes as you "
        "go with `story_commit`. Do not push and do not open a pull request "
        "-- you cannot, and you do not need to. WHEN THE WORK IS DONE, RUN "
        "THE CHECKS YOURSELF with `run_check` and fix what fails: the submit "
        "is refused unless every one of them has passed at the commit being "
        "submitted, and nothing else runs them for you. Then call "
        "`propose_completion` and the extension pushes and opens the pull "
        "request for a person to merge. If trunk has moved under you "
        "meanwhile, that is a catch-up PLAN a person approves and you then "
        "work, not something done behind you."
    ),
    "submitting": (
        "Your completion claim is in and the extension is pushing. Wait. It "
        "does NOT run your checks -- you do, with `run_check` -- it VERIFIES "
        "they passed at this commit and refuses otherwise, naming the ones "
        "outstanding. A refusal returns the story to the phase you were "
        "working in, so a catch-up comes back to `integrating` and not to "
        "`executing`, under the SAME approved plan."
    ),
    "awaiting_closeout": (
        "Every lane has MERGED. The work is in. The story is still not "
        "done until a person says so -- they mark it done, or ask for "
        "more work, which starts a new plan. Acknowledge and wait; do "
        "not commit or push anything else."
    ),
    "integrating": (
        "A CATCH-UP PLAN IS APPROVED AND IT IS YOURS TO WORK, the same as "
        "any other approved plan. Trunk moved under your branch and the "
        "plan says how to take it in. Call `sync_trunk` once -- it merges "
        "every lane the plan names into its own clone; do not merge by "
        "hand with git, because it resolves which branch goes where from "
        "the record. If it reports conflicts, resolve them in place, "
        "mount by mount, keeping the intent of BOTH sides, and "
        "`story_commit` to conclude each merge. Then run every check with "
        "`run_check`. When they pass, call `propose_completion` and the "
        "extension pushes. Do not push yourself -- you hold no token."
    ),
    "awaiting_merge": (
        "The work is pushed and a pull request is open. The story is NOT "
        "done: a person merges it on GitHub, and then either marks it "
        "complete or asks for more work -- which needs a NEW plan. "
        "Acknowledge and wait; do not commit or push anything else."
    ),
}


# AN ERRAND HAS NO LIFECYCLE, so none of the hints above describe it.
# It produces an answer rather than a branch: no plan is written for one,
# nothing pushes, and there is no pull request to wait on. Served the
# `planning` fallback -- which is what a phase-less errand got -- it was
# told to plan and that "a boundary hook refuses source edits until they
# approve", which is both untrue for an errand and a reason to stop.
_ERRAND_HINT = (
    "This is an ERRAND: it produces an answer, not a branch. No plan is "
    "needed and nothing here pushes or opens a pull request. Do what was "
    "asked, say what you found, and the person marks it done when they "
    "have their answer -- or asks you for more, in which case carry on in "
    "the same errand."
)


def _phase_hint(current_phase: str | None, work_kind: str | None = None) -> str:
    """The hint for this phase, falling back to the one for the action it
    resolves to.

    A FRESHLY ACCEPTED STORY HAS NO PHASE -- checkout does not set one -- so it
    resolves to `planning` and used to get an EMPTY hint, which is exactly the
    turn where the session most needs telling that it may not write code yet.
    """
    if (work_kind or "story") == "errand":
        return _ERRAND_HINT
    phase = (current_phase or "").strip()
    if phase in _PHASE_HINT:
        return _PHASE_HINT[phase]
    return _PHASE_HINT.get("planning", "") if _lifecycle_action(phase) == "planning" else ""


async def _pending_plan(pool, story_id: str, story) -> dict | None:
    """The plan awaiting a person's approval, or None.

    STEPS AND CHECKS COME FROM THE CARD, not from re-parsing the markdown.
    `submit_local_plan` posts a `plan_review` card carrying the normalised
    checks and the hash that was actually approved; the markdown beside it is
    a RENDERING of that, and re-deriving structure from a rendering is how a
    change to the renderer silently changes what a person thinks they agreed
    to. The markdown rides along for display.
    """
    if not story["pending_plan_md"]:
        return None
    from app.services.local_report import latest_card  # noqa: PLC0415
    card = await latest_card(pool, story_id, "plan_review")
    if isinstance(card, str):
        import json  # noqa: PLC0415
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    card = card or {}
    return {
        "path": story["pending_plan_path"] or "",
        "markdown": story["pending_plan_md"],
        "steps": card.get("steps") or [],
        "checks": card.get("checks") or [],
        "plan_hash": card.get("plan_hash") or "",
        # WHOSE PLAN THIS IS. A catch-up reuses the plan card, so without
        # this the person is shown something that looks exactly like a
        # plan they wrote -- when it is platform-written, is about taking
        # a named trunk commit in, and carries checks the story never had.
        # The card has always recorded it; the panel had no way to ask.
        "kind": card.get("plan_kind") or "work",
    }



async def _latest_review(pool, story_id: str) -> dict:
    """`{verdict, reviewed_at}` for the story's most recent review, or {}.

    Empty when nothing has reviewed it -- which the client must read as "no
    verdict yet", never as approval.
    """
    row = await pool.fetchrow(
        "SELECT verdict, reviewed_at FROM story_reviews "
        " WHERE story_id = $1 ORDER BY reviewed_at DESC NULLS LAST LIMIT 1",
        story_id,
    )
    if not row:
        return {}
    return {
        "verdict": row["verdict"] or "",
        "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else "",
    }

async def local_state(story_id: str) -> dict:
    """Read-only story-state snapshot for the `get_state` MCP tool + the
    `UserPromptSubmit` hook. Same status computation as `next_local_phase` but
    WITHOUT claiming a task (the hook calls this every turn)."""
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT goal_id, title, state, current_phase, workspace_path, current_plan_path, "
        "execution_paused, pending_plan_md, pending_plan_path, "
        "COALESCE(work_kind, 'story') AS work_kind FROM stories WHERE id = $1",
        story_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    goal_id = story["goal_id"]
    from app.services import execution_control  # noqa: PLC0415
    paused = bool(story["execution_paused"]) or (
        goal_id is not None and await execution_control.frozen_response(goal_id) is not None
    )
    # THE LIFECYCLE PHASE IS THE WHOLE ANSWER NOW. This used to claim the
    # next ready `claude-code-phase` task and, failing that, read the
    # graph's task counts to tell `blocked` from `busy` from `done`. There
    # is no graph: a local story is worked by a person in their editor, so
    # the only thing the control plane knows about where they are is the
    # story's own phase -- which is what `_lifecycle_action` has always
    # read, and was previously only reached when the graph had nothing to
    # say.
    next_action = "paused" if paused else _lifecycle_action(story["current_phase"])
    return {
        # THE PARKED PLAN, so the person can APPROVE it without the client
        # having to re-submit anything.
        #
        # The extension's Review Plan command used to read the plan file off
        # disk and POST it to `plan-submit` -- the old contract, where the
        # markdown WAS the plan. A plan is steps and checks now and the
        # agent registers it itself with `register_plan`, so re-submitting
        # markdown fails with "steps must be a non-empty list" and the
        # person, who was only trying to approve, is told their plan is
        # invalid. Approving needs the parked plan, not a new submission.
        "pending_plan": await _pending_plan(pool, story_id, story),
        "ok": True,
        "story_id": story_id,
        "goal_id": goal_id,
        "story_title": story["title"],
        "state": story["state"] or "",
        # WHAT KIND OF WORK THIS IS, because three panel rules turn on it
        # and the panel could not tell. An errand has no plan, no review
        # and no pull request to wait for, and `story_done_blocker`
        # already returns None for one -- but the PANEL re-answers the
        # same question locally to grey out a button without a round
        # trip, and without this it answered "waiting on <branch>" and
        # refused Mark Done on a story the control plane would have
        # accepted. The same rule in two places, disagreeing.
        "work_kind": story["work_kind"] or "story",
        # THE VERDICT, so the client can say what the review CONCLUDED
        # rather than only that one happened. `reviewed` + `revise` is work
        # still owed; a client that reports "story reviewed" for both tells
        # the person the opposite of what the reviewer found.
        "review": await _latest_review(pool, story_id),
        # A DIFF IS WAITING, STATED AS A FACT RATHER THAN INFERRED FROM
        # THE PHASE. The extension's "Review & Approve Diff" button was
        # gated on `phase == 'awaiting_review'`, so anything that moved the
        # phase afterwards -- a re-plan, a snapshot, a reconcile -- took the
        # button away while the diff sat undecided, and the only way left to
        # approve was the Control Panel in a browser. The approval row is
        # the thing that is actually true.
        "pending_diff": bool(await pool.fetchval(
            "SELECT 1 FROM approvals WHERE story_id = $1 AND kind = 'diff' "
            "   AND decision IS NULL LIMIT 1", story_id)),
        # WHERE THE WORK IS, ONCE IT IS PUSHED. With no diff-review card,
        # the pull request IS the review, and the panel can only point at
        # one if the control plane says where it is. `all_merged` is the
        # same question `story_done_blocker` asks, answered once here so
        # the panel can enable Mark Done without a second round trip --
        # and the refusal still stands on the server, because a disabled
        # button is a courtesy and not a gate.
        "landing": await _landing(pool, story_id),
        # WHICH CYCLE, so the panel can say "cycle 2" rather than
        # leaving a person to infer it from a plan filename.
        "cycle": await _cycle_state(pool, story_id),
        # SYNC TRUNK BEFORE YOU PUSH. Advisory: the submit gate is
        # what enforces it. The flag exists so a refusal is not the
        # first anybody hears of the requirement.
        "take_in_trunk": await _take_in_trunk(pool, story_id),
        # WHICH COMMIT TO TAKE IN, while the story is integrating.
        #
        # The extension performs the catch-up and needs the sha per lane.
        # It cannot read it from the plan: the plan card is CONSUMED at
        # approval (`pending_plan_md` is cleared), and parsing the sha
        # back out of the plan's prose would make the merge depend on how
        # a template happens to word itself. The control plane knows it
        # exactly -- it is the same query the catch-up offer was built
        # from -- so it says so.
        "integration": await _integration(pool, story_id, story),
        "phase": story["current_phase"] or "",
        "phase_hint": _phase_hint(story["current_phase"], story["work_kind"]),
        "next_action": next_action,
        "plan_path": story["current_plan_path"] or "(not written yet)",
        "workspace_path": story["workspace_path"] or "",
        "execution_paused": bool(paused),
        # ── THE CONSOLIDATION THRESHOLD, SERVED RATHER THAN HARDCODED ───
        #
        # In the cloud, consolidation is level-triggered after every turn:
        # the gateway reads MEMORY.md, and past `memoryFileMaxChars` it
        # rewrites it down to a target, with a ten-minute cooldown. Locally
        # nothing ran mid-story, so MEMORY.md grew unbounded for the whole
        # story and was touched once, at the end, by a prompt the human can
        # cancel.
        #
        # The local session cannot be level-triggered the same way -- there
        # is no turn loop to hang it off, and the Stop hook can no longer
        # block a turn -- so it is told the threshold and asked. Served from
        # here so the two lanes cannot drift to different numbers, which is
        # the failure a hardcoded 30000 in the extension would guarantee the
        # day the gateway's changes.
        "memory": {
            "max_chars": MEMORY_FILE_MAX_CHARS,
            "target_chars": MEMORY_FILE_TARGET_CHARS,
        },
    }


# ── WHAT WAS HERE, AND WHY IT IS NOT ─────────────────────────────────
#
# `next_local_phase` claimed the next `claude-code-phase` task and handed
# it to the local walker; `phase_task_state` was how observe-mode polled
# for the walker reporting back. Both were the DAG's dispatch loop, and a
# local run dispatches nothing: the person is working in their editor and
# the control plane is keeping the record.
#
# `_SELECT_READY` went with them -- it was the claim query.
