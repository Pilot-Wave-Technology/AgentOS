"""G3 — the engine's events, turned into wakes GO can act on.

Each handler: dedupe through go_wake_log (INSERT ... ON CONFLICT DO
NOTHING — a NOTIFY replay, a backstop overlap, or a restart cannot
produce a second wake), build a compact self-describing message, enqueue
it as the GO thread's own turn.

THE HOW-DIGEST rides the story_done wake: the completion claim's reason
set from the kernel, so GO learns not just that work finished but what
was claimed and checked — the record that used to stop at the kernel.

DONE IS NOT LANDED is enforced in prose here too: the story_done wake
says "on the branch, not on trunk" and names who rides next; only the
MERGED wake says delivered.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def _paths(value) -> list:
    """conflict_paths is jsonb, which asyncpg returns as a STRING unless
    a codec is registered. Treating it as a list "worked" only on NULL —
    joining a raw JSON string iterates its characters. Parse honestly."""
    if not value:
        return []
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else [str(value)]



async def _go_thread(pool, goal_id: str):
    return await pool.fetchrow(
        "SELECT go_agent_id, go_thread_id FROM goals "
        "WHERE id = $1 AND status = 'active' "
        "AND COALESCE(go_agent_id,'') <> '' "
        "AND COALESCE(go_thread_id,'') <> ''", goal_id)


async def _errand_thread(pool, execution: int) -> str:
    """The GO thread an errand's own conversation lives on, or "".

    `executions.driving_thread_id` is set by `errand_grants.approve` to
    the thread it opens for the errand, and since Phase B that thread
    belongs to the GO. Checked to be the GO's before it is used: a
    hand-started errand's driving thread is an ASSISTANT's, and a wake
    delivered there would put GO's voice in somebody else's
    conversation.
    """
    try:
        row = await pool.fetchrow(
            "SELECT t.id FROM kernel.executions e "
            "  JOIN threads t ON t.id = e.driving_thread_id "
            " WHERE e.id = $1 AND lower(t.agent_id) LIKE 'go-%'", execution)
    except Exception:                                       # noqa: BLE001
        _log.warning("go_wakes: could not resolve the errand thread for %s",
                     execution, exc_info=True)
        return ""
    return row["id"] if row else ""


async def _deliver(pool, goal_id: str, kind: str, ref: str,
                   content: str, thread_id: str = "") -> bool:
    """Dedupe, resolve the thread, enqueue. Returns whether it went out.

    `thread_id` NAMES THE ERRAND'S OWN THREAD, and without it every wake
    landed on the goal's MAIN one. That was right while the GO had a
    single conversation and became a real cost the moment errands got
    their own: an errand parks many times over its life, and each park,
    each question and each plan went into the main session's context --
    the one conversation a person actually reads, and the one whose
    window has to last the goal.

    Omitted, it is the main thread, which is correct for everything that
    is about the GOAL: a node event, a trunk advance, a resource.
    """
    from app.chat_module.system_ingress import enqueue_system_wake

    claimed = await pool.fetchval(
        "INSERT INTO go_wake_log (goal_id, kind, ref) VALUES ($1,$2,$3) "
        "ON CONFLICT (goal_id, kind, ref) DO NOTHING RETURNING id",
        goal_id, kind, ref)
    if claimed is None:
        return False
    go = await _go_thread(pool, goal_id)
    if go is None:
        # No GO yet: release the claim so the backstop retries later —
        # a wake for a goal mid-provisioning is delayed, not dropped.
        await pool.execute("DELETE FROM go_wake_log WHERE id = $1", claimed)
        return False
    msg_id = await enqueue_system_wake(
        thread_id=thread_id or go["go_thread_id"], goal_id=goal_id,
        agent_id=go["go_agent_id"], gateway_agent_id=go["go_agent_id"],
        agent_type="GO", content=content)
    if msg_id is None:
        # SAY SO. Releasing the claim is right -- the backstop should be
        # able to retry -- but doing it silently cost half an hour on
        # 2026-09-24: an errand wake was computed correctly, dropped
        # here because the process had no chat queue, and left NO
        # exception, NO row and NO log line. Indistinguishable from
        # "there was nothing to say", which is the watch's normal state.
        _log.warning("go_wakes: chat queue disabled in this process; "
                     "%s %s for goal %s was not delivered", kind, ref, goal_id)
        await pool.execute("DELETE FROM go_wake_log WHERE id = $1", claimed)
        return False
    _log.info("go_wakes: %s %s -> goal %s", kind, ref, goal_id)
    return True


async def _skip_review_if_excluded(pool, goal: str, story: str,
                                   execution: int) -> None:
    """Mark an errand / merge / engine-git story `reviewed`, with a reason.

    Reads the same two discriminators the acceptance seam reads, from the
    same places and for the same reasons: the ERRAND test comes from
    `executions.kind`, which is what resolved the profile that actually
    ran, and the engine test comes from `kernel.story_kinds`, the one
    definition all three callers share.
    """
    from kernel.worker.finish import in_pod_review_enabled

    if not in_pod_review_enabled():
        return
    try:
        kind = await pool.fetchval(
            "SELECT kind FROM kernel.executions WHERE id = $1", execution)
        reason = "skipped_errand" if kind == "errand" else ""
        if not reason:
            from kernel.story_kinds import COLUMNS, is_engine_git_story

            row = await pool.fetchrow(
                f"SELECT {', '.join(COLUMNS)} FROM stories WHERE id = $1",
                story)
            if row is not None and is_engine_git_story(row):
                reason = ("skipped_merge" if row["story_kind"] == "merge"
                          else "skipped_diff")
        if not reason:
            return                      # a coding story: it gets a review

        from kernel.worker.review_finish import record_review

        why = {
            "skipped_errand":
                "An errand's deliverable is an artifact, already "
                "measured mechanically when it claimed completion, and "
                "its branch is scaffolding that goes at `done` -- there "
                "is no diff left to read by now.",
            "skipped_merge":
                "A merge story's content is somebody else's reviewed "
                "work, rolled up.",
            "skipped_diff":
                "A diff or fix story is the git engine's own plumbing, "
                "not human-level goal work.",
        }[reason]
        text = chr(10).join((
            f"# Story review - {reason}", "",
            "This story was not reviewed, and that is correct for its "
            "kind. " + why, ""))

        async with pool.acquire() as conn:
            await record_review(conn, execution, verdict=reason,
                                reviewer="routing", review_text=text)
    except Exception:                                       # noqa: BLE001
        # NEVER THE THING THAT BREAKS `on_story_done`. This function also
        # deletes an errand's branch and closes its thread; a failure to
        # record a skip must not cost either.
        _log.exception("skip-review routing failed for story %s", story)


async def on_story_done(pool, goal: str, story: str, execution: int) -> bool:
    # Bridge: a kernel run reaching DONE is the story completing, for
    # stories executed through the kernel. Conservative — never moves a
    # story already terminal.
    #
    # THROUGH THE LEDGER HELPER, and that is not bookkeeping. This was a
    # bare UPDATE, so a kernel story could be accepted, delivered and
    # closed while emitting no `story.state_changed` — and the memory_v2
    # bridge listens for exactly that event to fire the assistant's
    # story-complete flush. Both flags were ON, the machinery worked, and
    # it was never told. KER12's audit trail read `accepted ->
    # in_progress` and then `done -> reviewed`, with the transition INTO
    # done missing: a story recorded as leaving a state it was never
    # recorded as reaching.
    from app.services.story_state_ledger import record_state_pooled

    await record_state_pooled(
        pool, story, goal, "done",
        actor_uid="", action="kernel_run_complete",
        extra_set="completed_at = now()",
        # Terminal states are left alone: a cancelled story whose run
        # happens to finish has not become done.
        #
        # `awaiting_merge` IS A STATE A STORY REACHES DONE FROM, and
        # leaving it out stranded every coding story at the last step.
        # It is the state a coding story sits in from the moment its
        # work is pushed until the merge lands, so by the time this
        # fires it is the ONLY state the story can be in -- the run
        # reaches `phase = review` straight out of the closeout, and
        # this refused the move in silence. Seen on BMIA1 caf071a4:
        # node MERGED, PR #21 merged, closeout answered, review
        # running, story still `awaiting_merge` with nothing wrong
        # anywhere.
        #
        # THIS IS THE INCLUSION-LIST TRAP IN ITS THIRD SHAPE. A plain
        # `state IN (...)` is greppable; `NOT EXISTS (... NOT IN ...)`
        # inverts the polarity; and this one is a PYTHON TUPLE passed
        # as a CAS guard, which no SQL search finds at all.
        only_from=("proposed", "accepted", "in_progress", "blocked",
                   "awaiting_merge"))

    # ── THE KINDS THAT ARE NOT REVIEWED STILL REACH `reviewed` ──────
    #
    # Before this they did not, and the board papered over it: an errand,
    # a merge story and the engine's own diff/fix stories were refused a
    # review and then sat at `done` for ever, rendered under the
    # `reviewed` column so it read as terminal. That cost more than
    # tidiness -- `done` is not in the workspace protect-list's terminal
    # set, so each one pinned its SHCA workspace and its assistant thread
    # binding permanently.
    #
    # A STORY THAT REACHED `reviewed` WITHOUT A REVIEW AND ONE THAT
    # EARNED IT ARE DIFFERENT FACTS, so the reason is recorded on the
    # row rather than inferred from the absence of text.
    await _skip_review_if_excluded(pool, goal, story, execution)

    # AND ITS CONVERSATION IS OVER. This used to happen in
    # `_finalize_merged`, when the last node reached MERGED — which was
    # the end of the story then and is not now: the run parks again on a
    # `closeout`, and the card asking "close the story, or more work?"
    # is posted into that same thread. Closing it at the merge retired
    # the conversation the question was about to be asked in.
    #
    # BEST-EFFORT, like the skip-review routing above and for the same
    # reason: `on_story_done` also unlocks resources and wakes the GO,
    # and a thread that fails to close must not cost either.
    try:
        from app.services.git_graph.story_threads import (
            close_thread_for_finished_story)
        await close_thread_for_finished_story(pool, story)
    except Exception:                                       # noqa: BLE001
        _log.exception("thread closure failed for story %s", story)

    # A finished story holds no locks. The cancel path has released them
    # since the beginning (go_response); the completion path never did,
    # so a lane a story locked while working stayed locked-for a story
    # that was already merged -- and every 'needs' on that lane read as
    # unmet, blocking the next story on the board for ever.
    await pool.execute(
        "UPDATE goal_resources SET locked_by = NULL, locked_for = NULL, "
        "locked_at = NULL WHERE goal_id = $1 AND locked_for = $2",
        goal, story)

    title = await pool.fetchval(
        "SELECT title FROM stories WHERE id = $1", story) or story
    # The how-digest: what the executor claimed when it proposed done.
    claim = await pool.fetchval(
        "SELECT reason_set::text FROM kernel.proposals "
        "WHERE execution_id = $1 AND kind = 'completion' "
        "ORDER BY id DESC LIMIT 1", execution)
    node = await pool.fetchrow(
        """
        SELECT gn.branch_name, gns2.story_id AS next_story, s2.assigned_to
          FROM git_node_stories gns
          JOIN git_nodes gn ON gn.id = gns.git_node_id
          LEFT JOIN git_node_stories gns2
            ON gns2.git_node_id = gn.id AND gns2.seq = gns.seq + 1
          LEFT JOIN stories s2 ON s2.id = gns2.story_id
         WHERE gns.story_id = $1 ORDER BY gns.seq LIMIT 1
        """, story)
    # Ec2f — WHAT IT PRODUCED, not only that it ended.
    #
    # For a coding story the claim plus the branch name is nearly enough:
    # the work IS the branch, and it is named two lines below. An errand
    # has no branch and a discarded tree, so without this GO learned that
    # something finished and nothing about what it found.
    #
    # Wrapped, because a GO wake that fails is a story that completes in
    # silence. The manifest is a nicety on this message and the state
    # transition above is not.
    from app.services import errand_result

    # R2-F5 — WHICH SHAPE OF WORK just finished. The closing line of this
    # wake says the opposite thing for each, and saying a story's line to
    # an errand tells GO to wait for a landing that never comes.
    #
    # Read from the run rather than the story row: `executions.kind` is
    # what resolved the profile that actually ran, so it cannot disagree
    # with what happened.
    is_errand = await pool.fetchval(
        "SELECT kind = 'errand' FROM kernel.executions WHERE id = $1",
        execution) or False

    # R2-F4 — AND THE BRANCH GOES. An errand's branch is scaffolding: it
    # exists so the run has somewhere to work, its work never lands, and
    # the artifacts are the deliverable. Left alone it accumulates one
    # dead ref per errand, each looking like work somebody forgot to
    # merge.
    #
    # Here because this is the one place that runs exactly once when a
    # run reaches DONE and knows which story it was. Best-effort and
    # after the state transition: an untidy branch is a nuisance, a run
    # that cannot finish is not.
    if is_errand:
        try:
            from app.services.git_graph.branch_cleanup import (
                delete_errand_branch)

            outcome = await delete_errand_branch(pool, story)
            _log.info("errand %s accepted: branch cleanup -> %s",
                      story, outcome)
        except Exception:                                   # noqa: BLE001
            _log.exception("errand branch cleanup failed for %s", story)

        # F2 — AND THE THREAD CLOSES. Closure was wired to the merge
        # path, which an errand cannot reach: `close_threads_for_landed`
        # needs a MERGED node and D1 guarantees an errand never lands. So
        # every accepted errand left a live thread behind for ever.
        try:
            from app.services.git_graph.story_threads import (
                close_thread_for_errand)

            closed = await close_thread_for_errand(pool, story)
            if closed:
                _log.info("errand %s accepted: closed threads %s",
                          story, ",".join(closed))
        except Exception:                                   # noqa: BLE001
            _log.exception("errand thread closure failed for %s", story)

    # F4 — THE LEASE RELEASE IS NOT HERE, AND THAT IS DELIBERATE. This
    # runs in the control panel; the lease file lives on the WORKER's
    # filesystem, under a workspace this process may not have mounted at
    # all. A release call here would find no file, delete nothing, log
    # success, and be a fix that cannot run — the shape that has already
    # cost this build twice.
    #
    # It is in `kernel/worker/service_tick.py` instead, where the sweep
    # is and where the files are, narrowed to recent terminal
    # transitions so it can afford to run every tick. (It lived in the
    # console until the worker loop was extracted into a process of its
    # own, and in `service.py` until D2a split that file.)

    produced, summary = "", ""
    try:
        artifacts = await errand_result.artifacts_for(pool, execution)
        produced = errand_result.render(artifacts)
        summary = await errand_result.record(pool, story, execution, claim)
    except Exception:                                       # noqa: BLE001
        _log.exception("errand_result failed for execution %s", execution)

    lines = [f"[STORY DONE] {story} — \"{title}\""]
    if produced:
        lines.append("Produced:")
        lines.append(produced)
    if claim:
        lines.append(f"claimed: {claim[:400]}")
    elif not produced:
        # NULL CLAIM IS NOT AN ERROR — a terminal command exited zero, or
        # somebody accepted it by hand. Saying nothing here reads as "it
        # produced nothing", which is a different statement.
        lines.append(summary or "No completion claim and no artifacts "
                                "recorded; it reached DONE some other way.")
    if node and node["branch_name"]:
        # R2-F5 — AN ERRAND IS DELIVERED ON ACCEPTANCE, and this told GO
        # the opposite.
        #
        # "Nothing is delivered until the merge story lands" is right for
        # a story and exactly backwards for an errand: D1 guarantees an
        # errand never lands, so this instructed GO to wait for something
        # that cannot happen — contradicting the paragraph in its own
        # prompt written to stop precisely that ("waiting for a landing
        # that cannot happen would leave every finished errand's answer
        # sitting unread").
        #
        # It was also factually wrong about the branch: an errand's file
        # changes are scaffolding and are not committed, so nothing
        # "sits on" it.
        if is_errand:
            lines.append(
                "DELIVERED — an errand is done when a person accepts it, "
                "and one just has. There is no merge and no landing wake "
                "coming; the artifacts above ARE the result. Say what it "
                "found. You can read them: `list_artifacts` for the "
                "index, `read_artifact` for the contents — do that before "
                "describing what is in them.")
        else:
            lines.append(f"The work sits on `{node['branch_name']}` — on the "
                         f"branch, NOT on trunk. Nothing is delivered until "
                         f"the merge story lands.")
        if node["next_story"]:
            lines.append(f"Next on this chain: {node['next_story']}"
                         + (f" ({node['assigned_to']})"
                            if node["assigned_to"] else ""))
    lines.append("Acknowledge in one line in the goal thread; do not "
                 "start anything — the team decides what runs next.")
    return await _deliver(pool, goal, "story_done", f"exec:{execution}",
                          "\n".join(lines))


async def on_node_event(pool, goal: str, node_id: str, state: str) -> bool:
    # `trunk_branch` is the LANE this node lands on, and owner/name so the
    # message names a repository rather than a `gr_` hash.
    n = await pool.fetchrow(
        "SELECT gn.branch_name, gn.conflict_paths, gn.last_error, "
        "       gn.pr_number, gn.git_repo_id, gn.trunk_branch, "
        "       r.owner, r.name "
        "  FROM git_nodes gn LEFT JOIN git_repos r ON r.id = gn.git_repo_id "
        " WHERE gn.id = $1", node_id)
    if n is None:
        return False
    riders = await pool.fetch(
        "SELECT gns.story_id, s.assigned_to FROM git_node_stories gns "
        "LEFT JOIN stories s ON s.id = gns.story_id "
        "WHERE gns.git_node_id = $1 ORDER BY gns.seq", node_id)
    who = ", ".join(r["story_id"] for r in riders) or "-"

    if state == "MERGED":
        try:
            # The landing is also the digest event for the brief (G4).
            from app.services.resource_digest import record_landing
            await record_landing(pool, goal, node_id)
        except Exception:                                   # noqa: BLE001
            _log.exception("resource_brief digest failed for %s", node_id)
        # NAME THE LANE, and say whose branches this actually moved. One
        # repository can carry several lanes in one goal; "other branches
        # on this repo are now behind trunk" is false for every branch on
        # a lane that did not move, and the planner acts on it.
        lane = n["trunk_branch"] or ""
        repo = (f"{n['owner']}/{n['name']}" if n["owner"] and n["name"]
                else n["git_repo_id"])
        msg = (f"[LANDED] `{n['branch_name']}` merged into "
               + (f"`{lane}`" if lane else "trunk")
               + f" on {repo}. Stories delivered: {who}.\n"
               + "Say what is now startable and to whom. Check `code_state` "
               + (f"— the other branches landing on `{lane}` are now behind "
                  f"it. Branches on this repo's OTHER lanes are untouched."
                  if lane else
                  "— other branches on this repo are now behind trunk."))
    elif state == "PR_OPENED":
        merger = next((r["assigned_to"] for r in riders
                       if r["assigned_to"]), None)
        msg = (f"[PR OPEN] #{n['pr_number']} for `{n['branch_name']}` "
               f"({who}). A human must merge it"
               + (f" — {merger} is the natural owner." if merger else "."))
    else:
        detail = ""
        cp = _paths(n["conflict_paths"])
        if cp:
            detail = f"\nconflicting paths: {', '.join(cp[:10])}"
        elif n["last_error"]:
            detail = f"\nlast error: {n['last_error'][:200]}"
        msg = (f"[LANE STUCK: {state}] `{n['branch_name']}` ({who}).{detail}\n"
               f"Propose exactly ONE of: a fix story, a resequence, or a "
               f"human decision — in the goal thread, as a proposal.")
    return await _deliver(pool, goal, "node", f"{node_id}:{state}", msg)


async def on_trunk_advance(pool, repo: str, trunk: str, sha: str) -> int:
    """Fan out to every goal with a live branch on this repo."""
    goals = await pool.fetch(
        """
        SELECT goal_id, count(*) AS live,
               count(*) FILTER (WHERE last_synced_trunk_sha IS DISTINCT
                                FROM $3) AS behind
          FROM git_nodes
         WHERE git_repo_id = $1 AND trunk_branch = $2
           AND state NOT IN ('MERGED', 'ABANDONED')
         GROUP BY goal_id
        """, repo, trunk, sha)
    sent = 0
    for g in goals:
        msg = (f"[TRUNK MOVED] {repo} `{trunk}` is now {sha[:12]}. "
               f"{g['behind']} of {g['live']} live branch(es) in this goal "
               f"are behind it. `code_state` shows which; conflicting "
               f"paths there are a resequencing signal.")
        if await _deliver(pool, g["goal_id"], "trunk",
                          f"{repo}:{sha}", msg):
            sent += 1
    return sent


# ── I5g · an errand stopped, and GO is the one watching ──────────────────

def _plan_text(row) -> str:
    """The plan behind a park, as text, or "" when there is not one.

    ONLY FOR THE KINDS THAT CARRY ONE. An escalation payload also has
    `checks` -- meaning "the checks that keep refusing", not "the checks
    that define done" -- and parsing it as a plan produces a confident
    description of something nobody asked. The card learned that the
    hard way; this reads the same guard rather than rediscovering it.
    """
    from kernel.assistant.cards import as_text, plan_of
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    if row["kind"] not in HUMAN_ANSWERED_KINDS:
        return ""
    try:
        return (as_text(plan_of(row["payload"])) or "").strip()
    except Exception:                                       # noqa: BLE001
        # A wake with no plan is worse than one with; a wake that raises
        # is worse than both, because the park then produces nothing at
        # all and the errand looks abandoned.
        _log.exception("parked wake: could not read the plan for %s",
                       row.get("kind"))
        return ""


async def _approver(pool, row) -> str:
    """Who approved this errand, by name. "" when unreadable."""
    from kernel.go.wake_format import approver

    return await approver(pool, row["goal_id"], row["story_id"] or "")


def _parked_wake(q: dict) -> str:
    """The park, in the shape every errand wake shares.

    THE HEADER CARRIES THE ONE FACT THAT MATTERS. `[ERRAND PARKED]` said
    nothing about whether GO could act, so the whole message had to be
    read to find out -- and the sentence that said so was the fifth copy
    of a rule that had moved, which GO obeyed while four other layers
    said the opposite. Splitting the header by WHO MAY SETTLE IT puts
    that in the first line, where it cannot be missed or contradicted.
    """
    from kernel.go.wake_format import wake

    kind = q["kind"]
    decidable = kind == "authorization" and bool(q.get("go_driven"))
    settleable = kind == "clarification"
    evidence = (q.get("plan_text") or "").strip() or         (q["question_text"] or "").strip() or "(no text)"

    mine = ("" if not q.get("proposed_by_me") else
            " You proposed this errand, so say in your report that you "
            "settled this yourself.")

    if decidable:
        return wake(
            header="ERRAND NEEDS YOUR DECISION",
            story=q.get("story") or "", title=q.get("title") or "",
            approver=q.get("approver") or "",
            happened=("The run has parked on its plan and is waiting. "
                      "This one is YOURS to settle -- a person approved "
                      "this errand knowing it would run unattended, and "
                      "sending every plan back puts that wait straight "
                      "back."),
            evidence=evidence,
            imperative=(
                f"Read the plan above against the errand's brief, then "
                f"call `decide_plan` with question_id={q['question']}, a "
                f"verdict of `approve` or `revise`, your reasoning in "
                f"`said`, and NO `human_uid` -- leaving it out is what "
                f"makes it your decision. Ask a person instead if it "
                f"spends real money, touches production, does something "
                f"irreversible, or changes what the errand is FOR."
                + mine),
        )

    if settleable:
        return wake(
            header="ERRAND NEEDS YOUR ANSWER",
            story=q.get("story") or "", title=q.get("title") or "",
            approver=q.get("approver") or "",
            happened=("The run has parked on a clarification. You "
                      "planned this work, so routing it to a person "
                      "costs a day for an answer you already hold."),
            evidence=evidence,
            imperative=(
                f"Answer it with `relay_answer(question_id="
                f"{q['question']}, text=..., idempotency_key=...)`. If "
                f"you genuinely do not know, say so and name who would "
                f"-- do not guess, because a wrong answer here becomes "
                f"the work." + mine),
        )

    return wake(
        header="ERRAND NEEDS A PERSON",
        story=q.get("story") or "", title=q.get("title") or "",
        approver=q.get("approver") or "",
        # BACKTICKED AND FOLLOWED BY A NOUN, which also sidesteps
        # "a acceptance" -- the kinds start with vowels as often as not.
        happened=(f"The run has parked on an `{kind}` question and you "
                  f"cannot settle it -- `relay_answer` will refuse you. "
                  f"A card is already in their thread."),
        evidence=evidence,
        imperative=("Say what is waiting and why it matters, so whoever "
                    "reads it knows what they are deciding. Then leave "
                    "it." + mine),
    )


async def on_execution_parked(pool, goal: str, execution: int,
                              question: int, kind: str) -> bool:
    """An errand parked. Wake GO with what it is and whether it can act.

    Deduped on the QUESTION, not the run: an errand parks many times over
    its life and each park is a distinct thing to know about, while a
    replayed NOTIFY for one is not.
    """
    row = await pool.fetchrow(
        """
        SELECT q.question_text, q.status, q.kind, q.payload,
               e.kind AS run_kind, e.goal_id, e.story_id,
               (SELECT st.title FROM stories st WHERE st.id = e.story_id)
                 AS story_title,
               -- SCALAR, because the grant is no longer joined: the
               -- EXISTS below scopes its own `s`, and leaving this as
               -- `s.proposed_by` would have been an undefined alias --
               -- a query that fails to PARSE, so every park wake dies
               -- and none is sent at all.
               (SELECT p.proposed_by FROM kernel.errand_schedules p
                 WHERE p.goal_id = e.goal_id
                   AND (p.story_id IS NULL OR p.story_id = e.story_id)
                 ORDER BY (p.story_id IS NOT NULL) DESC
                 LIMIT 1) AS proposed_by,
               -- IS THIS ERRAND AUTONOMOUS? A plan on one is GO's to
               -- settle since 2026-09-24. Same clauses as
               -- `kernel/go/drivable._HAS_GRANT`, read here rather than
               -- assumed, so revoking the grant puts the plan back in a
               -- person's hands on the very next park.
               --
               -- AND `story_id IS NULL` IS A GOAL-WIDE GRANT, which the
               -- first cut of this dropped: it joined `s.story_id =
               -- e.story_id`, so a grant covering every errand in the
               -- goal matched no row, `go_driven` came back false, and
               -- GO was told to fetch a person for a plan it owned. It
               -- read as the feature simply not working, on exactly the
               -- grants meant to need the least attention.
               EXISTS (
                 SELECT 1 FROM kernel.errand_schedules s
                  WHERE s.goal_id = e.goal_id
                    AND s.approved_by IS NOT NULL
                    AND s.revoked_at IS NULL
                    AND (s.expires_at IS NULL OR s.expires_at > now())
                    AND (s.story_id IS NULL OR s.story_id = e.story_id)
               ) AS go_driven
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE q.id = $1
        """, question)
    if row is None or row["goal_id"] != goal:
        return False
    # ANSWERED BETWEEN THE NOTIFY AND HERE is the normal case for the
    # backstop, and waking GO about a settled question is noise that
    # teaches it to stop reading these.
    if row["status"] != "OPEN":
        return False
    if row["run_kind"] != "errand":
        # Belt and braces: the NOTIFY already filters, the backstop
        # query already filters, and a story park reaching GO would put
        # two agents on a question that has an assistant.
        return False

    # INTO THE ERRAND'S OWN THREAD. An errand parks many times over its
    # life and every one of those used to land in the goal's main
    # conversation -- the one a person reads, and the one whose context
    # window has to last the goal. Falls back to the main thread when
    # the errand has no GO thread of its own, which is any errand
    # approved before Phase B.
    thread_id = await _errand_thread(pool, execution)

    # A PARK ONLY A PERSON CAN SETTLE GOES QUIET after this wake: the
    # scheduled check-ins are suppressed because GO has nothing to do,
    # which is right, and then nobody is watching a thread on an errand
    # whose whole premise is that nobody is watching. So tell the
    # approver directly -- the bell they already have. Their decision,
    # their runs, and GO repeating itself would not move it.
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    person_only = (row["kind"] in HUMAN_ANSWERED_KINDS
                   and not (row["kind"] == "authorization"
                            and bool(row["go_driven"])))
    if person_only:
        from app.services.errand_human_notice import notify

        await notify(pool, goal_id=goal, story_id=row["story_id"] or "",
                     thread_id=thread_id, question=question,
                     kind=row["kind"], title=row["story_title"] or "")

    return await _deliver(
        pool, goal, "errand_parked", f"q:{question}",
        _parked_wake({"execution": execution, "question": question,
                      "kind": row["kind"],
                      "story": row["story_id"] or "",
                      "title": row["story_title"] or "",
                      # NAMED, because a wake addressed to a firebase
                      # uid reads as plumbing -- which is the tone the
                      # principal exists to displace.
                      "approver": await _approver(pool, row),
                      "question_text": row["question_text"],
                      # THE PLAN ITSELF, and its absence made the whole
                      # feature hollow. On run 308 the wake's entire
                      # body was the two words "plan approval" --
                      # `question_text` -- while the five-step plan sat
                      # in `q.payload`, rendered only onto a card GO
                      # does not read. GO approved a plan it had never
                      # been shown, which is not a decision.
                      #
                      # SAME TWO HELPERS THE CARD USES, so the wake and
                      # the card cannot describe the same plan
                      # differently.
                      "plan_text": _plan_text(row),
                      # THE SEAM THIS WAS MISSING. The query selected
                      # `go_driven` and `_parked_wake` read it, and
                      # nothing carried it across -- so `decidable` was
                      # always None-y and the branch could not fire
                      # once. Both ends were tested and the join
                      # between them was not; run 307 parked with the
                      # old "you cannot settle it" text and a grant
                      # sitting right there in the table.
                      "go_driven": bool(row["go_driven"]),
                      "proposed_by_me": bool(row["proposed_by"])}),
        thread_id=thread_id)
