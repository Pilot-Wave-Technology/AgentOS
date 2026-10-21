"""H0v — the queries behind the console.

Separated from the HTTP layer so each answer is testable without a socket.

STATE IS THE POINT. A journal shows what a run did; the state machine shows
what the system thinks is happening, which is what you need when the two
disagree. Every view here carries three things together: where the run is,
how it got there, and what the declared machine says may happen next.

The undeclared-transition flag is the load-bearing part. `execution_transitions`
records what actually happened and `run_state_machine` records what was
permitted; comparing them surfaces the case where code and the declared
machine have drifted apart. That drift is not hypothetical — Eb2 found R3
and machine version 1 disagreeing about how an errand can possibly end.
"""

from __future__ import annotations

import json


async def machine(conn, version: int) -> dict:
    """The declared state machine for one version."""
    row = await conn.fetchrow(
        "SELECT version, states::text, transitions::text, "
        "park_states::text, terminal_states::text "
        "FROM run_state_machine WHERE version = $1", version)
    if row is None:
        return {}
    return {
        "version": row["version"],
        "states": json.loads(row["states"]),
        "transitions": json.loads(row["transitions"]),
        "park_states": json.loads(row["park_states"]),
        "terminal_states": json.loads(row["terminal_states"]),
    }


def legal_next(m: dict, state: str) -> list[dict]:
    """What the machine says may happen from here."""
    return [t for t in m.get("transitions", []) if t.get("from") == state]


def is_declared(m: dict, frm: str | None, to: str) -> bool:
    """Was this transition permitted by the machine in force?

    A first insert (frm is None) is not a transition and is always fine.
    """
    if frm is None:
        return True
    return any(t.get("from") == frm and t.get("to") == to
               for t in m.get("transitions", []))


async def executions(conn, goal_id: str | None = None,
                     limit: int = 50) -> list[dict]:
    """Runs, newest first, scoped to one goal.

    Unscoped, this listed every execution in the database — and the test
    suites leave dozens behind, so three real runs sat under thirty-four
    fixtures with ids in the eight thousands. A window you have to scroll
    past somebody else's litter to use is a window people stop opening.

    `goal_id = None` still returns everything, because reconcile and the
    tests want that; the console passes its own.
    """
    rows = await conn.fetch(
        """
        SELECT e.id, e.kind, e.domain, e.state, e.generation,
               e.machine_version, e.task, e.container_id, e.workspace_path,
               e.terminal_exit_code, e.tokens_to_first_write, e.updated_at,
               (SELECT count(*) FROM step_journal s
                 WHERE s.execution_id = e.id) AS steps,
               (SELECT count(*) FROM questions q
                 WHERE q.execution_id = e.id AND q.status = 'OPEN') AS waiting
        FROM executions e
        WHERE ($1::text IS NULL OR e.goal_id = $1)
        ORDER BY e.id DESC
        LIMIT $2
        """, goal_id, limit)
    return [dict(r) for r in rows]


async def transitions(conn, execution_id: int) -> list[dict]:
    """The path a run took, each hop marked declared or not."""
    rows = await conn.fetch(
        "SELECT from_state, to_state, generation, machine_version, at "
        "FROM execution_transitions WHERE execution_id = $1 ORDER BY id",
        execution_id)
    out, machines = [], {}
    for r in rows:
        v = r["machine_version"]
        if v not in machines:
            machines[v] = await machine(conn, v)
        out.append({
            "from": r["from_state"], "to": r["to_state"],
            "generation": r["generation"], "machine_version": v,
            "at": r["at"].isoformat(timespec="seconds"),
            "declared": is_declared(machines[v], r["from_state"], r["to_state"]),
        })
    return out


