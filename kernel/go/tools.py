"""The Goal Orchestrator's kernel surface. Three tools, and a boundary.

The assistant asks; GO decides. That split is only real if the two have
different surfaces, so these live apart from `kernel.assistant` rather
than as extra entries on it — an assistant that could reach
`grant_resource` by naming it would make the whole arrangement a
convention rather than a boundary.

    list_requests     what assistants have asked for, oldest first
    register_resource put a repository in the goal's catalogue
    grant_resource    attach a catalogued resource to a STORY
    answer_request    close an ask, so the assistant stops waiting

REGISTER AND GRANT ARE TWO ACTS, deliberately. Registering says the goal
owns a thing and whether a story may EVER write it; granting says this
story holds it now. Collapsing them would put "may this be written at
all" in the hands of whoever is attaching it to a story, which is the
policy question the catalogue exists to answer once.

WHAT GO STILL DOES NOT DO. It does not dispatch, does not approve a
plan, does not verify. Granting a resource is an act of authority over
what a story may touch; it is not an act of execution.

WHY GRANTS LAND ON THE STORY. A resource the work needed once it will
need again — an amendment reopens a story and dispatches a fresh run,
and a grant recorded against the execution would be lost exactly when
somebody re-runs the thing that needed it.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id

logger = logging.getLogger(__name__)

TOOLS = ("goal_state",
         # WHO IS ON THE GOAL. The roster used to ride on every
         # message -- a table rebuilt per turn, prepended to the
         # user text, and therefore written into the transcript once
         # per turn for ever. A tool asks for it when it is needed.
         "goal_members",
         "code_state", "plan_preview", "resource_brief",
         "read_resource", "goal_knowledge",
         "list_requests", "register_resource", "grant_resource",
         # I3c — what an errand PRODUCED. GO plans work whose
         # deliverable is an artifact and had no way to read one:
         # `goal_state` shows a story finished, and the answer it
         # found was nowhere GO could look. A goal-scoped read that
         # returns summaries and never bytes.
         "list_artifacts",
         # D2 — AND THE CONTENTS, not only the index. GO held
         # `list_artifacts` and not `read_artifact`: it could see that a
         # report existed and never what it said. Asked what an errand
         # had found, it answered correctly from domain knowledge and
         # presented it as a finding — right answer, invented
         # provenance, undetectable from outside. The asymmetry is what
         # made that the path of least resistance.
         "read_artifact",
         "answer_request", "update_goal",
         # I5e — WATCHING AN ERRAND IT DID NOT START.
         #
         # The autonomy design says the approving human's thread DRIVES an
         # errand while GO answers and watches. GO had no way to do
         # either half: no read that shows a parked run, and no write that
         # unblocks one. Both functions already exist on the assistant's
         # surface, are goal-scoped, and already refuse an approval — so
         # this is set membership, not new authority.
         #
         # WHAT GO MAY ACTUALLY SETTLE. A `clarification` — "which of
         # these two column names did you mean" — which is a question
         # about the work GO planned, and the reason routing it to a
         # person costs a day for nothing. Approving a plan, accepting a
         # completion, allowing a destructive command: refused, because
         # `resolve_request` requires a human uid for those and GO has
         # none to give (§1.3).
         "inbox", "relay_answer",
         # I5f — ASKING FOR AUTONOMY, which is a different act from
         # planning work. GO can already create a story or an errand
         # somebody starts; what it cannot do is decide that a run may
         # finish without a person accepting it. This asks a human for
         # that, and only a human can grant it.
         "propose_errand",
         # CARRYING THE APPROVAL, and carrying a request to run again.
         # `accept_errand` turns a person's "approved" into the errand's
         # story and its grant in one act -- there is no story before
         # that, which is why it takes a proposal and not a story id.
         # `dispatch_errand` asks for one more run under a grant that
         # already exists, because nothing schedules an errand: only the
         # first run is automatic, and it is automatic because approving
         # the errand WAS the decision to run it once.
         #
         # Both refuse without a `human_uid`. Neither is GO's decision.
         "accept_errand", "dispatch_errand",
         # Stage L — the record, and keeping the work pointed at the goal.
         "update_brief", "retire_decision",
         "raise_alignment", "resolve_alignment",
         # PHASE C — DRIVING AN AUTONOMOUS ERRAND.
         #
         # GO now holds a thread per live errand and was expected to
         # drive them while able only to see that one existed. These are
         # the same five functions the assistant uses: two reads that
         # answer "how is it going" at different depths, the instruction
         # channel, the one interrogative that changes nothing by
         # asking, and the carried cancellation.
         #
         # SET MEMBERSHIP IS NOT THE WHOLE BOUNDARY, and cannot be:
         # nothing under these functions checks authority -- each takes
         # `actor_id` as an opaque string -- so membership is a coarse
         # yes-or-no per tool and says nothing about WHICH run. Every one
         # is gated per call by `kernel/go/drivable.go_may_drive`, which
         # refuses anything that is not an errand covered by a live
         # grant. Stories and hand-started errands stay with the
         # assistant, because that is where the person is.
         #
         # `amend` is deliberately absent: it withdraws an approval and
         # writes a new contract version, which is the one thing in this
         # family that changes what a person agreed to.
         "story_status", "story_progress",
         "steer", "ask_executor", "cancel_story",
         # C.4 — PAUSE, RESUME, CANCEL THE RUN, and one tool for all
         # three because they are one control surface: the same verbs,
         # the same run, the same state machine. Three tools meant three
         # chances to wire one to a different mechanism, which is what
         # happened -- a first draft had `pause_errand` writing
         # `execution_pauses` while the Pause BUTTON submitted a `hold`
         # proposal, so one word meant two things.
         #
         # THE RUN, NOT THE STORY, which is why this sits beside
         # `cancel_story` rather than replacing it: that ends the story,
         # and a recurring errand's next run still comes.
         "run_control",
         # WHAT MAKES AN AUTONOMOUS ERRAND ACTUALLY AUTONOMOUS. Its
         # first run parks on its plan, and the person who approved the
         # errand has gone away by design -- so GO could read the plan,
         # explain it, and then only tell them to go and find the card.
         # `resolve_request`'s gate was never "the caller must be
         # human": it is "an approval must carry a human uid", which
         # `cancel_story` has relied on since it was written.
         #
         # GO must ASK AND BE ANSWERED first, and three checks make that
         # more than an instruction: a uid is required on all three
         # verdicts, the uid must be a MEMBER of the goal, and what the
         # person said is required and becomes the answer.
         "decide_plan",
         # AND PUT THE CARD BACK WHEN SOMEBODY ASKS FOR IT. The assistant
         # has had this; GO did not, so "where is the card?" on an errand
         # thread had no answer but a scroll. It is a RESET, not a second
         # posting path: it clears the poster's claim and the poster does
         # it again within a tick.
         "show_card")


async def list_requests(conn, goal_id: str, status: str = "PENDING") -> dict:
    """What the assistants in this goal have asked for.

    Oldest first, because a request nobody answered is the one that has
    been costing somebody time — and a newest-first list buries it under
    everything that arrived since.
    """
    rows = await conn.fetch(
        """
        SELECT id, actor_id, kind, body, execution_id, status,
               created_at, routed_at, answered_at, answer_text
          FROM assistant_requests
         WHERE goal_id = $1 AND ($2 = '' OR status = $2)
         ORDER BY created_at
        """,
        goal_id, status or "")
    return {"goal": goal_id, "count": len(rows),
            "requests": [{
                "request_id": r["id"], "from": r["actor_id"],
                "kind": r["kind"], "asks": r["body"],
                "story": r["execution_id"], "status": r["status"],
                "asked_at": r["created_at"],
                "answered": r["answer_text"],
            } for r in rows]}


async def register_resource(conn, goal_id: str, resource_id: str, source: str,
                            kind: str = "repo", name: str = "",
                            writable: bool = False,
                            registered_by: str = "go") -> dict:
    """Put a resource in the goal's catalogue.

    WITHOUT THIS THERE WAS NO WAY IN. `grant_resource` reads
    `goal_resources` and the only thing that ever wrote it was hand-run
    SQL, so GO held a tool for granting from a catalogue nothing could
    fill -- and the control panel's `register_resource` writes a
    DIFFERENT registry, so using it produced E_NO_SUCH_RESOURCE from a
    tool sitting next to it in the same list.

    `writable` here is the ceiling, not a grant: it says whether any
    story in this goal may EVER write the resource. `grant_resource`
    refuses to exceed it, which is what keeps "read-only by policy"
    meaningful rather than a default somebody can argue past.
    """
    if not (resource_id or "").strip():
        return Refused("E_NO_RESOURCE_ID",
                       "a resource needs an id the assistant can name")
    if not (source or "").strip():
        return Refused("E_NO_SOURCE",
                       f"{resource_id!r} needs a source -- a clone URL or a "
                       f"path -- or there is nothing to mount")
    if kind not in ("repo", "doc", "document"):
        return Refused("E_BAD_KIND",
                       f"{kind!r} is not a resource kind; expected repo or "
                       f"document")
    # `doc` WAS A WORD NOTHING ELSE IN THE SYSTEM USED. A repo is refused
    # outright four lines below, so `doc` was the ONLY row this tool could
    # create -- and `doc` is absent from `WRITE_BACKED_KINDS` here and
    # from the grantable list `go_response` builds for GO's own wake, so
    # every resource GO registered was refused a write grant and invisible
    # in the catalogue it was handed. Accepted on the way in, normalised
    # at the one place it is written.
    if kind == "doc":
        kind = "document"
    # AN ARTIFACT IS ALREADY IN THE CATALOGUE. D2 registers every
    # emitted report, dataset and method as `art_<hash>` the moment it is
    # produced, so registering one again creates a SECOND row for one
    # object -- a different id, no provenance, role 'feature' instead of
    # 'artifact'. Observed on KER25: GO registered three, granted those,
    # and the real rows sat unused beside them.
    #
    # Refused rather than documented. GO's prompt already said this in as
    # many words and GO did it anyway; a prompt is advice and a signature
    # is not, which is the lesson `grant_resource`'s required `why`
    # already encodes.
    if (source or "").strip().startswith("artifact://"):
        from kernel.artifacts.uri import artifact_id_of
        from kernel.harness.artifact_resource import resource_id_for

        existing = resource_id_for(artifact_id_of(source))
        return Refused(
            "E_ALREADY_CATALOGUED",
            f"that artifact is already a resource of this goal: "
            f"{existing!r}. Every report, dataset and method a run emits "
            f"joins the catalogue by itself, named after the work that "
            f"produced it. Grant {existing!r} with `needs` -- registering "
            f"it again makes a second row for one object, without the "
            f"provenance the first one carries.")
    if kind == "repo":
        # F1 — A REPOSITORY REGISTERED HERE IS INCOMPLETE, and the
        # failure lands a long way from the cause.
        #
        # This INSERT writes id, kind, name, uri/clone_url and
        # default_writable. It does NOT write `repo_owner`, `repo_name`,
        # `default_branch`, `branch_name` or `git_repo_id` -- and six
        # modules read the first two, including branch derivation,
        # reconcile, and GO's own `plan_preview`. So the row looks
        # catalogued, grants cleanly, mounts a workspace, and then fails
        # at derivation with nothing naming the reason. `role` is
        # hardcoded 'feature' besides, so a trunk is not registrable
        # either.
        #
        # REFUSED RATHER THAN DOCUMENTED. GO was already deferring
        # repositories to humans, correctly, on reasoning that is not in
        # its prompt -- which is luck. A prompt is advice and a signature
        # is not, which is the lesson `grant_resource`'s required `why`
        # already encodes.
        return Refused(
            "E_REPO_NEEDS_A_HUMAN",
            f"{resource_id!r} is a repository, and registering one takes "
            f"more than this tool writes: the GitHub App installation, "
            f"the trunk branch, and the owner/name the engine derives "
            f"branches from. A repo registered here would grant and mount "
            f"and then fail at branch derivation with nothing saying why. "
            f"Ask a person to add it from the goal's Resources page; then "
            f"`grant_resource` it like any other. Documents you can "
            f"register yourself; a dataset or a service is not a kind "
            f"this tool takes yet.")

    # The control panel's catalogue. `id` IS what the kernel called
    # resource_id -- already text, already the primary key -- and the
    # source lands in clone_url for a repository and uri otherwise,
    # rather than in a `source` column duplicating both.
    row = await conn.fetchrow(
        """
        INSERT INTO goal_resources (id, goal_id, kind, name, uri, clone_url,
                                    default_writable, created_by, role)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'feature')
        ON CONFLICT (id) DO UPDATE
           SET kind = $3, name = $4, uri = $5, clone_url = $6,
               default_writable = $7
        RETURNING id, kind, name, COALESCE(clone_url, uri) AS source,
                  default_writable
        """,
        resource_id, goal_id, kind, name or resource_id,
        source if kind != "repo" else None,
        source if kind == "repo" else None,
        writable, registered_by)
    logger.info("go: registered %s in %s (%s, %s)", resource_id, goal_id, kind,
                "writable" if writable else "read-only")
    return {"resource_id": row["id"], "kind": row["kind"],
            "name": row["name"], "source": row["source"],
            "can_write": row["default_writable"],
            "detail": "in the catalogue; grant it to a story to make it "
                      "reachable"}

WRITE_RELATIONS = ("produces", "modifies")
RELATIONS = ("needs",) + WRITE_RELATIONS

# Kinds whose changes have a road back. Repos and branches land through
# git; documents, files and datasets land through publish_artifact (G6)
# -- an explicit versioned publish, old version retained. A write grant
# on anything else still refuses: an edit with no road back dies with
# the container, silently, and a refusal is honest where loss is not.
WRITE_BACKED_KINDS = {"repo", "branch", "document", "file", "dataset"}


async def grant_resource(conn, goal_id: str, story: str, resource_id: str,
                         relation: str = "needs", at: str = "",
                         granted_by: str = "go", why: str = "") -> dict:
    """Give a story access to one of the goal's resources.

    Works mid-story: the repository appears in any live run's workspace
    immediately and the executor is told. It is readable at once and
    writable from the executor's next leg — the write scope is fixed for
    the leg in flight, and an envelope that changed underneath one would
    mean a write refused at step 12 and allowed at step 13 with nothing
    in the journal to explain either.
    """
    if not (story or "").strip():
        return Refused("E_NO_STORY", "say which story is being granted this")
    if relation not in RELATIONS:
        # ONE vocabulary for access. `writable` booleans, `role`, and
        # RESOURCES.md prose all described the same fact differently and
        # disagreed in production. `relation` is the control panel's own
        # word and now the only one: needs = read, produces|modifies =
        # write, bounded by the catalogue ceiling below.
        return Refused("E_BAD_RELATION",
                       f"{relation!r} is not a relation; expected needs, "
                       f"produces or modifies")
    writable = relation in WRITE_RELATIONS

    # BY NAME TOO. GO reaches for the name it can see -- on the board, in
    # the brief, in what the person said -- and the id in this table was
    # generated by the control panel. See kernel/go/resource_ref.py.
    resolved = await resolve_resource_id(conn, goal_id, resource_id)
    if isinstance(resolved, Refused):
        return resolved
    resource_id = resolved

    row = await conn.fetchrow(
        """
        SELECT id AS resource_id, kind, name, role,
               repo_owner, repo_name, branch_name,
               COALESCE(clone_url, uri) AS source, default_writable
          FROM goal_resources WHERE goal_id = $1 AND id = $2
        """,
        goal_id, resource_id)
    if row is None:
        # Unreachable now that the id is resolved above, and kept as the
        # backstop for a row deleted between the two queries.
        return Refused("E_NO_SUCH_RESOURCE",
                       f"{resource_id!r} is not a resource of {goal_id}")

    if writable and row["kind"] not in WRITE_BACKED_KINDS:
        # A write grant on a non-git resource is a promise the system
        # cannot keep: the story edits the file, the container dies, and
        # the edit dies with it -- with nothing to say it happened. A
        # typed refusal is honest where silent data loss is not. Lifts
        # per kind as the write-back path (G6) covers it.
        from kernel.credentials.clouds import cloud_of_kind

        if cloud_of_kind(row["kind"]) is not None:
            # A CREDENTIAL IS NOT WRITTEN, and the message above would
            # send GO looking for a write-back path that should not
            # exist. `needs` is the only relation that means anything
            # here: the story acts AS this identity, it does not change
            # it. Changing what an identity may do is an IAM act, and
            # deliberately outside anything a run can reach.
            return Refused(
                "E_CREDENTIAL_IS_NOT_WRITABLE",
                f"{resource_id!r} is a cloud identity. Grant it with "
                f"`needs` -- a story ACTS AS an identity, it does not "
                f"edit one. Widening what it may do is an IAM change a "
                f"person makes outside this system, which is what keeps "
                f"a grant a boundary rather than a suggestion.")
        return Refused(
            "E_NOT_GIT_BACKED",
            f"{resource_id!r} is a {row['kind']} and changes to it have "
            f"no way back from a workspace. Repos land through git; "
            f"documents/files/datasets land through publish_artifact. "
            f"This kind has neither, so a write grant would lose the "
            f"story's work silently.")

    if writable and not row["default_writable"]:
        # The catalogue says whether a resource may EVER be written by a
        # story. Refusing here rather than at the tool means a
        # read-only-by-policy repository cannot be made writable by
        # anyone who happens to be holding this tool.
        return Refused(
            "E_NOT_WRITABLE",
            f"{resource_id!r} is read-only for stories in this goal; "
            f"change the resource itself if that is wrong")

    # A WRITE TARGET WITH NO STATED REASON IS A SCOPING DECISION NOBODY
    # WROTE DOWN. `why` reaches the executor's brief as the one line
    # saying what each place is FOR, which is what it needs when
    # deciding where a given file belongs -- and on a story writing two
    # lanes of one repo, that is the whole question. The GO prompt has
    # asked for it since lanes existed and GO shipped a two-lane story
    # with both grants unexplained, because a prompt is advice and a
    # signature is not.
    #
    # Reads are exempt: "what is this input for" is usually answered by
    # the story text, and refusing a read grant over prose would make
    # the cheapest, safest grant the most annoying one to write.
    if writable and not (why or "").strip():
        return Refused(
            "E_NO_REASON",
            f"say why this story writes {resource_id!r}: pass `why` with "
            f"one line naming what belongs there. It becomes the "
            f"executor's scoping instruction, and a story writing more "
            f"than one place cannot tell them apart without it.")

    # Multi-write is a supported shape: a story may hold write
    # grants on several lanes and repos. Each write target derives
    # its own node and branch; they land together as ONE merge
    # group (one approval). The workspace mounts each write lane in
    # its own directory when a repo carries more than one.

    from kernel.workspace.live_grant import grant_to_story, mount_for_resource

    mount = mount_for_resource(row["kind"], row["source"], writable,
                               resource_id=resource_id, at=at,
                               ref=row.get("branch_name") or "")
    out = await grant_to_story(conn, goal_id, story, mount,
                               granted_by=granted_by, relation=relation,
                               why=why)
    # THE RELATION THAT WAS STORED, not the one asked for. An errand's
    # checkout is upgraded to writable on the way through, and the GO
    # reads this reply to know what its story now holds -- echoing the
    # request back would tell it `needs` about a tree it may write.
    relation = out.get("relation") or relation
    logger.info("go: granted %s to %s/%s as %s", resource_id, goal_id,
                story, relation)
    return {"resource": resource_id, "story": story, **out,
            "relation": relation}


async def update_goal(conn, goal_id: str, actor: str,
                      description: str | None = None,
                      objective: str | None = None) -> dict:
    """Rewrite what the goal is FOR, from the conversation that changed it.

    Goals arrive with a human's first guess at a description and
    objective, and the understanding that emerges from talking rarely
    makes it back into the record -- so every later reader (assistants,
    reviews, provisioning) works from the stale version. GO is the one
    having those conversations, so GO may rewrite the record.

    AUTO-RECORDED AS A DECISION. A changed objective changes what every
    story is measured against; doing it silently would be the
    "silently modify the plan" failure the old prompt banned. The
    decision row is the audit trail and the broadcast -- decisions are
    injected into every assistant's context, so the team's agents learn
    the goal moved without being told one by one.
    """
    if description is None and objective is None:
        return Refused("E_NOTHING_TO_UPDATE",
                       "pass description, objective, or both")
    old_row = await conn.fetchrow(
        "SELECT description, objective FROM goals WHERE id = $1", goal_id)
    if old_row is None:
        return Refused("E_NO_SUCH_GOAL", f"no goal {goal_id}")

    await conn.execute(
        """
        UPDATE goals SET description = COALESCE($2, description),
                         objective   = COALESCE($3, objective)
         WHERE id = $1
        """,
        goal_id, description, objective)

    changed = [k for k, v in (("description", description),
                              ("objective", objective)) if v is not None]
    import uuid
    await conn.execute(
        """
        INSERT INTO goal_decisions (id, goal_id, decision, rationale,
                                    decided_by)
        VALUES ($1, $2, $3, $4, $5)
        """,
        # goal_decisions.id has no default; the control panel mints text
        # ids at the call site, so this does the same.
        f"dec-{uuid.uuid4().hex[:12]}",
        goal_id,
        f"goal {' and '.join(changed)} rewritten",
        (objective if objective is not None else description or "")[:2000],
        actor)
    logger.info("go: goal %s %s updated by %s", goal_id,
                "+".join(changed), actor)
    return {"goal": goal_id, "updated": changed,
            "previous": {k: (old_row[k] or "") for k in changed},
            # This sentence was written before it was true: assistants had
            # no tool that returned `goal_decisions`, so a decision recorded
            # here reached nobody but GO. They read `goal_state` now, which
            # carries both the new objective and the decision behind it.
            "detail": "recorded as a decision; the assistants read it from "
                      "goal_state, so they see both the change and why"}


async def answer_request(conn, goal_id: str, request_id: int, text: str,
                         status: str = "ANSWERED") -> dict:
    """Answer an assistant's ask, so it stops waiting on you.

    A request GO acted on but never closed looks identical to one it
    never saw, and the assistant has no way to tell — so it either
    re-asks or reports a decision that was actually made as still
    pending.
    """
    if not (text or "").strip():
        return Refused("E_EMPTY_ANSWER",
                       "an answer with no text closes the request and tells "
                       "the assistant nothing")
    if status not in ("ANSWERED", "REJECTED"):
        return Refused("E_BAD_STATUS",
                       f"{status!r} is not an outcome; expected ANSWERED or "
                       f"REJECTED")

    row = await conn.fetchrow(
        """
        UPDATE assistant_requests
           SET status = $3, answer_text = $4, answered_at = now(),
               routed_at = COALESCE(routed_at, now())
         WHERE id = $1 AND goal_id = $2
        RETURNING id, kind, status
        """,
        request_id, goal_id, status, text)
    if row is None:
        return Refused("E_NO_SUCH_REQUEST",
                       f"no request {request_id} in {goal_id}")
    return {"request_id": row["id"], "kind": row["kind"],
            "status": row["status"], "answer": text}
