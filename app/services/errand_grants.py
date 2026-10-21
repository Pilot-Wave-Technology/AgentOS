"""I5f — a person in the goal turns GO's proposal into a standing grant.

THE ONE PLACE THAT KNOWS WHICH HUMAN IS ON THE OTHER END, which is what
makes it the only place a grant can be minted. Every kernel tool call
wears `agentos_worker` and that role has no INSERT on
`errand_schedules`; this path runs as `agentos_human` because a person
is signed in and their uid is the value being recorded.

WHO MAY APPROVE: ANYBODY IN THE GOAL. Wider than the assistant's usual
"the person whose story this is", because an errand GO thought of
belongs to nobody in particular yet — and narrower than what the park
endpoints shipped with, which was anybody signed in at all (I5a).

WHAT APPROVING ACTUALLY DOES, in one transaction:

  * a STORY, so the errand is a thing people can see. Every control the
    UI has is keyed on a story — the activity ticker, pause, resume,
    cancel, the workspace panel — and I1d already learned that a run
    without one is invisible and unanswerable.
  * a SCHEDULE carrying `approved_by`, the bounds the person read on the
    card, and `driving_thread_id` pointing at THEIR assistant thread.
    That column is what makes them the ledger principal and puts the
    cards where they are watching.
  * the proposal marked APPROVED and pointed at the schedule.

WHAT IT DOES NOT DO: pin a plan. The errand has not run and has no plan
to pin, so `approved_plan_hash` stays NULL and `standing_approval` — which
requires a non-NULL hash — covers nothing. The first run parks for plan
approval like any other errand; the hash is pinned when a person approves
THAT. Autonomy starts from the run after. This is the fail-closed
direction: an incompletely authorised grant grants nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from asyncpg.exceptions import UniqueViolationError

log = logging.getLogger(__name__)


class ProposalError(Exception):
    """Refused, with an HTTP status the router can use verbatim."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def driving_thread_for(conn, goal_id: str, uid: str) -> str | None:
    """The approver's own assistant thread in this goal.

    NEWEST OF THEIRS, and never anybody else's. The whole point of
    `driving_thread_id` is that the run follows the person who is
    accountable rather than whoever imported last — so falling back to
    the goal's newest thread would reintroduce exactly the bug it was
    added to fix, quietly, on the path that matters most.

    None is a legitimate answer: a member who has never opened an
    assistant in this goal has no thread. Their approval still stands —
    they are still `approved_by` — and the run resolves the old way.
    """
    return await conn.fetchval(
        """
        SELECT id FROM threads
         WHERE goal_id = $1 AND created_by = $2
           AND left_goal_at IS NULL
         ORDER BY created_at DESC
         LIMIT 1
        """, goal_id, uid)


async def _proposal(conn, pid: str, proposal_id: int):
    row = await conn.fetchrow(
        "SELECT * FROM kernel.errand_proposals WHERE id = $1",
        proposal_id)
    # 404 AND NOT 403 for the wrong goal, same reasoning as I5a: telling
    # somebody a proposal exists in a goal they cannot see is the
    # disclosure.
    if row is None or row["goal_id"] != pid:
        raise ProposalError(404, "No such proposal")
    if row["state"] != "PENDING":
        raise ProposalError(
            409, f"already {row['state'].lower()}"
                 + (f" by {row['decided_by']}" if row["decided_by"] else ""))
    return row


async def decline(conn, pid: str, proposal_id: int, uid: str,
                  reason: str = "") -> dict:
    """No. Recorded, because a proposal that vanishes gets re-proposed."""
    row = await _proposal(conn, pid, proposal_id)
    await conn.execute(
        "UPDATE kernel.errand_proposals SET state = 'DECLINED', "
        "decided_by = $2, decided_at = now(), decline_reason = $3 "
        "WHERE id = $1", proposal_id, uid, (reason or "").strip() or None)
    log.info("errand proposal %s declined by %s", proposal_id, uid)
    return {"proposal": row["id"], "state": "DECLINED", "decided_by": uid}


