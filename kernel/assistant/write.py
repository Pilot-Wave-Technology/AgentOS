"""H8 — the five write tools. The assistant proposes; something else applies.

Nothing here changes a run directly. Every one of these puts a row in a
box and returns, and the executor picks it up AT A STEP BOUNDARY — which
is not indirection for its own sake. A steer applied mid-step lands
between a tool call and its result, producing a message list some
providers reject outright and all of them handle differently, and a
journal that records a leg which did not happen as written.

FIVE, AND THE ABSENCES ARE THE DESIGN. There is no dispatch tool, no
provision tool, no approve, no verify, no commit, and no create-a-story —
not because a prompt forbids them but because no function here does them.
Same argument the codebase already makes about the executor: *"The
executor cannot do any of this, and not because a rule forbids it: there
is no tool."*

EVERY WRITE RESOLVES ITS TARGET FIRST. `context.target` runs before the
act, and an ambiguous reference refuses instead of picking. With several
stories in flight that is the difference between an instruction that goes
nowhere and one that goes somewhere wrong.
"""

from __future__ import annotations

import json

import logging

import asyncpg

from kernel.assistant.cards import story_key
from kernel.assistant.context import Refused, owns, target
from kernel.mailbox.api import submit_proposal

logger = logging.getLogger(__name__)


async def _aim(conn, goal_id, story, focus, hint=None):
    """Resolve a write to one execution in this goal, or refuse.

    `story` accepts BOTH identities: the story id everyone outside the
    kernel uses ('de5512b2') and the kernel's run number (1009). The
    kernel used to speak only run numbers, so a person heard "execution
    1009" about a story they knew as de5512b2 and every card carried
    the wrong name. One id from here on: story ids everywhere a person
    or an assistant looks; run numbers stay internal.
    """
    if story is not None:
        eid = await _as_execution(conn, goal_id, story)
        if eid is None:
            return Refused("E_NO_SUCH_STORY",
                           f"{story!r} matches no story or run in goal "
                           f"{goal_id!r}")
        if not await owns(conn, goal_id, eid):
            return Refused("E_NOT_IN_GOAL",
                           f"story {story} is not in goal {goal_id!r}; an "
                           f"assistant is scoped to one goal")
        return eid
    got = await target(conn, goal_id, hint=hint, focus=focus)
    if isinstance(got, Refused):
        return got
    return got.execution_id


async def _as_execution(conn, goal_id, story):
    """A story id or a run number, resolved to the run it means.

    A story may run more than once; the one a person means is the one
    that is live now, else the latest. Digits are a run number and pass
    through -- the console and old transcripts still speak them.
    """
    text = str(story).strip()
    if text.isdigit():
        return int(text)
    return await conn.fetchval(
        """
        SELECT e.id FROM executions e
         WHERE e.story_id = $1 AND e.goal_id = $2
         ORDER BY (e.container_id IS NOT NULL) DESC, e.id DESC
         LIMIT 1
        """, text, goal_id)


async def _propose(conn, actor_id, kind, payload, key, execution_id):
    """Submit, and turn the mailbox's typed refusal into ours."""
    got = await submit_proposal(conn, actor_id, kind, payload, key,
                                execution_id=execution_id)
    if not isinstance(got, int):
        return Refused(got.code, got.message)
    sid = await conn.fetchval(
        "SELECT story_id FROM executions WHERE id = $1", execution_id)
    # The story id when there IS one, the run id otherwise -- and the
    # run id stays an INT. Stringifying it changed the type of a field
    # callers compare and index by, which is a different bug from the
    # one the story-id work was fixing.
    return {"proposal": got, "story": story_key(sid, execution_id),
            "run": execution_id, "kind": kind,
            "applies": "at the executor's next step boundary"}


