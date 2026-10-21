"""What has happened, and — when something is wrong — why and what now.

ONE TOOL, NOT TWO. "How is it going" and "why is it stuck" are the same
question asked at different moments, and splitting them produces two
readers that drift: the day a new failure mode appears, one of them
learns about it. So this always answers the first and adds the second
only when there is something to add.

WHAT WAS MISSING. `story_status` answers from the state machine --
RUNNING, 27 steps, stalled -- which is true and thin. "What's happening"
means what CHANGED: which files, which branch, whether the tests ran.
And when it is stuck, the evidence lived in four places, of which the
assistant could reach exactly one (`stall_reason`); the others were
`criterion_results` (per-check output), `verify_error_count`, and
`git_nodes.last_error` -- recorded, cleared, and surfaced to NOBODY. A
story whose push was refused on a token scope simply stopped
progressing, with the thread and the state machine both silent.

DELIBERATELY NOT FILE ACCESS. Counts, paths and outcomes; never file
contents. Saying what happened is this tool's job -- reading the source
and reviewing it is not, and the file explorer already exists for the
person who wants to look.
"""

from __future__ import annotations

from kernel.assistant.cards import story_key
from kernel.workspace.browse import resolve_execution

# The last few steps, in the order a person reads them. Enough to say
# what it is doing now and what it just did; not a transcript.
RECENT_STEPS = 6


# Stored outputs to name, newest first. Separate from RECENT_STEPS because
# they answer a different question: the recent steps say what a run is doing
# NOW, and an artifact worth reading is usually one from earlier.
ARTIFACTS_SHOWN = 20


async def _steps(conn, eid: int) -> dict:
    rows = await conn.fetch(
        "SELECT seq, tool, generation FROM step_journal "
        "WHERE execution_id = $1 ORDER BY seq DESC LIMIT $2",
        eid, RECENT_STEPS)
    total = await conn.fetchval(
        "SELECT count(*) FROM step_journal WHERE execution_id = $1", eid)
    recent = [{"step": r["seq"], "tool": r["tool"], "leg": r["generation"]}
              for r in reversed(rows)]

    # THE IDS, OR read_artifact IS UNREACHABLE FROM HERE.
    #
    # An artifact id is the sha256 of its bytes, so `read_artifact` works on
    # an id from ANY run — including one in another thread. That made
    # cross-thread reading look solved. It was not: an id has to come from
    # somewhere, and the only places one appeared were the footer of a
    # bounded output in the reader's OWN turn and a user upload. Both are
    # in-thread, so the one case the property existed for — reading what
    # another story produced — had no way to name what to read.
    #
    # `result_ref` has been written on every bounded output since the store
    # landed, and the console already renders it. It was simply never in this
    # answer. The assistant's own prompt said to get ids from here.
    stored = await conn.fetch(
        "SELECT seq, tool, result_ref FROM step_journal "
        " WHERE execution_id = $1 AND result_ref IS NOT NULL "
        " ORDER BY seq DESC LIMIT $2", eid, ARTIFACTS_SHOWN)
    artifacts = [{"id": r["result_ref"], "from_step": r["seq"],
                  "tool": r["tool"]} for r in stored]

    # I1f — AND THE ONES SOMEBODY MEANT TO KEEP. Everything above is the
    # >32 KB ACCIDENT: an artifact exists there because a tool output was
    # too big to inline. That is the wrong half of the story for an
    # errand, whose deliverable is a 2 KB report nobody would ever bound.
    #
    # So `emit_artifact` (Ec2c) writes `kernel.artifact_manifest` and this
    # never looked at it — meaning the tool the prompt names twice for
    # "what did this run store" returned zero rows for exactly the runs
    # that were built to store things deliberately.
    #
    # Both, not one: a story's bounded outputs are still worth naming, and
    # an errand can have both. `emitted` marks which is which, because
    # "the executor chose to keep this" and "this was too big to show you"
    # are different facts and a reader acts on them differently.
    kept = await conn.fetch(
        "SELECT artifact_id, role, name, summary, bytes "
        "  FROM kernel.artifact_manifest "
        " WHERE execution_id = $1 ORDER BY id DESC LIMIT $2",
        eid, ARTIFACTS_SHOWN)
    superseded = set()
    if kept:
        superseded = {r for (r,) in await conn.fetch(
            "SELECT DISTINCT supersedes FROM kernel.artifact_manifest "
            " WHERE execution_id = $1 AND supersedes IS NOT NULL", eid)}
    artifacts = [{"id": r["artifact_id"], "emitted": True, "role": r["role"],
                  "name": r["name"], "summary": r["summary"] or "",
                  "bytes": r["bytes"]}
                 for r in kept if r["artifact_id"] not in superseded] + artifacts

    out = {"steps": total or 0, "recent": recent,
           "doing": recent[-1]["tool"] if recent else None}
    if artifacts:
        out["artifacts"] = artifacts
    return out