async def approve(conn, pid: str, proposal_id: int, uid: str) -> dict:
    """Yes, with my name on every run of it."""
    from kernel.go.errands import ONE_SHOT

    row = await _proposal(conn, pid, proposal_id)
    thread_id = await driving_thread_for(conn, pid, uid)

    story_id = str(uuid.uuid4())[:8]
    title = row["name"] or (row["task"][:60] +
                            ("…" if len(row["task"]) > 60 else ""))
    # THE SIX-WORD LABEL, derived rather than demanded. An errand's
    # `name` is already short -- it is what the GO called the schedule --
    # so the first words of it are a good label and there is nothing for
    # anybody to fill in. Clashes cannot be refused here the way they
    # can on `create_story`: a person has already approved this errand
    # and the story MUST be created, so a duplicate falls back to no
    # label and the thread reads from the title instead.
    from kernel.go.short_title import derive
    short_title = derive(title)
    try:
        await conn.execute(
            """
            INSERT INTO stories (id, goal_id, title, description, state,
                                 created_by, assigned_to, work_kind,
                                 sandbox_size, short_title)
            VALUES ($1,$2,$3,$4,'accepted',$5,$5,'errand',$6,$7)
            """, story_id, pid, title, row["task"], uid,
            row["sandbox_size"], short_title)
    except UniqueViolationError:
        log.info("errand %s: short title %r already used in %s; the "
                 "thread will read from the title instead",
                 story_id, short_title, pid)
        await conn.execute(
            """
            INSERT INTO stories (id, goal_id, title, description, state,
                                 created_by, assigned_to, work_kind,
                                 sandbox_size)
            VALUES ($1,$2,$3,$4,'accepted',$5,$5,'errand',$6)
            """, story_id, pid, title, row["task"], uid,
            row["sandbox_size"])

    # AND WHAT IT WAS PROPOSED WITH. This story is created here and
    # nowhere else, so this is the only moment its grants can be made --
    # there is no second call, and nothing downstream asks. Without it
    # the errand runs against an empty workspace and spends its legs
    # hunting for a repository it was never given (KER25 ed57dd54).
    #
    # Validated at PROPOSE time, so by here the ids are known to exist,
    # be active, and carry a legal relation. Re-checked against the goal
    # anyway on the insert, because a resource can be retired between
    # proposing and approving.
    await _grant_proposed_resources(conn, pid, story_id, row, uid)

    # AND A THREAD OF ITS OWN, here rather than in the kernel.
    #
    # The captured `thread_id` above is the approver's NEWEST thread, and
    # every thread is STORY-scoped -- so driving the run with it posts
    # this errand's cards into whatever story that thread belongs to. On
    # KER25 that put a plan-approval card in a finished errand's
    # conversation, where it was correct and invisible.
    #
    # The kernel tried to open one at sweep time and cannot: `threads` is
    # the control panel's table and the kernel role is refused INSERT on
    # it by privilege, exactly as intended. This is the side that owns
    # the table, and it is also the moment the person is watching -- a
    # thread that appears at approval is one they can find immediately,
    # instead of five minutes later when the cron first fires.
    # CAPTURED BEFORE THE THREAD MOVES. The operator workspace is the
    # approver's assistant's, not the GO's -- see `_approver_assistant`.
    # Read here because `_open_story_thread` no longer records it on the
    # row, and the provisioner used to read it back from there.
    approver_assistant = await _approver_assistant(conn, pid, uid, thread_id)
    own = await _open_story_thread(conn, pid, story_id, uid, thread_id, title)
    if own:
        thread_id = own
        # AND THE OPERATOR WORKSPACE, which is what the assistant panel
        # shows and what derives the git node.
        #
        # `stories.workspace_path` holds the operator workspace on the
        # GATEWAY HOST -- not the kernel's container path -- so the panel
        # was empty for every scheduled errand, and `reconcile` (which
        # `provision_story_workspace` calls) never ran for them either,
        # leaving the story unplaced with no branch. Import does both;
        # nothing else did.
        #
        # CHEAP FOR THIS CASE. For a kernel assistant the provisioner is
        # docs-only -- no clone, no token, no credential helper -- because
        # the work happens in a sandbox with its own checkout. The
        # operator flow it also serves is untouched: this only ADDS a
        # caller, and the function is idempotent by contract.
        #
        # IN THE BACKGROUND, and deliberately. It registers the agent on
        # the gateway, and that is the call that timed out at 30s when the
        # gateway was busy archiving. A person's approval must not fail
        # because provisioning was slow -- the runs are driven by the
        # schedule, not by this.
        _schedule_operator_workspace(pid, story_id, uid, thread_id,
                                     approver_assistant)

    # WHEN IT FIRST RUNS. A one-shot is due immediately — a person just
    # said yes and waiting for a cron tick that will never come would
    # look like the approval did nothing. A recurring one waits for its
    # first matching minute, which is what the person agreed to.
    if row["cron"] == ONE_SHOT:
        first = dt.datetime.now(dt.timezone.utc)
    else:
        from kernel.worker.schedules import next_after
        first = next_after(row["cron"], dt.datetime.now(dt.timezone.utc))

    # ── the two writes that carry authority, made as `agentos_human` ──
    #
    # The control panel connects as `agentos`, a superuser, which bypasses
    # every grant — so without this the privilege that is the whole
    # security argument would be true only of the agent surface. Lowering
    # here makes the claim "a grant is minted by a human path" enforced
    # rather than described, and it is the same thing
    # `kernel/worker/accept.py` does at the equivalent seam.
    #
    # SCOPED, because it cannot cover the story: `stories` is a control
    # panel table and `agentos_human` has no INSERT on it. `SET LOCAL`
    # ends with the transaction either way, so a failure between here and
    # COMMIT cannot leave the connection wearing the wrong role.
    await conn.execute("SET LOCAL ROLE agentos_human")

    schedule_id = await conn.fetchval(
        """
        INSERT INTO kernel.errand_schedules
            (goal_id, story_id, kind, domain, task, name, cron,
             next_run_at, max_runs, expires_at, created_by,
             approved_by, approved_at, driving_thread_id,
             proposed_by, rationale, sandbox_size)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,now(),$12,$13,$14,$15)
        RETURNING id
        """,
        pid, story_id, row["kind"], row["domain"], row["task"],
        row["name"], row["cron"], first, row["max_runs"], row["expires_at"],
        uid, thread_id, row["proposed_by"], row["rationale"],
        # ON THE GRANT TOO, not only the story. A RECURRING errand's
        # later runs re-resolve from the story row, and pinning the rung
        # on the grant is what makes run 2 land on the same machine as
        # run 1 rather than on whatever the goal says by then.
        row["sandbox_size"])

    await conn.execute(
        "UPDATE kernel.errand_proposals SET state = 'APPROVED', "
        "decided_by = $2, decided_at = now(), schedule_id = $3 "
        "WHERE id = $1", proposal_id, uid, schedule_id)
    await conn.execute("RESET ROLE")

    log.info("errand proposal %s approved by %s -> schedule %s, story %s, "
             "driving thread %s", proposal_id, uid, schedule_id, story_id,
             thread_id or "(none)")
    return {
        "proposal": row["id"], "state": "APPROVED", "decided_by": uid,
        "schedule": schedule_id, "story": story_id,
        "driving_thread": thread_id,
        "first_run_at": first,
        "detail": "the first run parks for plan approval like any other "
                  "errand; it finishes on its own from the run after that",
    }

