"""H8 — the three read tools. What is happening, what is waiting, what is in a file.

Everything here comes from a durable row or from arithmetic over durable
rows. Nothing asks a model anything, and nothing reports a model's claim
without saying that is what it is — which is H3's whole point and the
reason `story_status` returns provenance rather than values.

THE PROMPT NAMES NO STATE, and that is a decision rather than an
omission. `run_state_machine` is versioned per run: a deploy never
rewrites an in-flight two-day story, and each execution records the
version it started under. So the legal next steps are READ from that
run's own machine and returned with the status. The assistant explains
what can happen next instead of reciting a list that a later machine
version would make wrong — and it never tells somebody "we can cancel
this" about a run whose machine has no cancel edge, which the machine
module records happening twice in one session.
"""

from __future__ import annotations

from kernel.mailbox.schema import needs_a_person

from kernel.artifacts.reading import BadPattern, read_pattern, read_range
from kernel.artifacts.store import ArtifactNotFound, get_store
from kernel.assistant.cards import as_text, plan_of, story_key
from kernel.assistant.context import Refused, in_flight
from kernel.projections import project_execution, render


async def _legal_next(conn, execution_id: int) -> list:
    """The transitions this run's OWN machine allows from where it is."""
    rows = await conn.fetch(
        """
        SELECT t->>'to' AS to_state, t->>'predicate' AS predicate
          FROM executions e
          JOIN run_state_machine m ON m.version = e.machine_version,
               jsonb_array_elements(m.transitions) t
         WHERE e.id = $1 AND t->>'from' = e.state
         ORDER BY 1
        """,
        execution_id)
    return [{"to": r["to_state"], "when": r["predicate"]} for r in rows]


async def _story_of(conn, eid: int):
    sid = await conn.fetchval(
        "SELECT story_id FROM executions WHERE id = $1", eid)
    return story_key(sid, eid)


async def _phase(conn, eid: int) -> dict:
    """`{"phase": "review"}` while a reviewer is reading, else `{}`.

    THE EXECUTION SAYS `RUNNING` AND THE STORY SAYS `done`, and neither
    is the answer a person wants. A coding story takes one more phase
    after acceptance — a read-only reviewer, in the pod that did the
    work — and without this the assistant reports a story as running
    with no idea what it is running, days after the person said done.

    Absent rather than false when there is no phase: this is read into
    a status a model reads out loud, and a `"phase": null` on every
    ordinary story is a field it will try to explain.
    """
    phase = await conn.fetchval(
        "SELECT phase FROM executions WHERE id = $1", eid)
    return {"phase": phase} if phase else {}


async def story_status(conn, goal_id: str, story=None) -> dict:
    """What is happening. With no story, every one in flight, briefly.

    The no-argument form exists because the assistant is per (goal, user)
    and "how is it going" is usually a question about all of them. The
    single-story form is the one that carries provenance, because that is
    where a person acts on the answer.
    """
    if story is None:
        out = []
        for eid in await in_flight(conn, goal_id):
            proj = await project_execution(conn, eid)
            out.append({
                # ONE ID. The story id is the name everything outside
                # the kernel uses -- the board, the branch, the person.
                # The run number stays visible as `run`, but nothing
                # should ever need to say it back.
                "story": await _story_of(conn, eid),
                "run": eid,
                "state": proj["state"].value if "state" in proj else "unknown",
                "steps": proj["steps"].value if "steps" in proj else 0,
                "waiting_on": (proj["waiting_on"].value
                               if "waiting_on" in proj else None),
                "stalled": (proj["stalled"].value
                            if "stalled" in proj else None),
                **(await _phase(conn, eid)),
            })
        return {"goal": goal_id, "in_flight": len(out), "stories": out}

    # Both identities are accepted: 'de5512b2' (the story) and 1009
    # (the run). Strings resolve to the live run, else the latest.
    from kernel.workspace.browse import resolve_execution
    eid = await resolve_execution(conn, str(story))
    if eid is None:
        return {"story": story, "found": False,
                "detail": "no such story or run in this kernel"}

    proj = await project_execution(conn, eid)
    if "state" not in proj:
        return {"story": story, "found": False,
                "detail": "no such execution in this kernel"}

    row = await conn.fetchrow(
        "SELECT machine_version, goal_id FROM executions WHERE id = $1", eid)
    return {
        "story": await _story_of(conn, eid),
        "run": eid,
        **(await _phase(conn, eid)),
        "goal": row["goal_id"],
        "machine_version": row["machine_version"],
        "legal_next": await _legal_next(conn, eid),
        # Provenance travels with each field, so a caller cannot report a
        # model's claim in the same voice as a verdict row without going
        # out of its way to strip the tag first.
        "facts": {k: {"value": f.value, "provenance": f.provenance,
                      "source": f.source}
                  for k, f in proj.items()},
        # The same thing already said correctly, for a model to read out.
        "rendered": render(proj),
    }