async def _work(conn, eid: int) -> list:
    """Per writable checkout: the branch, and what changed on it."""
    from kernel.workspace import explorer as ws
    from kernel.workspace.changed import changed

    bases = {r["branch_name"]: r for r in await conn.fetch(
        """
        SELECT gn.branch_name, gn.base_sha, gn.state, gn.pr_number,
               gn.trunk_branch, gn.last_error
          FROM git_node_stories gns
          JOIN git_nodes gn ON gn.id = gns.git_node_id
         WHERE gns.story_id = (SELECT story_id FROM executions WHERE id = $1)
        """, eid)}

    out = []
    for m in await ws.mounts_of(conn, eid):
        if not m["writable"] or m["kind"] not in ("repo", "branch"):
            continue
        from kernel.workspace.mounts import mounts_for
        ref = next((x.ref for x in await mounts_for(conn, eid)
                    if x.at == m["at"]), "")
        node = bases.get(ref or "")
        entry = {"at": m["at"], "branch": ref or None}
        try:
            entry.update(changed(m["root"], (node or {}).get("base_sha") or ""))
        except Exception as e:                              # noqa: BLE001
            # A summary that cannot be produced must not take the whole
            # answer down with it -- the state and the stall reason are
            # the parts a stuck story most needs said.
            entry["detail"] = f"could not read this checkout: {e}"
        if node:
            entry["lands_on"] = node["trunk_branch"]
            entry["node_state"] = node["state"]
            if node["pr_number"]:
                entry["pr"] = node["pr_number"]
        out.append(entry)
    return out


async def _checks(conn, eid: int) -> list:
    """The most recent claim's checks, with output for the failures."""
    import json

    # The command lives in `criteria.spec->>'run'`; there is no column
    # for it. `held_out` criteria are the verifier's own and are not the
    # executor's to see, but a PERSON asking why their story failed is
    # owed the whole answer, so they are included here.
    rows = await conn.fetch(
        """
        SELECT cr.passes, cr.detail, c.spec::text AS spec, c.label,
               c.held_out
          FROM criterion_results cr
          LEFT JOIN criteria c ON c.id = cr.criterion_id
         WHERE cr.execution_id = $1
           AND cr.claim_seq = (SELECT max(claim_seq) FROM criterion_results
                                WHERE execution_id = $1)
         ORDER BY cr.id
        """, eid)
    out = []
    for r in rows:
        try:
            spec = json.loads(r["spec"] or "{}")
        except (ValueError, TypeError):
            spec = {}
        entry = {"label": r["label"], "run": spec.get("run"),
                 "passes": r["passes"]}
        if r["held_out"]:
            entry["held_out"] = True
        if r["passes"] != "pass":
            try:
                d = json.loads(r["detail"] or "{}")
            except (ValueError, TypeError):
                d = {}
            # The TAIL, not the head. A failing test run says why at the
            # end; the first few hundred characters are the banner, the
            # platform line and the plugin list.
            entry["output"] = (d.get("output") or "")[-400:]
            entry["reason"] = d.get("reason")
        out.append(entry)
    return out


async def _jobs(conn, eid: int) -> list:
    """Background jobs this run has going, from the mirror.

    THE ANSWER TO "HOW IS IT GOING?" WHILE A SUITE RUNS. Everything else
    in this function reads what has ALREADY happened -- steps journalled,
    checks recorded, work committed. A forty-minute job produces none of
    that while it runs, so without this the honest report on a story
    doing exactly what it should is "nothing has happened", which reads
    as stuck.

    "LAST BYTE N SECONDS AGO" IS THE PART THAT MATTERS. `running` alone
    cannot separate a slow compile from a pod that vanished; a job still
    marked running whose last byte was an hour ago is the phantom shape,
    and saying the number lets a person tell the difference without
    knowing anything about pods.
    """
    rows = await conn.fetch(
        "SELECT job_id, command, produced, running, exit_code, ended, "
        "       EXTRACT(EPOCH FROM (now() - last_seen_at))::int AS quiet_s, "
        "       EXTRACT(EPOCH FROM (now() - started_at))::int AS age_s "
        "  FROM kernel.execution_jobs WHERE execution_id = $1 "
        " ORDER BY started_at", eid)
    out = []
    for r in rows:
        job = {"job": r["job_id"], "command": (r["command"] or "")[:160],
               "running": r["running"], "bytes": r["produced"],
               "running_for_s": r["age_s"]}
        if r["running"]:
            job["last_byte_s_ago"] = r["quiet_s"]
        else:
            job["exit_code"] = r["exit_code"]
            # WHY it stopped, because "it ran out of time" is a different
            # sentence from "it failed" and a person asking deserves the
            # right one.
            job["ended"] = r["ended"]
        out.append(job)
    return out