async def _grant_proposed_resources(conn, goal_id: str, story_id: str,
                                    row, uid: str) -> int:
    """Replay the proposal's resources as this story's grants.

    Never raises: a grant that cannot be made must not lose the
    approval a person just gave. It is logged loudly instead, and the
    first run reports an empty workspace -- which is visible, unlike an
    exception swallowed at approval time.
    """
    import json as _json

    raw = row["resources"] if "resources" in row.keys() else None
    if not raw:
        return 0
    entries = _json.loads(raw) if isinstance(raw, str) else raw
    made = 0
    for e in entries or []:
        try:
            ok = await conn.fetchval(
                """
                INSERT INTO story_resources (story_id, goal_id, resource_id,
                                             relation, kind, granted_by, notes)
                SELECT $1, $2, gr.id, $4, gr.kind, $5, $6
                  FROM goal_resources gr
                 WHERE gr.id = $3 AND gr.goal_id = $2
                   AND COALESCE(gr.status, 'active') = 'active'
                ON CONFLICT DO NOTHING
             RETURNING resource_id
                """, story_id, goal_id, e.get("resource_id"),
                e.get("relation"), uid, (e.get("why") or "")[:500])
        except Exception:                                   # noqa: BLE001
            log.exception("errand grant: %s could not take %s",
                           story_id, e.get("resource_id"))
            continue
        if ok:
            made += 1
        else:
            log.warning("errand grant: %s named %s, which is gone or "
                         "retired since it was proposed",
                         story_id, e.get("resource_id"))
    log.info("errand grant: story %s granted %d/%d proposed resources",
              story_id, made, len(entries or []))
    return made

