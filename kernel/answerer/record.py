"""Everything the executor itself would know, gathered for the answerer.

THIS IS NOT A SUMMARY, IT IS THE SAME SOURCES. A resumed leg rebuilds
its messages from the task, the brief, the standing steers and any human
answer -- there is no retained mind to consult, which is exactly why
`standing_steers` reloads them every leg. So the honest way to answer
"what did the executor decide, and why" is to read what the executor
reads. Anything richer would be inventing a memory the executor does not
have; anything thinner would answer worse than it could.

WHAT IS DELIBERATELY ABSENT. Held-out check specs. They are hidden from
the executor by design (F4), and an answer that quoted one would put it
in front of a person who may relay it straight back -- defeating the
hold-out through the one channel built to be helpful.
"""

from __future__ import annotations

import json

# The journal's tail. Enough to see the shape of the last stretch of
# work; not so much that it crowds out the plan, which is what actually
# explains the work.
RECENT_STEPS = 40


# The argument that IDENTIFIES a step, per tool. A bare tool name says
# almost nothing -- twelve `bash` in a row is what the answerer used to
# be given for a whole diagnostic session -- and the whole args blob
# would crowd out the plan, which is what actually explains the work.
_IDENTIFYING = ("command", "path", "pattern", "job_id", "message",
                "name", "which", "query", "url")


def _moves(rows) -> list:
    """One readable line per journalled step.

    THE DEFECT THIS CLOSES. This section used to render `r["tool"]`
    alone, so a person asking "is it going through each test file
    individually?" was answered from
    `bash, bash, bash, read, bash, bash` -- a question about WHICH
    files, answered with a list that names none of them. Measured on run
    262, where the same question was answerable in one query against the
    `args` column this did not select.

    It is also what the module's own docstring already promised: "the
    honest way to answer what the executor decided, and why, is to read
    what the executor reads". `harness/brief.py`, which IS what the
    executor reads on rehydration, selects `tool, args, cost,
    model_text`. This selected two of the four.

    SAFE TO RENDER. `loop_step` scrubs both `args` and `model_text` on
    the way in (Ec4d), so a credential cannot reach here to be shown to
    a person -- and `test_credential_leak_guards` holds that.
    """
    out = []
    for row in rows:
        try:
            args = json.loads(row["args"]) if row["args"] else {}
        except (TypeError, ValueError):
            args = {}
        detail = ""
        if isinstance(args, dict):
            for key in _IDENTIFYING:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    detail = " ".join(value.split())[:160]
                    break
        out.append(f"{row['tool']} {detail}".rstrip())
    return out


def _plan_prose(payload: dict) -> str:
    steps = payload.get("steps") or []
    out = []
    for i, s in enumerate(steps, 1):
        out.append(f"  {i}. {s}")
    if payload.get("rationale"):
        out += ["", "Why, in its own words:", str(payload["rationale"])]
    checks = payload.get("checks") or []
    if checks:
        out += ["", "What it said would prove the work done:"]
        for c in checks:
            if isinstance(c, dict):
                spec = c.get("spec") or c
                run = spec.get("run") if isinstance(spec, dict) else spec
                out.append(f"  - [{c.get('label', 'unlabelled')}] {run}")
            else:
                out.append(f"  - {c}")
    return "\n".join(out)