async def journal(conn, execution_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT seq, generation, depth, tool, args::text, cost::text,
               model_text, result_ref, ts
        FROM step_journal WHERE execution_id = $1 ORDER BY seq
        """, execution_id)
    steps = []
    for r in rows:
        cost = json.loads(r["cost"]) if r["cost"] else {}
        steps.append({
            "seq": r["seq"], "generation": r["generation"],
            "depth": r["depth"], "tool": r["tool"],
            "args": json.loads(r["args"]) if r["args"] else {},
            "tokens": sum(v for v in cost.values()
                          if isinstance(v, (int, float))),
            "text": r["model_text"],
            "artifact": r["result_ref"],
            "at": r["ts"].isoformat(timespec="seconds"),
        })
    return steps


async def briefs(conn, execution_id: int) -> list[dict]:
    """What each leg handed to the next one."""
    rows = await conn.fetch(
        "SELECT generation, data::text FROM kernel.execution_checkpoints "
        "WHERE execution_id = $1 AND kind = 'leg_brief' ORDER BY generation",
        execution_id)
    from kernel.harness.brief import dict_to_brief, format_brief_for_prompt

    out = []
    for r in rows:
        payload = json.loads(r["data"])
        rendered = format_brief_for_prompt(dict_to_brief(payload))
        progress = payload.get("progress_state")
        if progress:
            rendered = f"{rendered}\n\n{progress}"
        out.append({"generation": r["generation"], "rendered": rendered})
    return out


async def parks(conn, execution_id: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, kind, question_text, payload::text, status, declined, "
        "answer_text, answered_by, proposed_default, blocking_on, "
        "created_at, checks::text AS checks "
        "FROM questions WHERE execution_id = $1 ORDER BY id",
        execution_id)
    # The console speaks the caller's vocabulary — a person reads
    # "plan_approval", not "authorization".
    # F11's escalation is not an ordinary question: the HARNESS raised
    # it, not the executor, and a person reading "question" would go
    # looking for which model asked. It answers like one.
    out_kind = {"authorization": "plan_approval",
                "escalation": "the harness is asking"}
    return [{"id": r["id"],
             "kind": out_kind.get(r["kind"], "question"),
             "question": r["question_text"],
             "payload": json.loads(r["payload"]),
             "proposed": r["proposed_default"],
             "blocking_on": r["blocking_on"],
             # Declined is answered and is NOT permission; the page must
             # show the difference or an approval screen lies.
             "status": ("declined" if r["declined"] else r["status"].lower()),
             "answer": r["answer_text"], "answered_by": r["answered_by"],
             # F11 — the checks a person approving a plan is also
             # approving, so they travel with it rather than being a
             # second screen. WHAT THEY DID against the pre-work tree
             # used to travel beside them; E9 retired the dry run that
             # measured it (plan 5.6) and `questions.dryrun` is now
             # never written, so shipping it would be shipping `{}`.
             "checks": json.loads(r["checks"] or "[]"),
             "at": r["created_at"].isoformat(timespec="seconds")}
            for r in rows]


async def proposals(conn, execution_id: int) -> list[dict]:
    """What the assistant has said, and what became of it.

    Without this the console can show a steer's EFFECT and never the steer
    — the journal records tool calls, not injected turns, so a person
    watching a redirected run has to take it on trust that the redirect is
    why. The status is the interesting column: ACCEPTED means it reached a
    step boundary, PENDING means it has not yet (a parked run has no leg
    to apply it to), SUPERSEDED means something later overtook it, and
    REJECTED means it was refused with a reason.
    """
    rows = await conn.fetch(
        "SELECT id, actor_id, kind, status, payload::text, created_at "
        "FROM proposals WHERE execution_id = $1 ORDER BY id", execution_id)
    said = {"steer": "instruction", "cancel": "reason", "replan": "reason",
            "answer": "text"}
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        out.append({
            "id": r["id"], "actor": r["actor_id"], "kind": r["kind"],
            "status": r["status"].lower(),
            "text": payload.get(said.get(r["kind"], ""), ""),
            "at": r["created_at"].isoformat(timespec="seconds"),
        })
    return out


async def verdicts(conn, execution_id: int) -> list[dict]:
    """What the Verifier decided, and what it could see while deciding.

    The verdict alone is not enough to act on. A person looking at a
    refusal needs the criterion that failed and its output; a person
    looking at a PASS needs to know whether anything could not be
    checked — F6's parity, rendered as sentences, because the failure
    mode is scrolling past a figure nobody interpreted.
    """
    from kernel.verifier.parity import concerns

    rows = await conn.fetch(
        "SELECT id, claim_seq, passed, refusal::text, parity::text, "
        "generation, adjudicator, created_at FROM verdicts "
        "WHERE execution_id = $1 ORDER BY id", execution_id)
    out = []
    for r in rows:
        parity = json.loads(r["parity"])
        results = await conn.fetch(
            "SELECT cr.run_index, cr.passes, cr.detail::text, c.kind, "
            "c.spec::text FROM criterion_results cr "
            "JOIN criteria c ON c.id = cr.criterion_id "
            "WHERE cr.execution_id = $1 AND cr.claim_seq = $2 "
            "ORDER BY cr.criterion_id, cr.run_index",
            execution_id, r["claim_seq"])
        out.append({
            "id": r["id"], "claim_seq": r["claim_seq"],
            "passed": r["passed"],
            "refusal": json.loads(r["refusal"]),
            "parity": parity, "concerns": concerns(parity),
            "generation": r["generation"], "by": r["adjudicator"],
            "at": r["created_at"].isoformat(timespec="seconds"),
            "criteria": [
                {"run": json.loads(c["spec"]).get("run", ""),
                 "kind": c["kind"], "run_index": c["run_index"],
                 "passes": c["passes"],
                 "output": (json.loads(c["detail"]).get("output") or "")[-1500:]}
                for c in results],
        })
    return out


async def contract_of(conn, execution_id: int):
    """The definition of done this run was dispatched under, if any."""
    row = await conn.fetchrow(
        "SELECT c.id, c.base_image, c.check_surface_hash, "
        "c.workspace_spec::text, c.approved_by, c.approved_at "
        "FROM contracts c JOIN executions e ON e.contract_id = c.id "
        "WHERE e.id = $1", execution_id)
    if row is None:
        return None
    criteria = await conn.fetch(
        "SELECT kind, spec::text, held_out FROM criteria "
        "WHERE contract_id = $1 ORDER BY id", row["id"])
    spec = json.loads(row["workspace_spec"])
    return {
        "id": row["id"], "image": row["base_image"],
        "surface": spec.get("check_surface") or [],
        "surface_hashed": bool(row["check_surface_hash"]),
        "approved_by": row["approved_by"],
        "checks": [json.loads(c["spec"]).get("run", "") for c in criteria],
    }


async def detail(conn, execution_id: int) -> dict:
    from kernel.worker.accept import acceptance_of

    row = await conn.fetchrow(
        "SELECT id, kind, domain, state, generation, machine_version, task, "
        "container_id, workspace_path, terminal_command, terminal_exit_code, "
        "terminal_output, tokens_to_first_write, steps_to_first_write "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        return {}
    m = await machine(conn, row["machine_version"])
    return {
        "execution": dict(row),
        "machine": m,
        "next": legal_next(m, row["state"]),
        "parked": row["state"] in m.get("park_states", []),
        "terminal": row["state"] in m.get("terminal_states", []),
        "transitions": await transitions(conn, execution_id),
        "journal": await journal(conn, execution_id),
        "briefs": await briefs(conn, execution_id),
        "parks": await parks(conn, execution_id),
        "proposals": await proposals(conn, execution_id),
        "verdicts": await verdicts(conn, execution_id),
        "contract": await contract_of(conn, execution_id),
        # F12. Read on EVERY render, not only when DONE: an
        # acceptance that stops being visible makes DONE mean
        # nothing, and the whole reason the refused verdict was
        # left untouched was so both facts could be shown at once.
        "acceptance": await acceptance_of(conn, execution_id),
    }