async def inbox(conn, goal_id: str) -> dict:
    """Everything waiting on a person, grouped as cards.

    A card is a VIEW: questions arriving within the batching window are
    grouped for presentation and each still parks exactly one execution.

    `needs_human` is the field that matters. An `authorization` is a plan
    approval and the assistant may not settle it — it can relay a revise
    or a decline, never an approval (H8a). Marking it here means the
    assistant can say *"this one needs you"* rather than discovering the
    refusal by attempting the act.
    """
    rows = await conn.fetch(
        """
        SELECT COALESCE(q.batch_id, 'q-' || q.id::text) AS batch_id,
               q.id, q.execution_id, q.kind, q.question_text,
               q.blocking_on, q.reversible, q.created_at, q.payload,
               e.story_id,
               -- IS THIS AN AUTONOMOUS ERRAND? A plan on one is the
               -- GO's to settle, so it must not be reported as needing
               -- a person -- see `mailbox.schema.needs_a_person`. Same
               -- clauses as `kernel/go/drivable._HAS_GRANT`, read HERE
               -- rather than taken on trust, because a revoked grant
               -- has to put the question back in a person's hands
               -- immediately.
               (COALESCE(s.work_kind, 'story') = 'errand'
                AND EXISTS (SELECT 1 FROM kernel.errand_schedules g
                             WHERE g.goal_id = e.goal_id
                               AND g.approved_by IS NOT NULL
                               AND g.revoked_at IS NULL
                               AND (g.expires_at IS NULL
                                    OR g.expires_at > now())
                               AND (g.story_id IS NULL
                                    OR g.story_id = e.story_id))
               ) AS go_driven
          FROM questions q
          JOIN executions e ON e.id = q.execution_id
          LEFT JOIN stories s ON s.id = e.story_id
           JOIN run_state_machine m ON m.version = e.machine_version
         WHERE e.goal_id = $1 AND q.status = 'OPEN'
           -- A QUESTION ON A TERMINAL RUN IS WAITING ON NOBODY.
           --
           -- Cancelling a story does not close its questions, so they
           -- stayed OPEN for ever and the inbox filled with cards for
           -- work that had stopped. Seven of ten cards in the first live
           -- inbox were this, and an inbox that is mostly dead is one a
           -- person stops reading -- which costs exactly the real card
           -- it was built to surface.
           --
           -- Terminal is read from the execution's OWN machine, for the
           -- same reason the container reaper reads it that way: a run
           -- carried forward has different terminal states, and judging
           -- it by another machine's is how a live card disappears.
           AND NOT (m.terminal_states @> to_jsonb(e.state))
         ORDER BY q.created_at
        """,
        goal_id)

    cards: dict = {}
    for r in rows:
        card = cards.setdefault(r["batch_id"], {
            "card": r["batch_id"], "opened_at": r["created_at"],
            "needs_human": False, "questions": []})
        needs_human = needs_a_person(r["kind"], bool(r["go_driven"]))
        card["needs_human"] = card["needs_human"] or needs_human
        # THE PLAN TRAVELS WITH THE CARD. `question_text` for an
        # approval is the label "plan approval" -- thirteen characters --
        # and the plan itself sat in `payload`, unread by every consumer.
        # The assistant could name the question id and nothing about what
        # it asked, so the person's next words were "approve WHAT?".
        plan = (plan_of(r["payload"])
                if r["kind"] == "authorization" else None)
        entry = {
            "question_id": r["id"],
            "story": story_key(r["story_id"], r["execution_id"]),
            "run": r["execution_id"],
            "kind": r["kind"], "asks": r["question_text"],
            "blocking": r["blocking_on"], "reversible": r["reversible"],
            "needs_human": needs_human,
            "anchor": f"q-{r['id']}",
        }
        if plan:
            entry["plan"] = plan
            entry["plan_text"] = as_text(plan)
        card["questions"].append(entry)

    out = list(cards.values())
    return {"goal": goal_id, "cards": len(out),
            "needs_human": sum(1 for c in out if c["needs_human"]),
            "open": out}


