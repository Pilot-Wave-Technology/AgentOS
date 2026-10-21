"""Null executor — scripted, no LLM, one flag per failure mode.

C2 increment of Stage C, updated by C5 (journal idempotency keys),
C7 (heartbeat loop + generation fencing), E3 (synthetic token usage),
and E5 (--propose-without-commit).

A deterministic executor that connects to the E0 database, INSERTs into
kernel.step_journal, optionally heartbeats its lease, and exits.

C7 changes:
- Heartbeat loop (opt-in via --holder): background task refreshes the lease
  at --heartbeat-interval-ms.  Zero-rowcount → fenced_event → exit 3.
- --stop-heartbeating: disables the heartbeat loop (lease goes stale, but
  nothing observes it — the detector is out of scope at C7).
- Trigger rejection (step_journal_generation_fence) caught as a fencing
  signal: FENCED_WRITE marker, fenced_event set, exit 3.
- Inter-step sleep is interruptible via fenced_event.wait().

E3 changes:
- --usage-per-step <json>: synthetic token usage written to step_journal.cost
  on each step.  Field names match NormalizedUsage: input, output, cacheRead,
  cacheWrite, thinking (optional).  Omitted = cost stays NULL.
- --budget-basis <measured|estimated>: the adapter's declared capability.
  Required when --usage-per-step is provided.  The test fixture sets both
  this flag AND passes basis to check_token_budget() — the flag does not
  reach the harness through any channel (see plan §"How the harness learns
  budget_basis").

E5 changes:
- --propose-without-commit: after all steps complete, calls propose_completion
  with an empty reason set.  This is E5's half of D6.  D6's full assertion
  completes at F2b — refusing a claim with an unchanged reason set requires
  a verdict, which does not exist until F2b writes one.  The flag lands here;
  the chaos-suite scenario that tests refusal after a verdict is deferred to F2b.

Exit codes: 0 = success, 1 = crash/die-at-step, 2 = refusal/bad args,
3 = fenced (heartbeat returned 0 rows or trigger rejected a stale write).

Usage:
    python3 kernel/null_executor.py --execution-id <int> --generation <int> \
        [--holder <str>] [--steps N] [--interval-ms N] [chaos flags...]

Chaos flags are either wired (behaviour implemented here) or not-implemented
(exit 2 with a message naming the increment that adds the mechanism).
"""

import argparse
import asyncio
from contextlib import suppress
import json
import os
import sys

import asyncpg

# propose_completion is imported lazily in _propose_after_steps() because
# the null executor runs as a script from kernel/null_executor.py and
# sys.path does not include the repo root.  The lazy import adds /app
# to sys.path first, matching how test files do it.


# ── Unimplemented flag registry ──────────────────────────────────────────────
# Each maps the flag name to the increment that adds its mechanism.
# Checked BEFORE connecting to the database — no journal rows are written.
_NOT_IMPLEMENTED = {
    "park_mid_effect": "D8",
    # propose_without_commit: removed from NOT_IMPLEMENTED at E5.
    # The flag is now wired — see _propose_after_steps().
    "ask_and_never_answer": "E/F",
}


# ── Heartbeat ────────────────────────────────────────────────────────────────

async def _heartbeat(conn, execution_id: int, holder: str, generation: int) -> int:
    """Send one heartbeat.  Returns rowcount.  0 = fenced."""
    result = await conn.execute(
        "UPDATE execution_leases SET heartbeat_at = now() "
        "WHERE execution_id = $1 AND holder = $2 AND generation = $3 "
        "AND released_at IS NULL",
        execution_id, holder, generation,
    )
    return int(result.split()[-1])


async def _heartbeat_loop(
    conn,
    execution_id: int,
    holder: str,
    generation: int,
    interval_ms: int,
    fenced_event: asyncio.Event,
) -> None:
    """Background heartbeat task.  Sets fenced_event on zero rowcount."""
    while not fenced_event.is_set():
        await asyncio.sleep(interval_ms / 1000.0)
        try:
            rowcount = await _heartbeat(conn, execution_id, holder, generation)
        except Exception:
            # Connection error — treat as fenced to be safe.
            rowcount = 0
        if rowcount == 0:
            print(
                f"HEARTBEAT_FENCED: generation={generation}",
                flush=True,
            )
            fenced_event.set()
            return