async def _approver_assistant(conn, goal_id: str, uid: str,
                              like_thread: str | None) -> str | None:
    """Which assistant this person uses in this goal, or None.

    WHY IT IS NOT JUST `threads.agent_id` ANY MORE. It used to be: the
    errand's thread carried the approver's assistant, and
    `_schedule_operator_workspace` read it back off that row. Phase B
    pointed the thread at the GO, so the value had to be carried
    explicitly -- and carrying it was not enough, because the lookup
    behind it only ever asked THREADS.

    MEASURED ON ERR01, run 277: a goal created fresh, approved from the
    goal thread, no assistant thread in it yet. Both thread lookups
    returned nothing, the provisioner logged "names no assistant", and
    the errand ran with no operator workspace at all. That failure was
    always possible and Phase B makes it the NORMAL case -- the point of
    moving the conversation is that a person no longer needs an
    assistant thread for an errand, so requiring one to have existed is
    exactly backwards.

    So the last resort asks `agents` directly: the person's SHCA for
    this deployment exists whether or not they have ever opened a thread
    with it.

    NEVER THE GO. A `go-%` id here would send the workspace provisioner
    to resolve a GO instance -- `is_kernel_assistant` is false for one,
    so it would take the full-clone branch and mint a token for an agent
    that does no work. The thread lookups can now return one, because
    the errand's own thread names the GO.
    """
    def _ok(value):
        v = (value or "").strip()
        return v if v and not v.lower().startswith("go-") else None

    if like_thread:
        found = _ok(await conn.fetchval(
            "SELECT agent_id FROM threads WHERE id = $1", like_thread))
        if found:
            return found

    found = _ok(await conn.fetchval(
        "SELECT agent_id FROM threads WHERE goal_id = $1 "
        " AND created_by = $2 AND left_goal_at IS NULL "
        " AND COALESCE(agent_id, '') NOT LIKE 'go-%' "
        " ORDER BY created_at DESC LIMIT 1", goal_id, uid))
    if found:
        return found

    # THE ASSISTANT ITSELF, not a conversation with it. Same shape the
    # sidebar uses to find a person's assistant for a goal.
    return _ok(await conn.fetchval(
        "SELECT a.id FROM agents a "
        "  JOIN users u ON u.actor_id = a.owner_actor_id "
        " WHERE u.uid = $1 AND a.type = 'SHCA' AND a.status = 'active' "
        " ORDER BY a.created_at LIMIT 1", uid))