async def steer(conn, goal_id: str, actor_id: str, instruction: str,
                idempotency_key: str, story=None, focus=None, hint=None):
    """The next instruction to work that is already in flight.

    NOT BOUNDED BY SIZE. This said "without changing what done means", and
    that description left an instruction which changed the scope with nowhere
    to go: the executor either set it aside for being too big or acted on it
    while the approved plan still described different work. Both are worse
    than the third option, which is what the executor is now told to do — fold
    it into a new plan and have a person approve THAT. So a steer may change
    the plan; the plan is still how the change becomes real.

    THE DISTINCTION FROM `amend` survives, narrower and more useful. Amend
    writes a new contract version and withdraws the approval outright: reach
    for it when the change is one you want RECORDED as a contract, or when the
    story is already FINISHED — amend works from any state and reopens one,
    which a steer cannot do.
    """
    if not (instruction or "").strip():
        return Refused("E_EMPTY_STEER",
                       "a steer must carry an instruction; an empty one "
                       "reaches the executor as a message saying nothing")
    aimed = await _aim(conn, goal_id, story, focus, hint)
    if isinstance(aimed, Refused):
        return aimed
    return await _propose(conn, actor_id, "steer",
                          {"instruction": instruction}, idempotency_key, aimed)


async def ask_executor(conn, goal_id: str, actor_id: str, question: str,
                       idempotency_key: str, asked_by: str = "",
                       story=None, focus=None, hint=None):
    """Ask the story a question, and change nothing by asking.

    THE ONLY INTERROGATIVE TOOL HERE, and its absence was a real gap.
    Every other write on this surface is an order: steer says do it
    differently, amend says the goal moved, cancel says stop. A person
    who merely wanted to KNOW something — why this shape, does it handle
    X, what did you assume — had to phrase it as a steer, and an
    executor forty steps into doing as it is told reads anything in that
    position as one more thing to do. Asked why it had chosen in-memory
    storage, it went and changed the storage.

    WHO ANSWERS DEPENDS ON WHETHER ANYONE CAN, and the caller does not
    choose. `route_question` looks at the run's state:

      RUNNING        the EXECUTOR answers, from its own head, at its
                     next step boundary. It knows what it tried and
                     rejected and what it is about to do -- none of
                     which is written down for anything else to read.
      anything else  the ANSWERER answers beside the run, from the
                     record and a copy of the tree. Five of the seven
                     askable states have no live leg: a park ENDS the
                     leg, so PARKED_HOT looks live and is not.

    Asking a person to know which is asking them to understand the leg
    model in order to ask "what are you doing".

    SAFE EITHER WAY. Neither path may change the work. The executor is
    handed the question labelled as a question, with the instruction not
    to act on it, and answering costs it one step; a job it was waiting
    on keeps running throughout. An `ask` the executor does not answer
    within a few minutes becomes a `question` and the answerer takes it,
    so the fallback is automatic and the person never waits on a leg
    that died.

    IT CAN STILL STOP THE WORK, AND ONLY EVER STOP IT. If the answer
    turns out to need a decision — the plan assumes something untrue,
    or the question was a preference wearing a question mark — the run
    parks on a `direction` question and waits for a person. That is the
    one consequence, it is visible, and it is undone by answering. There
    is no path from here that starts work or lifts a gate.

    TRY THE RECORD FIRST. `story_progress` already knows the state, the
    plan, the checks and the verdicts, and answering from it costs
    nothing. Save this for what needs the code read or the reasoning
    behind a choice.
    """
    if not (question or "").strip():
        return Refused("E_EMPTY_QUESTION",
                       "say what is being asked; an empty question spends a "
                       "model call and answers nobody")
    aimed = await _aim(conn, goal_id, story, focus, hint)
    if isinstance(aimed, Refused):
        return aimed
    from kernel.mailbox.schema import route_question

    state = await conn.fetchval(
        "SELECT state FROM executions WHERE id = $1", aimed)
    kind = route_question(state or "")
    out = await _propose(conn, actor_id, kind,
                         {"question": question, "asked_by": asked_by or ""},
                         idempotency_key, aimed)
    if isinstance(out, dict):
        # `_propose` says "at the executor's next step boundary", which
        # is true of a steer and only half true here — so say which
        # happened rather than leaving a caller to infer it.
        out["applies"] = (
            "the executor answers this itself at its next step boundary; "
            "its work is not touched and a job it is waiting on keeps "
            "running" if kind == "ask" else
            "answered beside the run; the work is not touched unless the "
            "answer needs a decision")
    return out


