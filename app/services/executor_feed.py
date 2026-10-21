"""The ticker's queries. READ ONLY, and two sources rather than one.

WHY TWO. `step_journal` is the EXECUTOR's log, and for most of a run that is
the whole story. But when the executor claims it is done the run moves to
VERIFYING, the executor stops, and the adjudicator takes over in a clean
container — writing nothing to `step_journal`. A ticker reading only that
table goes blank for the whole of verification, which on this database ran
four minutes. Blank is exactly the state the ticker exists to disprove, and
it lands at the worst moment: right when a person is waiting to hear whether
the work passed.

WHAT VERIFICATION CAN AND CANNOT SHOW. It cannot be streamed check by check.
Every `criterion_results` row for one claim carries the SAME timestamp — the
adjudicator runs the whole set and commits once — so there is no partial
progress recorded anywhere, and inventing a spinner per check would be
describing something the database does not know. What it can do is say how
many checks are running, and then emit each outcome the moment the batch
lands. That is honest and it is enough.

Both cursors are opaque to the caller and independent: steps advance by
`seq`, checks by row `id`.
"""
from __future__ import annotations

import json

from app.services.executor_activity import HIDDEN, summarise

_HIDDEN = sorted(HIDDEN)


async def steps_since(conn, execution_id: int, after_seq: int = 0,
                      limit: int = 60) -> tuple[list[dict], int]:
    """Visible tool calls after `after_seq`, oldest first, and how far we read.

    The cursor returned is the highest sequence SCANNED, not the highest
    shown. Those differ whenever the newest row is a hidden one — a
    `boundary_prompt` closing a leg, which is the single most common last
    row — and returning the highest shown would re-read that row on every
    poll for as long as the leg lasts.
    """
    rows = await conn.fetch(
        """SELECT seq, tool, args, ts
             FROM kernel.step_journal
            WHERE execution_id = $1 AND seq > $2
         ORDER BY seq
            LIMIT $3""",
        execution_id, int(after_seq or 0), limit)
    if not rows:
        return [], int(after_seq or 0)
    return _as_steps(rows), rows[-1]["seq"]


async def steps_tail(conn, execution_id: int,
                     count: int = 8) -> tuple[list[dict], int]:
    """The last few VISIBLE tool calls, oldest first — for switching on.

    Turning the ticker on used to jump straight to the tail cursor and wait
    for the next call, so it showed nothing at all until the executor acted
    again. Between legs, or parked, or mid-verification, that is a long time
    to look at an empty strip and conclude the feature is broken. A short
    backlog costs one extra query once and answers "is this thing working"
    immediately.

    Hidden tools are excluded in SQL rather than after the fetch, so `count`
    is a count of lines a person will actually see.
    """
    rows = await conn.fetch(
        """SELECT seq, tool, args, ts
             FROM kernel.step_journal
            WHERE execution_id = $1 AND tool IS NOT NULL AND tool <> ''
              AND NOT (tool = ANY($2::text[]))
         ORDER BY seq DESC
            LIMIT $3""",
        execution_id, _HIDDEN, max(1, count))
    high = await conn.fetchval(
        "SELECT max(seq) FROM kernel.step_journal WHERE execution_id = $1",
        execution_id)
    return _as_steps(list(reversed(rows))), int(high or 0)


def _as_steps(rows) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        got = summarise(r["tool"], r["args"])
        if got is None:
            continue
        got["seq"] = r["seq"]
        got["at"] = r["ts"].isoformat() if r["ts"] else None
        out.append(got)
    return out


async def checks_since(conn, execution_id: int, after_id: int = 0,
                       limit: int = 40) -> tuple[list[dict], int]:
    """Verification outcomes after `after_id`, as ticker lines.

    One line per criterion per claim, not per ROW: a flaky check is re-run
    and each attempt is its own row, so criterion 537 on this database has
    three identical failures. Showing all three says nothing the first does
    not, so the highest `run_index` — the attempt that settled it — wins.

    Deduplicated here rather than in SQL because `DISTINCT ON` and a row-id
    cursor do not compose: the cursor must advance past every row read,
    including the ones collapsed away.
    """
    rows = await conn.fetch(
        """SELECT r.id, r.criterion_id, r.claim_seq, r.run_index, r.passes,
                  c.label, c.spec
             FROM kernel.criterion_results r
             JOIN kernel.criteria c ON c.id = r.criterion_id
            WHERE r.execution_id = $1 AND r.id > $2
         ORDER BY r.id
            LIMIT $3""",
        execution_id, int(after_id or 0), limit)
    if not rows:
        return [], int(after_id or 0)

    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["criterion_id"], r["claim_seq"])
        if key in best and best[key]["run_index"] >= (r["run_index"] or 0):
            continue
        best[key] = {"run_index": r["run_index"] or 0, "row": r}

    out = [_as_check(v["row"]) for _, v in sorted(
        best.items(), key=lambda kv: kv[1]["row"]["id"])]
    return out, rows[-1]["id"]


def _as_check(r) -> dict:
    passed = str(r["passes"] or "").lower() == "pass"
    return {
        "tool": f"check {r['label'] or 'check'}",
        "detail": _command_of(r["spec"]),
        # Its own families, not a tool's. A verification outcome is the one
        # thing in this feed a person is waiting on, and it should not have
        # to be told apart from a commit by reading the text.
        "family": "pass" if passed else "fail",
        "check_id": r["id"],
    }


def _command_of(spec) -> str:
    """The check's command, for a person who wants to know WHICH check."""
    from app.services.executor_activity import clip

    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except (TypeError, ValueError):
            return clip(spec)
    if isinstance(spec, dict):
        return clip(spec.get("run") or spec.get("cmd") or "")
    return ""


async def criteria_count(conn, execution_id: int) -> int:
    """How many checks this run's contract has — the "verifying · 5 checks".

    Zero is a legitimate answer and the caller must render it as such rather
    than as an error: a contract can exist before its criteria are written.
    """
    return int(await conn.fetchval(
        """SELECT count(*) FROM kernel.criteria c
             JOIN kernel.executions e ON e.contract_id = c.contract_id
            WHERE e.id = $1""", execution_id) or 0)