async def story_progress(conn, goal_id: str, story="") -> dict:
    """What this story has done, and what (if anything) is in its way."""
    from kernel.assistant.remedies import (for_node_error, for_question,
                                           for_stall, for_state)

    eid = await resolve_execution(conn, str(story or ""))
    if eid is None:
        return {"story": story, "found": False,
                "detail": "no run for this story yet — it may be created "
                          "but not started"}
    row = await conn.fetchrow(
        "SELECT id, goal_id, story_id, state, generation, workspace_path, "
        "stall_reason, verify_error_count, updated_at "
        "FROM executions WHERE id = $1", eid)
    if row is None or (goal_id and row["goal_id"] != goal_id):
        return {"story": story, "found": False,
                "detail": "no such run in this goal"}

    work = await _work(conn, eid)
    out = {
        "story": story_key(row["story_id"], eid), "run": eid,
        "state": row["state"], "leg": row["generation"],
        "since": row["updated_at"],
        **await _steps(conn, eid),
        "work": work,
        "checks": await _checks(conn, eid),
        "jobs": await _jobs(conn, eid),
    }

    question = await conn.fetchrow(
        "SELECT id, kind, question_text FROM questions "
        "WHERE execution_id = $1 AND status = 'OPEN' ORDER BY id DESC "
        "LIMIT 1", eid)

    # ── the second half, present only when there is something wrong ──
    signals: list = []
    remedies: list = []
    if row["stall_reason"]:
        signals.append({"from": "the run", "says": row["stall_reason"]})
        remedies += for_stall(row["stall_reason"])
    for r in await conn.fetch(
            """
            SELECT gn.branch_name, gn.last_error FROM git_nodes gn
              JOIN git_node_stories gns ON gns.git_node_id = gn.id
             WHERE gns.story_id = $1 AND gn.last_error IS NOT NULL
            """, row["story_id"] or ""):
        signals.append({"from": "the git engine", "branch": r["branch_name"],
                        "says": r["last_error"]})
        remedies += for_node_error(r["branch_name"], r["last_error"])
    failed = [c for c in out["checks"] if c["passes"] != "pass"]
    if failed:
        signals.append({"from": "the verifier",
                        "says": f"{len(failed)} check(s) did not pass"})
    if (row["verify_error_count"] or 0) > 0:
        signals.append({"from": "the adjudicator",
                        "says": f"{row['verify_error_count']} consecutive "
                                f"failure(s) verifying this claim"})
    # ── questions people asked, and what came back ───────────────────
    #
    # A CARD SCROLLS AWAY AND THE ANSWER GOES WITH IT. The assistant is
    # then asked "what did it say about the nulls?" and has nowhere to
    # look -- so it either guesses or tells the person to scroll, and
    # this is the tool its own prompt says to reach for first.
    #
    # `held` is the field that matters. An answer that stopped the work
    # is a different thing to report from one that did not, and getting
    # that wrong means telling somebody their story is running when it
    # is parked and waiting on them.
    asked = await conn.fetch(
        "SELECT id, question, answer, held, failed, asked_by, created_at "
        "FROM answers WHERE execution_id = $1 ORDER BY id DESC LIMIT 5", eid)
    if asked:
        out["questions_asked"] = [
            {"asked": a["question"], "by": a["asked_by"] or "someone",
             "answer": a["answer"],
             # Said in words rather than left as a flag, because the
             # flag is what gets skimmed past.
             "changed_the_work": ("stopped it — a decision is waiting"
                                  if a["held"] else "no"),
             "answered": not a["failed"], "when": a["created_at"]}
            for a in reversed(asked)]

    if question:
        out["waiting_on"] = {"question_id": question["id"],
                             "kind": question["kind"],
                             "asks": question["question_text"]}
        remedies += for_question(question["kind"])
    else:
        remedies += for_state(row["state"], bool(row["workspace_path"]),
                              len(work))

    if signals or question or row["state"] == "PENDING":
        out["stuck"] = {"signals": signals, "remedies": remedies}
    return out