async def amend(conn, goal_id: str, actor_id: str, idempotency_key: str,
                scope: str = "", criteria_prose: list | None = None,
                story=None, focus=None, hint=None):
    """Change what the story is FOR. Works from any state.

    Criteria go in as PROSE and never as a shell command. The assistant
    knows what was meant and cannot see the tree; the executor can see the
    tree and must not decide what done means. Prose lands as `unlabelled`,
    which is honest — nobody has said whether it is a goal or a guard —
    and the dry-run reports it as uncheckable rather than guessing. The
    executor proposes the runnable form at the next plan, and a person
    approving is what labels it.

    An amendment ALWAYS withdraws the plan approval. A plan approved for
    the old scope is not approval for the new one, and `requires_plan`
    then blocks every write until somebody approves a new one. Structural,
    not advice.
    """
    if not (scope or "").strip() and not criteria_prose:
        return Refused("E_EMPTY_AMENDMENT",
                       "an amendment must change something: a scope, some "
                       "criteria, or both")
    aimed = await _aim(conn, goal_id, story, focus, hint)
    if isinstance(aimed, Refused):
        return aimed

    from kernel.worker.amend import AmendRefused
    from kernel.worker.amend import amend as apply_amendment

    try:
        # `checks` is the COMPLETE new set, not an addition — which is
        # what makes removing one possible at all.
        out = await apply_amendment(conn, aimed, scope=scope or "",
                                    checks=list(criteria_prose or []) or None,
                                    by=actor_id)
    except AmendRefused as exc:
        return Refused("E_AMEND_REFUSED", str(exc))
    logger.info("goal %s: story %s amended by %s", goal_id, aimed, actor_id)
    return out


async def relay_answer(conn, goal_id: str, actor_id: str, question_id: int,
                       text: str, idempotency_key: str, verdict: str = ""):
    """Carry a person's answer to a parked executor.

    NOT an approval, ever. An `authorization` question is a plan waiting
    on a person, and the assistant may relay a `revise` or a `declined`
    but never an approve — the vocabulary has no word for it and
    `resolve_request` refuses without a human uid besides (H8a). Relaying
    a rejection is the point of this tool; a person saying "nearly right,
    change step 3" through the assistant is exactly the case it is for.
    """
    row = await conn.fetchrow(
        "SELECT q.execution_id, q.status, q.kind, e.goal_id "
        "FROM questions q JOIN executions e ON e.id = q.execution_id "
        "WHERE q.id = $1", question_id)
    if row is None:
        return Refused("E_NO_QUESTION", f"no question {question_id}")
    if row["goal_id"] != goal_id:
        return Refused("E_NOT_IN_GOAL",
                       f"question {question_id} belongs to another goal")
    if row["status"] != "OPEN":
        return Refused("E_ALREADY_ANSWERED",
                       f"question {question_id} is {row['status']}; act on "
                       f"the answer rather than replacing it")

    payload = {"question_id": question_id, "text": text}
    if verdict:
        payload["verdict"] = verdict
    return await _propose(conn, actor_id, "answer", payload,
                          idempotency_key, row["execution_id"])


async def cancel_story(conn, goal_id: str, actor_id: str, reason: str,
                       human_uid: str, idempotency_key: str,
                       story=None, focus=None, hint=None):
    """Stop a run. A person's decision, carried.

    `human_uid` is required and recorded. Cancelling is not approving, so
    H8a's guard does not cover it — but it ends somebody's work, and an
    agent deciding that alone is the same shape of mistake. The machine
    gained cancel edges from every state a run can sit in (v3) precisely
    so a story parked on a question nobody would answer has a legal
    ending; that made cancellation reachable, not automatic.
    """
    if not (reason or "").strip():
        return Refused("E_NO_REASON",
                       "a cancellation must record why; the run is about to "
                       "stop and the journal is what remains")
    if not (human_uid or "").strip():
        return Refused("E_NO_HUMAN",
                       "cancelling ends somebody's work and records who "
                       "decided; relay a person's decision, do not take it")
    aimed = await _aim(conn, goal_id, story, focus, hint)
    if isinstance(aimed, Refused):
        return aimed
    return await _propose(conn, actor_id, "cancel",
                          {"reason": reason, "human_uid": human_uid},
                          idempotency_key, aimed)