async def _open_story_thread(conn, goal_id: str, story_id: str, uid: str,
                             like_thread: str | None, title: str) -> str | None:
    """A thread for this errand's own story, driven by the GO.

    WHOSE CONVERSATION THIS IS, and why it changed. The thread used to
    take its `agent_id` from whichever thread the approver last used, so
    an approved errand landed on that person's ASSISTANT. That was never
    a decision -- it was the only agent id in reach at the moment the row
    had to be written.

    An autonomous errand is the GO's to drive: the GO proposed it, the
    GO is already woken on every park (`kernel/harness/park_open.py:100`,
    errands only), and the assistant's part is over once a person has
    approved it. So the thread names the GO.

    `created_by` STAYS THE APPROVER, and that is what makes this safe.
    The ledger derives `human_uid` from `threads.created_by`
    (`kernel/ledger/actors.py:111`), never from `agent_id` -- so the
    person who approved remains the principal on every run, and billing,
    attribution and the cancel path are untouched. Only the conversation
    moves.

    ONLY AUTONOMOUS ERRANDS COME THROUGH HERE. `approve` is this
    function's single caller and it is the grant-approval path; a
    hand-started errand never reaches it and keeps the assistant thread
    it was started from.

    `like_thread` is still read, but for `_approver_assistant` -- the
    operator workspace needs to know whose assistant this is even though
    the thread no longer records it. Returns None when no thread could be
    opened, and the caller then falls back to the captured one: an odd
    conversation beats none.
    """
    import uuid as _uuid

    agent_id = await conn.fetchval(
        "SELECT go_agent_id FROM goals WHERE id = $1", goal_id)
    if not agent_id:
        # NO GO ON THIS GOAL. Older goals predate the orchestrator and
        # `go_agent_id` defaults to ''. Falling back to the approver's
        # assistant is exactly the behaviour these goals had before this
        # change, so they keep working rather than losing the thread.
        agent_id = await _approver_assistant(conn, goal_id, uid, like_thread)
        log.warning("errand thread: goal %s has no GO; the thread for %s "
                    "stays on the approver's assistant", goal_id, story_id)
    if not agent_id:
        # NOT A REASON TO GIVE UP, and until now it silently was.
        # `threads.agent_id` is NOT NULL DEFAULT '', so passing None made
        # the INSERT raise, the savepoint roll back, and the errand keep
        # the approver's CAPTURED thread -- a card in somebody else's
        # story, which is the exact outcome this function exists to
        # prevent. The comment said "opening the thread anyway" and the
        # code could not.
        #
        # `''` IS THE COLUMN'S OWN DEFAULT and the honest value: nobody
        # could be resolved. The thread is still the errand's, still
        # owned by the approver, and still the right place for its
        # cards.
        agent_id = ""
        log.warning("errand thread: no agent id for %s in %s; opening "
                    "the thread anyway, unowned", uid, goal_id)

    existing = await conn.fetchval(
        "SELECT id FROM threads WHERE goal_id = $1 AND story_id = $2 "
        "AND created_by = $3 AND left_goal_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1", goal_id, story_id, uid)
    if existing:
        return existing

    thread_id = str(_uuid.uuid4())
    # THE THREAD'S NAME IN THE SIDEBAR, and it used to open with the
    # story id: `9dd4ca65 — Document the four endpoints`. Nobody
    # recognises the hex, so every errand row began with eight
    # characters of noise before the part a person reads.
    #
    # `label_for` prefers the GO's six-word short title, falls back to
    # the first words of the real title for anything created before it
    # existed, and only reaches the id when a story has no title at all.
    from kernel.go.short_title import label_for
    # READ FROM THE ROW rather than threaded through every caller:
    # the story exists by now, and a parameter added to this
    # signature would have to be passed correctly by each of them.
    short = await conn.fetchval(
        "SELECT short_title FROM stories WHERE id = $1", story_id)
    label = label_for(short, title, story_id)
    # A SAVEPOINT, because `except` does not un-poison a transaction.
    # This runs inside the approval's own transaction, and a failed
    # INSERT (a foreign key, a constraint) aborts it -- so catching the
    # Python exception leaves every later statement failing with
    # InFailedSQLTransactionError and the person's approval is lost for
    # the sake of a thread. The nested block rolls back to here and the
    # approval stands.
    try:
        async with conn.transaction():
            # PARTICIPANTS EMPTY, WHICH MEANS THE WHOLE GOAL.
            #
            # It used to be ARRAY[approver], and under the visibility
            # rule a non-empty list means "these people and nobody
            # else" -- so an autonomous errand, which is the GO acting
            # on the goal's behalf while nobody watches, was visible to
            # exactly one person. The one person least likely to need
            # telling, since they had just approved it.
            #
            # Empty is the GO-thread convention and reads as "every
            # member of the goal" in both the panel's access check and
            # the Slack membership sync. An errand is goal business.
            await conn.execute(
                """
                INSERT INTO threads (id, goal_id, agent_id, story_id,
                                     created_by, title, participants, status)
                VALUES ($1,$2,$3,$4,$5,$6,'{}'::text[],'active')
                """, thread_id, goal_id, agent_id, story_id, uid, label[:120])
            await conn.execute(
                "UPDATE stories SET active_thread_id = $2, updated_at = now() "
                "WHERE id = $1 AND COALESCE(active_thread_id, '') = ''",
                story_id, thread_id)
    except Exception:                                       # noqa: BLE001
        log.exception("errand thread: could not open one for %s; the "
                      "approval stands and the run keeps the captured "
                      "thread", story_id)
        return None
    log.info("errand thread: opened %s for %s/%s (owner %s)",
             thread_id, goal_id, story_id, uid)

    # AND ITS SLACK CHANNEL. An errand thread never had one: it is
    # created here rather than through ensure_story_thread, which is
    # where every other thread picks one up, so the one kind of work
    # that runs with nobody watching was the one kind with no channel.
    # ON `conn`, NOT THE POOL, and this is the third time this trap has
    # been walked into in this codebase.
    #
    # `approve` runs inside `conn.transaction()` in all three of its
    # callers, so the thread INSERT above is NOT YET COMMITTED. Passing
    # `get_pool()` here handed the channel creator a different
    # connection: it made the Slack channel and wrote the binding
    # (both fine, both on the pool), and then `sync_channel_members`
    # read `threads WHERE id = $1` to find out who should be invited,
    # could not see the row, and returned an empty set. No invites, no
    # error, no log line.
    #
    # Measured on CALY5: channel C0CA1138EG2 existed, correctly named
    # and bound, containing exactly one member -- the bot. Slack does
    # not show a non-member a private channel, so to the person who
    # approved the errand it simply had not appeared.
    #
    # Same shape as the errand proposal card and the notify before it.
    # Everything here reads through `fetch`/`fetchrow`/`execute`, which
    # a connection has, so the whole operation now sees the pending row
    # and rolls back with it.
    try:
        from app.channels.slack.thread_channels import ensure_channel_for_thread
        await ensure_channel_for_thread(
            conn, goal_id=goal_id, thread_id=thread_id,
            title=label, agent_id=agent_id, story_id=story_id)
    except Exception as exc:                                # noqa: BLE001
        # Same rule as the thread insert above: the approval stands.
        log.warning("errand thread: no Slack channel for %s: %s",
                    thread_id, exc)
    return thread_id