async def list_resources(conn, goal_id: str, story: str = "") -> dict:
    """Everything the goal has, and whether this story already has it.

    The assistant holds no workspace and cannot see a tree, so "which
    repositories?" is otherwise a question it can only put to a person —
    who often does not know the exact clone URL either. Without this the
    first act of every story is a guess.

    `held` is the field that matters. A resource the goal has and the
    story does not is something to ASK GO for; the assistant cannot
    attach it itself, the same way it cannot approve.

    `writable` and `can_write` ARE NOT THE SAME QUESTION and collapsing
    them costs a story. `writable` is what this story holds right now;
    `can_write` is what the catalogue would ever allow. An assistant
    that sees only the first cannot tell a repository it may ask write
    access for from one that is read-only by policy -- so it asks, eats
    an E_NOT_WRITABLE, and learns nothing it could not have been told.

    The expensive case is not the wasted ask. It is criteria: a check
    naming a path the story can never write fails honestly on the
    current tree and can never pass, which no dry-run can detect. That
    is H0x -- fourteen legs and 359 steps spent dying on the leg ceiling
    while looking like a model failure.
    """
    rows = await conn.fetch(
        """
        SELECT r.id AS resource_id, r.kind, r.name,
               COALESCE(r.clone_url, r.uri) AS source, r.default_writable,
               CASE WHEN r.git_repo_id IS NOT NULL THEN
                 ARRAY(SELECT DISTINCT g2.goal_id FROM goal_resources g2
                        WHERE g2.git_repo_id = r.git_repo_id
                          AND g2.goal_id <> r.goal_id
                          AND g2.status = 'active')
               ELSE ARRAY[]::text[] END AS shared_with
          FROM goal_resources r WHERE r.goal_id = $1 ORDER BY r.kind, r.name
        """,
        goal_id)

    held: dict = {}
    if story:
        # `writable` is derived from relation, not a column: the control
        # panel already says needs | produces | modifies, and a boolean
        # beside it would be the same decision recorded twice.
        # Goal scoping via the stories join: the git engine's auto-link
        # leaves story_resources.goal_id NULL (same seam as mounts.py),
        # and filtering on it hid an engine-granted branch from the very
        # assistant it was granted for.
        for m in await conn.fetch(
                "SELECT sr.at, sr.resource_id, "
                "       sr.relation IN ('produces','modifies') AS writable "
                "  FROM story_resources sr "
                "  JOIN stories s ON s.id = sr.story_id "
                " WHERE s.goal_id = $1 AND sr.story_id = $2",
                goal_id, story):
            held[m["resource_id"]] = m

    out = []
    for r in rows:
        mine = held.get(r["resource_id"])
        out.append({
            "resource_id": r["resource_id"], "kind": r["kind"],
            "name": r["name"], "source": r["source"],
            "held": mine is not None,
            # What this story holds NOW.
            "writable": bool(mine["writable"]) if mine else False,
            # What the catalogue would ever allow -- the field that
            # makes "ask GO for write access" a decision rather than a
            # guess.
            "can_write": bool(r["default_writable"]),
            # G8: the same repo serving other goals is a sequencing fact
            # -- cross-goal share existed in the schema all along and was
            # surfaced nowhere. One active merge lane per (repo, trunk)
            # spans goals, so who else is landing on this repo matters.
            "shared_with": list(r["shared_with"] or []),
            "at": mine["at"] if mine else None,
        })
    return {"goal": goal_id, "story": story or None,
            "resources": out,
            "held": sum(1 for r in out if r["held"]),
            "available": sum(1 for r in out if not r["held"]),
            "writable_now": sum(1 for r in out if r["writable"])}


def read_artifact(artifact_id: str, pattern: str | None = None,
                  offset: int = 0, length: int = 0):
    """Part of a stored output — the matching lines, or a byte range.

    The assistant holds NO WORKSPACE (§1.2: it produces no files, so
    there is nothing to corrupt, leak between users or clean up), which
    makes the content-addressed store the only way it can read anything
    at all — a file a user attached, or an executor's oversized output.

    Never the whole object. A read that returned everything would put the
    50 MB back into the context the bounding kept it out of.
    """
    artifact_id = (artifact_id or "").strip()
    if not artifact_id:
        return Refused("E_NO_ARTIFACT",
                       "read_artifact needs an id — pass it as `artifact` "
                       "or `artifact_id`, both work. Ids come from "
                       "`list_artifacts`, from the footer of a bounded "
                       "output, or from an upload.")
    try:
        total = get_store().size(artifact_id)
    except ArtifactNotFound:
        # NAMES THE STORE IT SEARCHED. "in the store" is true and
        # useless: this process picks its store from ARTIFACT_STORE,
        # and a service that never set it searched an empty directory
        # while the writer used a bucket. Saying which one turns "the
        # artifact is missing" into "this reader is looking in the
        # wrong place".
        from kernel.artifacts import store as _store    # noqa: PLC0415

        return Refused("E_NO_SUCH_ARTIFACT",
                       f"no artifact {artifact_id!r} in "
                       f"{_store.describe()}. If the writer used a "
                       f"different binding, this reader is configured "
                       f"wrong rather than the artifact being absent.")

    if pattern:
        try:
            return {"artifact": artifact_id, "bytes": total,
                    "text": read_pattern(artifact_id, pattern, total)}
        except BadPattern as exc:
            return Refused("E_BAD_PATTERN", f"invalid pattern: {exc}")

    return {"artifact": artifact_id, "bytes": total,
            "text": read_range(artifact_id, offset, length, total)}