async def ask_go(conn, goal_id: str, actor_id: str, kind: str, body: str,
                 idempotency_key: str, story=None):
    """Ask the Goal Orchestrator for something. The only creation path.

    The assistant cannot create a story, grant a resource or sequence a
    dependency. It asks, and GO decides — which is why this is a tool and
    story creation is not.

    ROUTING IS H8f, AND THIS SAYS SO. The ask is recorded durably here and
    the result reports `routed: False` until a router exists. A tool that
    returned success for a delivery it had not made is the
    `custom_session_control` failure: a stub answering ok for work it does
    not do is worse than no tool, because the caller stops looking.
    """
    if kind not in ("story", "resource", "sequence", "question", "report"):
        return Refused("E_UNKNOWN_ASK",
                       f"{kind!r} is not something to send GO; expected "
                       f"story, resource, sequence, question or report")
    if not (body or "").strip():
        return Refused("E_EMPTY_ASK", "say what is being asked for")

    # ── `report` is the one kind that ASKS FOR NOTHING ────────────────
    #
    # A story finished, and GO's picture of the goal should not depend
    # on it going and reading a diff. The report is stored ON THE STORY
    # as well as delivered, because delivery is a message in a thread
    # that scrolls away and GO's context is compacted — a summary that
    # exists only as a chat message is one GO cannot consult in three
    # weeks when it is planning the story that builds on this one.
    #
    # It needs a story for exactly that reason: a report with nowhere to
    # live is a message, and there is already a tool for messages.
    if kind == "report":
        if story is None:
            return Refused(
                "E_REPORT_NEEDS_STORY",
                "a report is filed against a story — that is where it is "
                "stored and where GO reads it back. Say which one.")
        stored = await conn.fetchval(
            "UPDATE stories SET result_summary = $3, updated_at = now() "
            "WHERE id = $1 AND goal_id = $2 RETURNING id",
            str(story), goal_id, body.strip())
        if stored is None:
            return Refused("E_NO_SUCH_STORY",
                           f"no story {story} in goal {goal_id!r}")

        # Stage L — the EVIDENCE half, derived here rather than asked for.
        #
        # Which branches, whether they reached trunk, what was granted,
        # what the checks said, what was produced: every one of those is
        # already a row. Asking the assistant to retype them would get a
        # confident answer assembled from a conversation, and it would sit
        # in the record next to facts that are true.
        #
        # Now, at the report, because this is the moment the git engine
        # has had its say. Earlier would record a landing that has not
        # happened — `done` is not `landed`, and this record must never be
        # the place that blurs it.
        try:
            from kernel.knowledge.evidence import derive_evidence
            from kernel.knowledge.read import plans_for
            from kernel.knowledge.write import record_cycle
            evidence = await derive_evidence(conn, goal_id, str(story))
            if evidence:
                cycles = await plans_for(conn, str(story))
                await record_cycle(conn, goal_id, str(story),
                                   len(cycles) or 1, evidence=evidence,
                                   actor="kernel")
        except Exception:                                   # noqa: BLE001
            # A report must not fail because its footnotes could not be
            # gathered. The prose the assistant wrote is the report.
            logger.exception("report: deriving evidence for story %s failed",
                             story)

    if kind != "report" and story is not None and not await owns(
            conn, goal_id, int(story)):
        return Refused("E_NOT_IN_GOAL", f"story {story} is not in this goal")

    row = await conn.fetchrow(
        """
        INSERT INTO assistant_requests
            (goal_id, actor_id, kind, body, execution_id, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (idempotency_key) DO UPDATE SET goal_id = EXCLUDED.goal_id
        RETURNING id, status, routed_at
        """,
        goal_id, actor_id, kind, body,
        # TWO IDENTITIES, AGAIN. Every other kind takes `story` as an
        # execution id and puts it straight in the column; a report is
        # filed against a STORY id, because that is the thing that
        # outlives the run. `_as_execution` accepts both and resolves to
        # the run, which may legitimately be None for a story nobody has
        # started -- a report is about work, so in practice there is one.
        (await _as_execution(conn, goal_id, story) if kind == "report"
         else int(story)) if story is not None else None,
        idempotency_key)

    # H8f/G1: the router (app/services/go_wakes.py) listens on this
    # channel and posts the ask into GO's goal thread as a wake, then
    # stamps routed_at. NOTIFY is the fast path; the router's poll over
    # routed_at IS NULL is the backstop, so a missed notification delays
    # delivery rather than losing it. `routed` stays honest: it reports
    # what has actually happened, not what is expected to.
    await conn.execute(
        "SELECT pg_notify('assistant_ask', $1)",
        json.dumps({"goal": goal_id, "request": row["id"]}))
    if kind == "report":
        # CLOSED ON ARRIVAL. Every other request waits for GO to call
        # `answer_request`, and a PENDING row is how the assistant knows
        # it is still waiting — a report that sat PENDING would read as
        # an unanswered ask for ever, to GO and to the assistant both.
        await conn.execute(
            "UPDATE assistant_requests SET status = 'ANSWERED', "
            "answered_at = now(), answer_text = 'report — no reply needed' "
            "WHERE id = $1", row["id"])
        return {"request": row["id"], "kind": kind, "story": str(story),
                "stored_on_story": True,
                "routed": row["routed_at"] is not None,
                "detail": "stored as the story's result_summary and sent to "
                          "GO. Nothing is waiting on a reply — GO reads it "
                          "to keep its picture of the goal current, and can "
                          "read it back off the story later."}
    return {"request": row["id"], "kind": kind, "status": row["status"],
            "routed": row["routed_at"] is not None,
            "detail": "recorded and signalled; GO is woken in its goal "
                      "thread and answers with answer_request"}