# ── Argument parsing ─────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Null executor — C2/C5/C7")
    p.add_argument("--execution-id", type=int, required=True)
    p.add_argument("--generation", type=int, required=True)
    p.add_argument("--steps", type=int, default=5)
    p.add_argument("--interval-ms", type=int, default=200)

    # Wired flags
    p.add_argument("--die-at-step", type=int, default=None)
    p.add_argument("--hang-after", type=int, default=None)
    p.add_argument("--duplicate-idempotency", action="store_true")
    p.add_argument("--stale-generation", action="store_true")
    p.add_argument("--seq-race-delay-ms", type=int, default=0)

    # C7 flags
    p.add_argument("--holder", type=str, default=None,
                    help="Lease holder identity.  Enables heartbeat loop.")
    p.add_argument("--heartbeat-interval-ms", type=int, default=500)
    p.add_argument("--stop-heartbeating", action="store_true",
                    help="Disable heartbeat loop (lease goes stale).")

    # E3 flags — synthetic token usage for harness predicate testing
    p.add_argument("--usage-per-step", type=str, default=None,
                    help='JSON object per NormalizedUsage, e.g. \'{"input":100,"output":50}\'')
    p.add_argument("--budget-basis", type=str, default=None,
                    choices=["measured", "estimated"],
                    help="Adapter capability declaration (required with --usage-per-step).")

    # Not-implemented flags
    p.add_argument("--park-mid-effect", action="store_true")
    p.add_argument("--propose-without-commit", action="store_true")
    p.add_argument("--ask-and-never-answer", action="store_true")

    return p.parse_args()


def _check_unimplemented(args: argparse.Namespace) -> None:
    """Exit 2 if any not-implemented flag is set. Runs before DB connect."""
    for attr, increment in _NOT_IMPLEMENTED.items():
        if getattr(args, attr, False):
            flag = attr.replace("_", "-")
            print(
                f"ERROR: --{flag} not implemented until {increment}",
                file=sys.stderr,
            )
            sys.exit(2)


def _check_stale_generation_guard(args: argparse.Namespace) -> None:
    """--stale-generation with --generation 0 is nonsense, not stale."""
    if args.stale_generation and args.generation == 0:
        print(
            "ERROR: --stale-generation requires --generation >= 1",
            file=sys.stderr,
        )
        sys.exit(2)