def _schedule_operator_workspace(goal_id: str, story_id: str, uid: str,
                                 thread_id: str,
                                 assistant_id: str | None = None) -> None:
    """Provision the story's operator workspace without blocking approval.

    Fire-and-forget on purpose: the approval is the person's act and is
    already committed by the time this matters. A failure here leaves the
    panel empty and the story unplaced — visible, and repairable by an
    import — where a failure inline would lose the approval itself.

    `assistant_id` IS PASSED, NOT READ OFF THE THREAD. It used to come
    from `threads.agent_id`, which worked only while that column held the
    approver's assistant. The errand thread now names the GO, and
    `provision_story_workspace` branches on
    `is_kernel_assistant(agent_id)` — false for a GO — so reading the row
    would silently upgrade every errand from a docs-only workspace to a
    full clone with a token and a credential helper. The argument is
    optional so the old call shape still resolves the same value.
    """
    import asyncio

    async def _go() -> None:
        try:
            from app.database import get_pool
            from app.services.operator_provisioning import (
                provision_story_workspace)

            pool = get_pool()
            agent_id = assistant_id or await pool.fetchval(
                "SELECT agent_id FROM threads WHERE id = $1", thread_id)
            if not agent_id:
                # LOUD, because the errand still RUNS. It dispatches with
                # no operator workspace, `stories.workspace_path` stays
                # empty, the file panel shows nothing, and reconcile never
                # gives it a git node -- and the only trace was a warning
                # among a hundred request lines. Observed on ERR01/5227676d
                # before `_approver_assistant` learned to ask `agents`.
                log.error("errand workspace: no assistant for %s in %s, so "
                          "%s RUNS WITHOUT ONE -- no workspace_path, no "
                          "file panel, no git node. Import the story to "
                          "repair it.", uid, goal_id, story_id)
                return
            out = await provision_story_workspace(
                goal_id, story_id, agent_id, thread_id)
            if out.get("ok"):
                log.info("errand workspace: provisioned %s for %s",
                         out.get("workspace_path"), story_id)
            else:
                log.warning("errand workspace: %s not provisioned (%s)",
                            story_id, out.get("error"))
        except Exception:                                   # noqa: BLE001
            log.exception("errand workspace: provisioning %s failed",
                          story_id)

    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        # No loop (a sync caller, or a test): skip rather than block. The
        # story is still correct; only the panel and the node are late.
        log.warning("errand workspace: no event loop; %s deferred", story_id)