async def _compose_brief(conn, goal_id: str, story: str,
                         task: str) -> str:
    """The executor's brief: the story as GO wrote it, what it may
    touch and why, then the assistant's framing.

    Composed here rather than trusted to the caller, because every
    layer that must remember to copy something eventually forgets --
    and what got forgotten was the entire what-goes-where contract for
    a two-branch story. GO had written it into the description
    ("Backend (cal_backend branch): ... Frontend (cal_frontend
    branch): ...") and the executor never saw a line of it: `task` was
    whatever the assistant typed, and the description sat in a column
    nobody read. The assistant's prose is FRAMING; the story and its
    grants are the SPEC.
    """
    from kernel.workspace.mounts import story_resources

    row = await conn.fetchrow(
        "SELECT title, description FROM stories WHERE id = $1", story)

    # THE MOUNTS ARE THE SCOPE, not the grant rows. This listed every
    # row in `story_resources`, and that table has TWO writers: GO
    # grants the trunk lanes, and the git engine auto-links a row for
    # each branch it cuts. So a two-lane story was described to the
    # executor as FOUR write targets -- each lane once by its trunk name
    # and once by its feature-branch name, in two different
    # vocabularies, for two directories that do not exist.
    #
    # The derivation already resolves that: it excludes the engine's
    # rows when a trunk-lane write grant exists, and it is what actually
    # builds the workspace. Composing from it means the brief,
    # WORKSPACE.md and the tree on disk cannot disagree -- they are one
    # answer from one source. Which is also why the duplication was
    # nearly invisible: the workspace was right the whole time.
    mounts = await story_resources(conn, goal_id, story)

    # The grant rows are still where the human labels live -- the
    # resource's name and the `why` GO recorded -- so they are joined
    # back by resource id rather than re-derived.
    label = {g["resource_id"]: g for g in await conn.fetch(
        """
        SELECT sr.resource_id, gr.name, sr.notes
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1
        """, story)}

    # Where each writable branch LANDS. A feature branch is machinery;
    # what the executor needs to know is which lane it merges into, so
    # it can tell which half of the work belongs in which checkout.
    lands = {r["branch_name"]: r["trunk_branch"] for r in await conn.fetch(
        """
        SELECT gn.branch_name, gn.trunk_branch
          FROM git_node_stories gns
          JOIN git_nodes gn ON gn.id = gns.git_node_id
         WHERE gns.story_id = $1 AND gn.state NOT IN ('MERGED', 'ABANDONED')
        """, story)}

    parts = []
    if row and (row["title"] or "").strip():
        parts += ["# " + row["title"].strip(), ""]
    if row and (row["description"] or "").strip():
        parts += ["## The story", "", row["description"].strip(), ""]
    if mounts:
        parts += ["## What this story may touch", ""]
        for m in sorted(mounts, key=lambda x: (not x.writable, x.at)):
            g = label.get(m.resource_id)
            name = (g["name"] if g else None) or m.at
            verb = "MODIFY" if m.writable else "READ"
            trunk = lands.get(m.ref or "")
            if m.writable and trunk:
                where = ("` -- commit on `" + m.ref
                         + "`, which merges into `" + trunk + "`")
            elif m.writable and m.ref:
                where = "` -- commit on `" + m.ref + "`"
            elif m.ref:
                where = "` -- the landed state of `" + m.ref + "`"
            else:
                where = "`"
            note = ((g["notes"] if g else None) or "").strip()
            why = (chr(10) + "  " + note) if note else ""
            parts.append("- **" + verb + "** " + name + " in `" + m.at
                         + where + why)
        parts += ["",
                  "WORKSPACE.md maps these to the directories in your "
                  "workspace. Each branch is already checked out there: "
                  "never create, switch or merge branches yourself.", ""]
    if (task or "").strip():
        parts += ["## From the person starting this run", "",
                  task.strip(), ""]
    return chr(10).join(parts).strip() or task

