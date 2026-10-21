"""Trigger a goal review from a shell, and watch it.

    python -m scripts.council_run CAL05            # enqueue, dedup applies
    python -m scripts.council_run CAL05 --force    # clear the way first
    python -m scripts.council_run CAL05 --watch    # follow the stages
    python -m scripts.council_run --status CAL05   # last run, no trigger
    python -m scripts.council_run --findings CAL05 # the open ledger

WHY NOT JUST THE ENDPOINT. `POST /goals/{pid}/council/run` needs an admin
session, which makes "review this goal now" a browser task. Testing the
council means running it repeatedly against a chosen goal and reading
what came out, and that wants to be one command.

--force IS THE POINT OF THIS SCRIPT. The global single-run index means a
run wedged on any other goal blocks the one you want, and the plain
trigger answers 409 in a way that reads like a broken button rather than
a queue held elsewhere. `force_run` cancels the holder and says so.

Reads DATABASE_URL from the environment, like every other script here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _pool():
    from app.database import init_db
    return await init_db()


async def _show(pool, goal_id: str) -> str:
    run = await pool.fetchrow(
        "SELECT id, status, trigger, created_at, finished_at "
        "FROM council_review_runs WHERE goal_id = $1 "
        "ORDER BY created_at DESC LIMIT 1", goal_id)
    if not run:
        print(f"{goal_id}: no council run has ever been enqueued")
        return "none"
    print(f"\nrun {run['id'][:8]}  {run['trigger']}/{run['status']}  "
          f"created {run['created_at']:%Y-%m-%d %H:%M:%S}"
          + (f"  finished {run['finished_at']:%H:%M:%S}" if run["finished_at"] else ""))
    stages = await pool.fetch(
        "SELECT stage, status, error, length(COALESCE(output::text,'')) AS chars "
        "FROM council_review_stages WHERE run_id = $1 ORDER BY created_at",
        run["id"])
    for s in stages:
        line = f"  {s['stage']:<12} {s['status']:<16} {s['chars']:>6} chars"
        if s["error"]:
            line += f"  — {s['error'][:90]}"
        print(line)
    return run["status"]


async def _findings(pool, goal_id: str) -> None:
    rows = await pool.fetch(
        "SELECT key, severity, scope, story_id, times_seen, status, title "
        "FROM council_findings WHERE goal_id = $1 "
        "ORDER BY status, times_seen DESC, key", goal_id)
    if not rows:
        print(f"{goal_id}: the findings ledger is empty")
        return
    print(f"\n{len(rows)} finding(s) for {goal_id}:")
    for r in rows:
        where = f"story:{r['story_id']}" if r["story_id"] else "goal"
        print(f"  [{r['key']}] {r['status']:<9} {r['severity']:<7} {where:<16}"
              f" seen {r['times_seen']}x  {r['title'][:70]}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Trigger and watch a goal review.")
    ap.add_argument("goal_id")
    ap.add_argument("--force", action="store_true",
                    help="cancel this goal's active run AND whatever holds "
                         "the global single-run slot, then enqueue")
    ap.add_argument("--watch", action="store_true",
                    help="poll until the run reaches a terminal state")
    ap.add_argument("--status", action="store_true",
                    help="show the last run and exit, triggering nothing")
    ap.add_argument("--findings", action="store_true",
                    help="show the findings ledger and exit")
    args = ap.parse_args()

    pool = await _pool()

    if args.findings:
        await _findings(pool, args.goal_id)
        return 0
    if args.status:
        await _show(pool, args.goal_id)
        return 0

    from app.services.council.runs import enqueue_run, force_run

    result = await (force_run(args.goal_id, created_by="scripts.council_run")
                    if args.force
                    else enqueue_run(args.goal_id, created_by="scripts.council_run"))
    if result.get("cancelled_runs"):
        print(f"cancelled {result['cancelled_runs']} in-flight run(s)")
    if not result.get("created"):
        print(f"not enqueued: {result.get('reason')}")
        print("(--force clears both the per-goal and the global block)")
        return 1
    print(f"enqueued run {result['run_id'][:8]} for {args.goal_id}")

    if not args.watch:
        return 0

    # The dispatcher polls roughly every 20s and a stage is a Flash turn
    # over documents, so a full run is minutes. Poll slowly; the deadline
    # sweep is what guarantees this terminates rather than this loop.
    for _ in range(180):
        await asyncio.sleep(10)
        status = await _show(pool, args.goal_id)
        if status in ("done", "failed"):
            await _findings(pool, args.goal_id)
            return 0 if status == "done" else 1
    print("still running after 30 minutes — check the dispatcher logs")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
