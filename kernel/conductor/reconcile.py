"""Reconcile — compare database state against live substrate containers.

Reads park_states and terminal_states from run_state_machine (not hardcoded).
Returns an observable list of action records so tests can assert on exactly
what the reconciler did.

Called independently from conductor_tick — the reconciler's tick rate and the
admit/dispatch tick rate may differ.  Tests call this function in a loop.
"""

from __future__ import annotations

import json

from kernel.conductor.orphans import orphan_sweep


async def reconcile_tick(
    conn,
    driver,
    goal_id: str,
    machine_version: int = 1,
    *,
    sweep_orphans: bool = True,
    dry_run: bool = False,
) -> list[dict]:
    """One tick of reconciliation.  Returns list of action dicts.

    Actions: "recover", "reap", "release", "demote", "skip", "healthy",
    "waiting", "unhandled".

    F2c added `release`, and with it the rule that makes the whole list
    testable: **no state may be invisible to both loops.** Every state in
    `run_state_machine` must produce a named disposition here, and
    `unhandled` must be EMPTY. It was not: `VERIFYING` fell through to it,
    and so did a healthy `RUNNING`, so "we handle everything" was a claim
    nothing checked.

    Args:
        conn: asyncpg connection (search_path includes kernel).
        driver: substrate Driver instance with list_containers/destroy.
        goal_id: the goal to reconcile.
        machine_version: run_state_machine version to read park/terminal
            states. DEFAULTS TO 1 and `executions.machine_version`
            defaults to 5 — a production caller must pass the row's own
            version, which is what kernel/conductor/sweep.py does.
        sweep_orphans: run section 4. TRUE preserves the behaviour every
            existing caller relies on. `reconcile_all` passes False and
            runs ONE global sweep afterwards, because the sweep's
            keep-set is global and running it inside a per-goal pass
            means each run sees every container on the host with only
            one goal's executions to account for them.
        dry_run: count, do not destroy. See sweep.py for why that is the
            default there and not here.

    Returns:
        List of {"action": str, "execution_id": int|None, "detail": str}.
    """
    actions = []

    # ── 1. Read park_states + terminal_states from run_state_machine ──────
    # No hardcoded state names.  Proven by test 7 (version-2 machine).
    machine = await conn.fetchrow(
        "SELECT park_states, terminal_states "
        "FROM run_state_machine WHERE version = $1",
        machine_version,
    )
    park_states = set(json.loads(machine["park_states"]))
    terminal_states = json.loads(machine["terminal_states"])

    # ── 2. List live containers from substrate ────────────────────────────
    # list_containers uses GET /containers/json WITHOUT all=true, so it
    # returns running containers only.  That default is load-bearing:
    # a killed container reads as "compute gone" and drives recovery.
    # Adding all=true would make has_compute stay true for dead containers
    # and silently break recovery.  Reaping stopped containers belonging
    # to VERIFYING executions is F2c's job (BUILD_ORDER: "excluded from
    # reconcile and unknown to sweep").
    live_containers = await driver.list_containers()
    live_cids = {c["id"] for c in live_containers}

    # NO execution-id → container MAP HERE ANY MORE. One was built and
    # never read, and `int(eid_label)` inside it raised on any label that
    # was not digits -- which would have taken down the whole tick, every
    # tick, on one mislabelled pod. Section 3b works from
    # `executions.container_id` and section 4 reads the labels itself
    # through `kernel/ownership.py`, which parses without raising.

    # ── 3. Query non-terminal executions for this goal ────────────────────
    rows = await conn.fetch(
        "SELECT id, state, container_id FROM executions "
        "WHERE goal_id = $1 AND state != ALL($2::text[])",
        goal_id, terminal_states,
    )

    db_cids = set()  # container_ids belonging to known executions

    for r in rows:
        eid, state, cid = r["id"], r["state"], r["container_id"]
        if cid:
            db_cids.add(cid)
        has_compute = cid is not None and cid in live_cids

        # ── Park check (from table, not hardcoded) ────────────────────
        if state in park_states:
            if state == "PARKED_HOT" and not has_compute:
                # Compute vanished — demote to PARKED_COLD.
                await conn.execute(
                    "UPDATE executions SET state = 'PARKED_COLD', "
                    "container_id = NULL, updated_at = now() "
                    "WHERE id = $1 AND state = 'PARKED_HOT'",
                    eid,
                )
                actions.append({
                    "action": "demote",
                    "execution_id": eid,
                    "detail": "PARKED_HOT -> PARKED_COLD (compute vanished)",
                })
            else:
                # Parked with or without compute — leave alone.
                actions.append({
                    "action": "skip",
                    "execution_id": eid,
                    "detail": f"parked ({state}), no action",
                })
            continue

        # ── RUNNING with no compute — crashed ─────────────────────────
        if state == "RUNNING" and not has_compute:
            # Move to ADMITTED for re-dispatch.  The admission slot is
            # still held — skip re-admission.  Generation advanced on
            # recovery (C7/S3) — fences the old holder's writes and
            # heartbeats.  Wrapped in a transaction so the state change,
            # generation advance, and lease release are atomic.
            async with conn.transaction():
                await conn.execute(
                    "UPDATE executions SET generation = generation + 1, "
                    "state = 'ADMITTED', container_id = NULL, updated_at = now() "
                    "WHERE id = $1 AND state = 'RUNNING'",
                    eid,
                )
                # Release the lease so dispatch can claim a fresh one.
                await conn.execute(
                    "UPDATE execution_leases SET released_at = now() "
                    "WHERE execution_id = $1 AND released_at IS NULL",
                    eid,
                )
            actions.append({
                "action": "recover",
                "execution_id": eid,
                "detail": "RUNNING -> ADMITTED (container gone)",
            })
            continue

        # ── DISPATCHING with no compute — stuck ───────────────────────
        if state == "DISPATCHING" and not has_compute:
            # Generation advanced on recovery (C7/S3) — same as RUNNING
            # path above.  DISPATCHING may not have a lease yet, but the
            # generation advance still fences any partial work.
            async with conn.transaction():
                await conn.execute(
                    "UPDATE executions SET generation = generation + 1, "
                    "state = 'ADMITTED', container_id = NULL, updated_at = now() "
                    "WHERE id = $1 AND state = 'DISPATCHING'",
                    eid,
                )
            actions.append({
                "action": "recover",
                "execution_id": eid,
                "detail": "DISPATCHING -> ADMITTED (container gone)",
            })
            continue

        # ── VERIFYING — release the compute, keep the slot (F2c, R2) ──
        # A stopped container belonging to a VERIFYING execution was
        # touched by NEITHER loop: excluded from reconcile as a
        # non-dispatchable state, unknown to the sweep because its
        # execution exists. Resources leaked silently, in the same class
        # as the orphan the sweep exists to prevent. Eight of them were
        # up on a laptop when this was found.
        #
        # A LIVE SANDBOX SURVIVES VERIFICATION. A DEAD ONE IS CLEANED UP.
        #
        # This destroyed the container unconditionally, reasoning that
        # "the Verifier runs its own ephemeral container against the
        # COMMITTED tree, so the executor's sandbox is dead weight from
        # the moment the claim is made". True of adjudication and false
        # of everything that happens next:
        #
        #   a refused claim returns VERIFYING -> RUNNING, and that is a
        #   real edge -- so every rejection paid for a full pod rebuild
        #
        #   the acceptance park that follows a PASS asks a person to
        #   accept or ask for more, and they were asked it with no
        #   workspace to look at. The file explorer reads the pod; the
        #   pod had been destroyed at the moment somebody most wants to
        #   see the tree
        #
        # `kernel/conductor/reclaim.py` argues this in its own docstring
        # -- "tearing down there costs a full pod rebuild on every
        # rejection, and blanks the assistant panel at exactly the
        # moment somebody is watching for the outcome" -- and then the
        # reconciler tore it down anyway. Errands never reach this
        # branch (their self-check runs in-leg and they park straight
        # onto acceptance), so a story and an errand behaved
        # differently for no reason either could name.
        #
        # THE LEAK THIS BRANCH WAS WRITTEN FOR IS STILL CLOSED. That was
        # a STOPPED container in VERIFYING, touched by neither loop --
        # eight of them on a laptop. `has_compute` is false for exactly
        # those, and nulling `container_id` hands them to the orphan
        # sweep, which is what removed them. Killing LIVE sandboxes was
        # never the part that fixed it.
        #
        # So the reference is dropped only when the compute is already
        # gone: a live pod keeps its `container_id` or the sweep would
        # reap the thing this branch just decided to keep.
        #
        # THE COST, STATED: a sandbox idles from the claim until a
        # person accepts, which can be hours. That is the cost an errand
        # has always paid on its acceptance park, and the pod goes the
        # moment the story reaches a terminal state (section 3b below)
        # or the reclaim pass takes it.
        #
        # Release the lease: nothing holds this execution now, and a
        # live lease would fence the worker that picks it up if the
        # claim is refused.
        #
        # HOLD the admission slot. S7 lists a slot as released by
        # completion, park, cancel and failure — not by verifying — and
        # releasing it here would let newer work take the slot while a
        # refused execution waits behind it for one to come free. The
        # run is not finished; it is being judged.
        if state == "VERIFYING":
            async with conn.transaction():
                if not has_compute:
                    await conn.execute(
                        "UPDATE executions SET container_id = NULL, "
                        "updated_at = now() "
                        "WHERE id = $1 AND state = 'VERIFYING'",
                        eid,
                    )
                await conn.execute(
                    "UPDATE execution_leases SET released_at = now() "
                    "WHERE execution_id = $1 AND released_at IS NULL",
                    eid,
                )
            actions.append({
                "action": "release",
                "execution_id": eid,
                # SAYS WHAT IT DID, and it no longer destroys. The old
                # text read "container destroyed" because it was; a
                # summary that keeps claiming a teardown nobody performs
                # is the kind of line somebody reads instead of looking.
                "detail": ("lease released, admission slot HELD; sandbox "
                           + ("kept for the acceptance park"
                              if has_compute
                              else "was already gone, reference cleared")),
            })
            continue

        # ── Nothing to do, said explicitly ────────────────────────────
        # `unhandled` must stay empty, so a state with no work is a
        # NAMED disposition rather than a fall-through. A healthy RUNNING
        # execution reported as "unhandled" every tick is how a real gap
        # hides: in a column that is never empty, nobody reads it.
        if state in ("RUNNING", "DISPATCHING") and has_compute:
            actions.append({
                "action": "healthy",
                "execution_id": eid,
                "detail": f"{state} with live compute",
            })
            continue
        if state in ("PENDING", "ADMITTED"):
            actions.append({
                "action": "waiting",
                "execution_id": eid,
                "detail": f"{state}; admission and dispatch own this, "
                          f"not reconcile",
            })
            continue

        # ── Genuinely unknown ─────────────────────────────────────────
        # A state the machine declares and this loop has never been
        # taught. Empty is the assertion; a row here is the bug.
        actions.append({
            "action": "unhandled",
            "execution_id": eid,
            "detail": f"state={state}, has_compute={has_compute}",
        })

    # ── 3b. Terminal executions that still hold compute ───────────────────
    # A hole both loops had, found while building F12. Section 3 queries
    # NON-terminal executions, and section 4 skips any container whose
    # execution still exists — so a container attached to a DONE or
    # CANCELLED run is invisible to both, which is precisely the rule
    # F2c exists to enforce read from the other side.
    #
    # The verified path used to escape it by accident: a story reaching
    # DONE went through VERIFYING, and F2c's `release` had already
    # destroyed the container. Nothing else did. CANCEL FROM RUNNING HAS
    # ALWAYS LEAKED, and F12's accept-from-RUNNING would have leaked the
    # same way — the second instance is what made it visible.
    #
    # THAT ACCIDENT IS GONE AND THIS IS NOW THE PRIMARY PATH. `release`
    # keeps a live sandbox through verification and the acceptance park,
    # because the person deciding "done, or more work" needs the tree in
    # front of them and a refused claim should not pay for a rebuild. So
    # every story's pod now leaves HERE, when the run reaches a terminal
    # state -- accepted, failed or cancelled -- which is also the promise
    # made to the person: the sandbox lasts until the run does.
    #
    # The lease goes with it. A terminal run holds nothing.
    #
    # ITS AUTHORITY IS THE ROW, not a label: this destroys only what an
    # execution says it is holding in `container_id`, which is the same
    # ownership `reclaim.py` acts on and the reason it needs no pod
    # predicate. A capture Job's pod is never written there -- and if a
    # future dispatch path wrote one, this would inherit execution 208's
    # bug through the front door.
    finished = await conn.fetch(
        "SELECT id, state, container_id FROM executions "
        "WHERE goal_id = $1 AND state = ANY($2::text[]) "
        "AND container_id IS NOT NULL",
        goal_id, terminal_states,
    )
    for r in finished:
        eid, cid = r["id"], r["container_id"]
        db_cids.add(cid)
        if cid in live_cids:
            await driver.destroy(cid, stop_timeout=0)
        async with conn.transaction():
            await conn.execute(
                "UPDATE executions SET container_id = NULL, "
                "updated_at = now() WHERE id = $1", eid)
            await conn.execute(
                "UPDATE execution_leases SET released_at = now() "
                "WHERE execution_id = $1 AND released_at IS NULL", eid)
        actions.append({
            "action": "reap",
            "execution_id": eid,
            "detail": (f"{r['state']} execution still held container "
                       f"{cid[:12]}; destroyed and lease released"),
        })

    # ── 4. Orphan sweep — containers with no matching execution ───────────
    # EXTRACTED to kernel/conductor/orphans.py: it has two callers now,
    # and they need different keep-sets. `db_cids` here is this goal's
    # containers, which is what this function has always passed.
    if sweep_orphans:
        actions.extend(await orphan_sweep(
            conn, driver, db_cids, dry_run=dry_run,
            live_containers=live_containers))

    return actions