async def start_story(conn, goal_id: str, actor_id: str, story: str,
                      task: str, idempotency_key: str,
                      criteria_prose: list | None = None,
                      kind: str = "story", domain: str = ""):
    """Start a RUN of a story GO already created.

    THE ASSISTANT DOES NOT CREATE STORIES AND THIS IS NOT ONE. GO makes
    the story; this makes an execution -- one attempt at it. The
    distinction is not a technicality: a story can be run twice, and the
    second run is a different row with its own journal, verdicts and
    workspace. "Only the Conductor dispatches" also still holds, because
    this writes a PENDING row and stops. Admission and dispatch are the
    Conductor's, on its own tick.

    WHY THE ASSISTANT AND NOT GO. GO writes the story, often thinly --
    an outcome and a title. What repositories it touches, what must not
    break, what "done" means: those come out of a conversation with a
    person, and the assistant is the one having it. A story dispatched
    before that conversation is a story the executor discovers is
    underspecified, three legs in.

    Resources come from the STORY, not from arguments here. GO grants
    them; the assistant asks. Passing them in would be an attach by
    another name.
    """
    if not (task or "").strip():
        return Refused("E_NO_TASK",
                       "a run needs the task in prose; the executor reads "
                       "this and nothing else describes the work")
    if not (story or "").strip():
        return Refused("E_NO_STORY",
                       "say which story this is a run of -- GO created it, "
                       "and a run detached from one cannot be re-run or "
                       "reported against")

    # I3a — THE SHAPE GO CHOSE, unless the assistant deliberately says
    # otherwise. GO writes `work_kind` on the story; without reading it
    # here the planner's judgement never reached the thing that runs the
    # work, and the only route to an errand was the assistant guessing.
    #
    # An explicit `kind` still wins: the assistant has had the
    # conversation GO has not, and "this is smaller than GO thought" is a
    # real correction. NULL on the row means story, which is what every
    # row that predates the column is.
    if not (kind or "").strip() or kind == "story":
        from_story = await conn.fetchval(
            "SELECT work_kind FROM stories WHERE id = $1 AND goal_id = $2",
            story, goal_id)
        if from_story:
            kind = from_story
    kind = (kind or "story").strip().lower()

    # I3b — REFUSED HERE, NOT AT DISPATCH. This is a raw INSERT, so it
    # bypasses `runner.create_execution`'s `resolve(kind, domain)` and
    # nothing else validates the pair. `goal_state` returns a field
    # literally called `kind` whose values are `code`/`merge` — a
    # different taxonomy under the same name — so an assistant echoing
    # the board into this argument wrote a PENDING row that died at
    # dispatch, a long way from its cause.
    if kind not in ("story", "errand"):
        return Refused(
            "E_UNKNOWN_KIND",
            f"{kind!r} is not a work shape. A `story`'s work LANDS ON "
            f"TRUNK; an `errand`'s never does — it gets the same "
            f"allocation, and what survives is what it emits. (`code` and "
            f"`merge` are the board's `kind` field, which is the git "
            f"engine's taxonomy, not this one.)")

    from kernel.workspace.mounts import inherit, story_resources, writable

    mounts = await story_resources(conn, goal_id, story)
    if kind == "story" and not mounts:
        return Refused(
            "E_NO_RESOURCES",
            f"story {story} has no resources, so there is nothing to work "
            f"in. Ask GO to grant what it needs -- `list_resources` shows "
            f"what the goal has.")

    # ── the achievability guard ──────────────────────────────────────
    #
    # Nothing downstream catches this. F11's dry-run proves a check can
    # tell done from not-done, and it is right to: a check naming a repo
    # the story may not write DOES fail on the current tree, honestly.
    # It just can never pass. H0x lost fourteen legs and 359 steps to
    # exactly that -- a criterion needing a file under a frozen path --
    # and the story died on the leg ceiling looking like a model failure.
    writable_at = {m.at for m in writable(mounts)}
    if kind == "story" and criteria_prose:
        readonly = [m.at for m in mounts if not m.writable and m.at
                    and any(m.at in c for c in criteria_prose)]
        if readonly and not writable_at:
            return Refused(
                "E_UNACHIEVABLE",
                f"the criteria mention {readonly} and this story may write "
                f"nothing. A check like that fails now and can never pass, "
                f"which no dry-run can tell you -- ask GO for write access "
                f"first.")

    # THE SAME DERIVATION LOCAL EXECUTION USES. There is no
    # `stories.domain`, so this defaulting IS the domain -- and a second
    # copy of it would let a local run and a pod run of one story resolve
    # different profiles. Both work; they just disagree about what the
    # agent may do.
    from kernel.profiles.story_keys import derive_kind_domain

    kind, domain = derive_kind_domain(kind, domain)

    # I3b — and the PAIR has to resolve, not just the kind. `errand` with
    # a domain nobody built is the same dead row by another route, and
    # `resolve` is what `runner.create_execution` uses, so this refuses
    # exactly what dispatch would refuse — with the list of what exists.
    from kernel.profiles.record import UnknownProfileError
    from kernel.profiles.registry import resolve as _resolve_profile

    try:
        _resolve_profile(kind, domain)
    except UnknownProfileError as unknown:
        return Refused("E_UNKNOWN_PROFILE", str(unknown))

    # ONE RUN AT A TIME, said in words before the database says it in a
    # constraint. The docstring above is right that a story can be run
    # TWICE -- after the first run ends. While one is live a second would
    # take its own admission slot and work the same branch in a second
    # pod, which is why `executions_one_live_per_story` refuses it. That
    # refusal is a UniqueViolationError, and a stack trace out of a tool
    # gives the assistant nothing to say to the person; this gives it a
    # sentence naming the run they probably want to look at.
    from kernel.worker.one_live_run import already_running, live_run

    running = await live_run(conn, story)
    if running is not None:
        return Refused("E_ALREADY_RUNNING", already_running(story, running))

    # AND ONE PLANE AT A TIME, which is the same sentence about a
    # different second writer. `one_live_run` stops two KERNEL runs on
    # one story; nothing stopped a kernel run starting on a story a
    # person has checked out to their own machine. The kernel cannot
    # see that state at all -- `execution_mode` appears nowhere else in
    # `kernel/` -- so the pod would clone the branch the person is
    # working in and push over them, reporting success.
    #
    # Read here rather than inferred from a lock: the local lease is a
    # column on `stories`, it expires, and the reaper clears it, so the
    # live row is the only honest answer.
    local = await conn.fetchrow(
        "SELECT execution_mode, checked_out_to_uid FROM stories WHERE id = $1",
        story)
    if local is not None and (local["execution_mode"] or "") == "local":
        return Refused(
            "E_RUNS_LOCALLY",
            f"{story} is checked out to run on somebody's own machine, so "
            f"there is nothing for a pod to do -- starting one would work "
            f"the same branch from two places. It finishes there, or its "
            f"checkout is released first.")

    # THE STORY'S OWN WORDS REACH THE EXECUTOR: the description GO
    # wrote and the grants it made, composed into the brief here so
    # no layer has to remember to retype them.
    task = await _compose_brief(conn, goal_id, story, task)

    try:
        eid = await conn.fetchval(
            """
            INSERT INTO executions (kind, domain, goal_id, story_id, state,
                                    task)
            VALUES ($1, $2, $3, $4, 'PENDING', $5) RETURNING id
            """,
            kind, domain, goal_id, story, task)
    except asyncpg.exceptions.UniqueViolationError:
        # THE RACE the read above cannot close: two starts in the same
        # moment both read "clear" and both insert. One wins. The loser
        # gets the same answer it would have got a millisecond earlier,
        # rather than the raw constraint name.
        running = await live_run(conn, story)
        return Refused(
            "E_ALREADY_RUNNING",
            already_running(story, running) if running else
            f"story {story} was started by something else in the same "
            f"moment; look at its runs before starting another")
    await inherit(conn, eid, goal_id, story)

    logger.info("goal %s: story %s started as execution %s (%s/%s, %d mount(s))",
                goal_id, story, eid, kind, domain, len(mounts))
    return {"execution_id": eid, "story": story, "kind": kind,
            "domain": domain,
            # THE REF, NOT JUST THE PATH. A writable checkout sits at the
            # canonical unsuffixed path -- repos/<owner>__<repo> -- so
            # reporting the path alone renders an errand's own feature
            # branch as "the whole repository, writable". That is the
            # alarming reading and it is the one a person took: "is this
            # about to write trunk?" is the single most important thing
            # to be able to see about a run at a glance, and that line
            # could not answer it. The row carries the ref; pass it on.
            "mounts": [{"at": m.at, "writable": m.writable, "ref": m.ref}
                       for m in mounts],
            "criteria_prose": list(criteria_prose or []),
            "state": "PENDING",
            "detail": "the Conductor admits and dispatches on its next tick; "
                      "the executor will plan first and park for approval"}