# ── Main run loop ────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    dsn = os.environ["DATABASE_URL"]

    # Canonical connection style — C4 onward copies this pattern.
    conn = await asyncpg.connect(dsn, server_settings={"search_path": "kernel,public"})

    fenced_event = asyncio.Event()
    heartbeat_task = None

    try:
        generation = args.generation
        if args.stale_generation:
            generation = args.generation - 1

        # ── Start heartbeat loop (opt-in via --holder) ───────────────────
        # No holder → no lease → no heartbeat → existing tests untouched.
        # --stop-heartbeating disables the loop even when --holder is set
        # (the lease exists but nothing refreshes it — isolates the trigger
        # detector for test 5b).
        if args.holder is not None and not args.stop_heartbeating:
            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(
                    conn,
                    args.execution_id,
                    args.holder,
                    generation,
                    args.heartbeat_interval_ms,
                    fenced_event,
                )
            )

        # Each step autocommits — do NOT wrap in conn.transaction().
        # --die-at-step tests committed-steps-survive-crash (D1); a transaction
        # would make that test pass vacuously with zero rows.
        for step_index in range(args.steps):
            # ── Pre-INSERT fence check ───────────────────────────────────
            if fenced_event.is_set():
                print("FENCED: exiting before step INSERT", flush=True)
                sys.exit(3)

            idem_key = f"null-exec-{args.execution_id}-step-{step_index}"

            # --duplicate-idempotency: step 1 reuses step 0's key
            is_deliberate_dup = args.duplicate_idempotency and step_index == 1
            if is_deliberate_dup:
                idem_key = f"null-exec-{args.execution_id}-step-0"

            # Store-allocated seq (S1): read MAX from the store, not process memory.
            # A restarted process picks up from where the journal left off.
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM step_journal WHERE execution_id = $1",
                args.execution_id,
            )

            # Announce the allocated seq — makes the allocation point observable.
            # Used by test 15's handshake (poll for this line, inject at the
            # announced seq). Also useful for D-stage concurrent-allocation scenarios.
            print(f"SEQ_ALLOCATED: {seq}", flush=True)

            # --seq-race-delay-ms: sleep between MAX read and INSERT to allow
            # deterministic collision injection from the test harness.
            if args.seq_race_delay_ms > 0:
                await asyncio.sleep(args.seq_race_delay_ms / 1000.0)

            # ON CONFLICT (execution_id, idempotency_key) DO NOTHING:
            # - Row returned → new step committed
            # - None returned → already committed (replay), skip silently
            # A seq collision (concurrent race) still raises UniqueViolationError
            # because the conflict target names only the idempotency index.
            # The step_journal_generation_fence trigger (C7) rejects writes at a
            # stale generation — caught below as a fencing signal.
            # E3: synthetic token usage — written to step_journal.cost when
            # --usage-per-step is provided.  Omitted = NULL (existing tests
            # unchanged).  Field names match NormalizedUsage.
            cost_json = args.usage_per_step  # already validated JSON or None

            try:
                row_id = await conn.fetchval(
                    """
                    INSERT INTO step_journal
                        (execution_id, seq, generation, idempotency_key, tool, args, cost)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    ON CONFLICT (execution_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    args.execution_id,
                    seq,
                    generation,
                    idem_key,
                    "null_step",
                    json.dumps({"step": seq, "probe": 'he said "hi"'}),
                    cost_json,
                )
            except asyncpg.exceptions.RaiseError as e:
                if "step_journal generation fence" in str(e):
                    print(f"FENCED_WRITE: {e}", flush=True)
                    fenced_event.set()
                    break  # falls through to exit 3 below
                raise

            if row_id is None:
                # Step was already committed — resume skip.
                if is_deliberate_dup:
                    # Preserve DUPLICATE_REFUSED marker for test 9 / D3.
                    print(f"DUPLICATE_REFUSED: idempotency_key={idem_key}", flush=True)
                print(f"SKIP_COMPLETED: idempotency_key={idem_key}", flush=True)
                continue

            print(
                f"step {step_index + 1}/{args.steps} committed (seq {seq})",
                flush=True,
            )

            # ── Post-INSERT fence check ──────────────────────────────────
            if fenced_event.is_set():
                print("FENCED: exiting after step INSERT", flush=True)
                sys.exit(3)

            # --die-at-step N: unclean crash after inserting step N (1-based).
            # Keyed on step_index+1 so the flag keeps its 1-based meaning:
            # --die-at-step 2 = "die after the second step" = 2 rows.
            if args.die_at_step is not None and step_index + 1 == args.die_at_step:
                os._exit(1)

            # --hang-after N: hang forever after inserting step N (1-based).
            if args.hang_after is not None and step_index + 1 == args.hang_after:
                await asyncio.sleep(86400)

            # Sleep between steps (skip after the last one).
            # The sleep is interruptible: if the heartbeat sets fenced_event,
            # wait_for wakes us ~heartbeat-interval-ms later instead of
            # waiting the full step interval.
            if step_index < args.steps - 1:
                try:
                    await asyncio.wait_for(
                        fenced_event.wait(),
                        timeout=args.interval_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    pass  # normal interval elapsed

                # Check after waking — either timeout or event fired.
                if fenced_event.is_set():
                    print("FENCED: exiting during inter-step sleep", flush=True)
                    sys.exit(3)

    finally:
        # Cancel the heartbeat task if it's running.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        await conn.close()

    # If fenced_event was set by the trigger catch (break from loop),
    # exit 3 here.
    if fenced_event.is_set():
        print("FENCED: exiting after trigger rejection", flush=True)
        sys.exit(3)

    # ── E5: --propose-without-commit ─────────────────────────────────────
    # After all steps complete, call propose_completion with an empty reason
    # set.  This is E5's half of D6 — the flag lets the null executor make a
    # claim.  The refusal-after-verdict scenario defers to F2b.
    if args.propose_without_commit:
        print("PROPOSE_COMPLETION: calling with empty reason set", flush=True)
        # Lazy import — sys.path may not include the repo root when running
        # as a script.  Add it so kernel.harness.* is resolvable.
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from kernel.harness.propose_completion import propose_completion
        conn2 = await asyncpg.connect(dsn, server_settings={"search_path": "kernel,public"})
        try:
            result = await propose_completion(
                conn2,
                execution_id=args.execution_id,
                actor_id=f"null-executor-{args.execution_id}",
                reason_set=[],
                idempotency_key=f"completion:{args.execution_id}:0",
            )
            print(
                f"PROPOSE_RESULT: accepted={result.accepted}, "
                f"proposal_id={result.proposal_id}, message={result.message}",
                flush=True,
            )
        finally:
            await conn2.close()

    # Simulated work artifact — container-local, not load-bearing past C2.
    artifact = f"/tmp/null-exec-{args.execution_id}.done"
    with open(artifact, "w") as f:
        f.write(f"execution_id={args.execution_id} steps={args.steps}\n")

    print(f"null executor finished: {args.steps} steps, artifact={artifact}", flush=True)


def main() -> None:
    args = _parse_args()

    # Unimplemented flags exit 2 BEFORE connecting to the database.
    _check_unimplemented(args)

    # --stale-generation with --generation 0 is nonsense.
    _check_stale_generation_guard(args)

    # --duplicate-idempotency fires at step_index 1; with fewer than 2 steps the
    # flag is inert and the run looks like an ordinary happy path.
    if args.duplicate_idempotency and args.steps < 2:
        print(
            "ERROR: --duplicate-idempotency requires --steps >= 2",
            file=sys.stderr,
        )
        sys.exit(2)

    # E3: --usage-per-step requires --budget-basis (and vice versa for legibility).
    if args.usage_per_step is not None and args.budget_basis is None:
        print(
            "ERROR: --usage-per-step requires --budget-basis",
            file=sys.stderr,
        )
        sys.exit(2)

    # Validate JSON early — before connecting to the database.
    if args.usage_per_step is not None:
        try:
            json.loads(args.usage_per_step)
        except json.JSONDecodeError as e:
            print(
                f"ERROR: --usage-per-step is not valid JSON: {e}",
                file=sys.stderr,
            )
            sys.exit(2)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