async def gather(conn, execution_id: int) -> tuple[str, dict]:
    """`(record_prose, facts)`. `facts` carries what the caller must branch on."""
    e = await conn.fetchrow(
        "SELECT id, state, generation, kind, domain, task, story_id, "
        "goal_id, workspace_path, workspace_host, contract_id, "
        "stall_reason FROM executions WHERE id = $1", execution_id)
    if e is None:
        return "", {}

    parts = [f"Story {e['story_id'] or e['id']} — currently {e['state']}, "
             f"on leg {e['generation'] + 1}."]

    story = None
    if e["story_id"]:
        story = await conn.fetchrow(
            "SELECT title, description FROM stories WHERE id = $1",
            e["story_id"])
    if story and (story["title"] or "").strip():
        parts += ["", "## The story", "", story["title"].strip()]
        if (story["description"] or "").strip():
            parts += ["", story["description"].strip()]

    if (e["task"] or "").strip():
        parts += ["", "## The brief the executor was given", "",
                  e["task"].strip()[:6000]]

    # ── the plan, and whether it is still permission ──────────────────
    #
    # BOTH FACTS MATTER AND THEY COME APART. A plan can be registered,
    # approved, and then spent -- a verdict ends the cycle it authorised
    # -- and the answer to "why is nothing happening?" is entirely
    # different in each case.
    plan_row = await conn.fetchrow(
        "SELECT id, payload::text, status, verdict, declined "
        "FROM questions WHERE execution_id = $1 AND kind = 'authorization' "
        "ORDER BY id DESC LIMIT 1", execution_id)
    if plan_row:
        from kernel.harness.park import plan_is_approved

        payload = json.loads(plan_row["payload"] or "{}")
        live = await plan_is_approved(conn, execution_id)
        if plan_row["status"] != "ANSWERED":
            stance = "waiting for a person to approve it"
        elif plan_row["declined"]:
            stance = "declined by a person"
        elif (plan_row["verdict"] or "approve") != "approve":
            stance = f"sent back by a person ({plan_row['verdict']})"
        elif live:
            stance = "approved, and that approval still stands"
        else:
            stance = ("approved, but that approval has since been spent or "
                      "withdrawn — writes are gated until a new plan is "
                      "approved")
        parts += ["", f"## The plan it registered ({stance})", "",
                  _plan_prose(payload)]
    else:
        parts += ["", "## No plan registered yet",
                  "", "It has not yet said what it intends to do."]

    # ── what done means ───────────────────────────────────────────────
    checks = await conn.fetch(
        "SELECT c.kind, c.label, c.spec::text AS spec FROM criteria c "
        "JOIN executions e ON e.contract_id = c.contract_id "
        "WHERE e.id = $1 AND NOT c.held_out ORDER BY c.id", execution_id)
    if checks:
        parts += ["", "## What has to be true for this to be done", ""]
        for c in checks:
            spec = json.loads(c["spec"] or "{}")
            parts.append(f"  - [{c['label'] or c['kind']}] "
                         f"{spec.get('run') or spec}")

    # ── standing instructions ─────────────────────────────────────────
    from kernel.harness.intake import standing_steers

    steers = await standing_steers(conn, execution_id)
    if steers:
        parts += ["", "## Instructions a person added after it started", ""]
        for i, s in enumerate(steers, 1):
            parts.append(f"  {i}. {s['text'].strip()}")

    # ── what it has actually done ─────────────────────────────────────
    total = await conn.fetchval(
        "SELECT count(*) FROM step_journal WHERE execution_id = $1",
        execution_id)
    recent = await conn.fetch(
        "SELECT tool, args::text, model_text FROM step_journal "
        "WHERE execution_id = $1 ORDER BY seq DESC LIMIT $2",
        execution_id, RECENT_STEPS)
    if total:
        parts += ["", f"## What it has done: {total} steps so far", "",
                  "Its most recent moves, oldest first:", ""]
        parts += [f"  - {line}" for line in _moves(reversed(recent))]
        said = [(r["model_text"] or "").strip() for r in reversed(recent)]
        said = [s for s in said if s]
        if said:
            parts += ["", "## What it said while doing it", ""]
            parts += [f"  - {s[:400]}" for s in said[-8:]]

    # ── verdicts ──────────────────────────────────────────────────────
    verdicts = await conn.fetch(
        "SELECT passed, refusal::text, claim_seq FROM verdicts "
        "WHERE execution_id = $1 ORDER BY id DESC LIMIT 3", execution_id)
    if verdicts:
        parts += ["", "## Verification so far", ""]
        for v in verdicts:
            if v["passed"]:
                parts.append(f"  - claim {v['claim_seq']}: PASSED its checks")
            else:
                parts.append(f"  - claim {v['claim_seq']}: REFUSED — "
                             f"{(v['refusal'] or '')[:600]}")

    if (e["stall_reason"] or "").strip():
        parts += ["", "## It is stalled", "", e["stall_reason"].strip()]

    # `workspace_host` travels with the path because the answerer does
    # not run where the workspace is. It runs in the conductor, and on
    # Kubernetes the claim is ReadWriteOncePod and belongs to the
    # execution's pod -- so the path alone is an invitation to read the
    # wrong disk, which is exactly what `_tree` did.
    facts = {"state": e["state"], "goal_id": e["goal_id"],
             "story_id": e["story_id"], "workspace_path": e["workspace_path"],
             "workspace_host": e["workspace_host"],
             "contract_id": e["contract_id"], "generation": e["generation"]}
    return "\n".join(parts), facts