async def show_card(conn, goal_id: str, actor_id: str, question_id: int,
                    idempotency_key: str = ""):
    """Put an open question's card back in the thread.

    WHY THIS IS A RESET AND NOT A POST. The card poster already knows
    how to find the thread, claim the row so two control panels do not
    both post it, render the plan and fan the message out. A second
    posting path would be a second place all of that could be wrong --
    and this one lives in the kernel, which does not own `messages` and
    must not learn to.

    So it clears the poster's claim and the poster does it again, within
    a tick. `card_posted_at IS NULL` is the whole protocol.

    TWO COPIES OF A CARD ARE SAFE, which is what makes this reasonable
    at all: the buttons carry the question id and every write is guarded
    on `status = 'OPEN'`, so whichever copy is clicked settles the one
    question and the other reports it decided. A card scrolled out of
    reach is a real problem; a duplicate is not.
    """
    _ = idempotency_key, actor_id
    row = await conn.fetchrow(
        "SELECT q.status, q.kind, e.goal_id FROM questions q "
        "JOIN executions e ON e.id = q.execution_id WHERE q.id = $1",
        question_id)
    if row is None:
        return Refused("E_NO_QUESTION", f"no question {question_id}")
    if row["goal_id"] != goal_id:
        return Refused("E_NOT_IN_GOAL",
                       f"question {question_id} belongs to another goal")
    if row["status"] != "OPEN":
        # Re-showing a settled card would put live buttons on a decision
        # already made, and the person would click them.
        return Refused(
            "E_ALREADY_ANSWERED",
            f"question {question_id} is {row['status'].lower()}; there is "
            f"nothing left to decide, so say what was decided instead of "
            f"showing the card again")
    await conn.execute(
        "UPDATE questions SET card_posted_at = NULL WHERE id = $1",
        question_id)
    return {"question_id": question_id, "kind": row["kind"],
            "detail": "the card will reappear in the thread within a few "
                      "seconds, with its buttons live"}
