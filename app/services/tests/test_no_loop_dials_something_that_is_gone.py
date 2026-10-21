"""A loop must not act on something that no longer exists.

THE CLASS, not the instance. Found twice on 2026-09-17 and both times it
presented as a wedged conductor with a perfectly healthy event loop:

  * the CREDENTIAL SWEEP selected executions in terminal states -- right,
    that is when a lease should be revoked -- and dialled their pods.
    But `destroy()` nulls `container_id` and reclaim had normally been
    there first, so those pods were GONE. Every four seconds it opened a
    TCP connection to an unrouted address and held a pool thread for the
    full 120s timeout. Threads accumulated faster than they drained; the
    pool emptied; every Kubernetes and podfs call queued behind it; no
    pass finished; no heartbeat was written; the probe killed a process
    whose event loop was idle.

  * the ADJUDICATOR selected VERIFYING executions without asking whether
    the pod was still there, then fetched the committed tree through
    podfs. `archive` is slow BY DESIGN -- a tree takes a while -- so a
    run whose node was preempted sent six concurrent verifications to
    wait on a dead address, each holding a connection and a thread.

`claimable` has always had `AND e.container_id IS NOT NULL` and was never
affected. That is the pattern, and this file is the rule.

THE SHAPE TO WATCH FOR: a query selects a row naming something EXTERNAL
-- `container_id` is a pod, `workspace_host` is a pod IP,
`workspace_path` is a path inside one -- and the code then dials it.
Every such query must constrain on the thing still existing, or the loop
inherits the wedge.

AND SKIPPING IS NOT ENOUGH. A row dropped silently becomes an invisible
stall, which is the failure Phase S exists to end. Each skip here has a
matching reporter that writes `stall_reason`.
"""
from __future__ import annotations

import pathlib

from kernel.conductor import promote, reclaim
from kernel.credentials import sweep as credential_sweep
from kernel.verifier import adjudicator
from kernel.worker import loop as worker_loop


def _src(mod) -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8")


def _sql_lines(src: str) -> str:
    """Source with `--` comment lines removed.

    Needed because the comments here QUOTE the clause they are warning
    about, so a naive substring check passes for the wrong reason -- and
    the first version of this test did exactly that.
    """
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        out.append(line)
    return chr(10).join(out)



# ── the queries that feed something which dials ────────────────────────

def test_the_leg_claim_requires_a_pod():
    """The original of the pattern -- it dials the bridge."""
    assert "e.container_id IS NOT NULL" in _src(worker_loop)


def test_verification_requires_a_pod():
    """It fetches the tree through podfs, and `archive` is slow by
    design -- so a missing pod costs the full timeout, six at a time.

    Here the QUERY is the right place: unlike the credential sweep, there
    is no local fallback -- verification cannot happen at all without the
    tree, on any substrate."""
    body = _sql_lines(_src(adjudicator))
    at = body.index("async def verifying(")
    assert "container_id IS NOT NULL" in body[at:at + 1200]


def test_the_credential_sweep_guards_the_dial_not_the_query():
    """AND THIS ONE IS A CORRECTION. The first version put
    `AND container_id IS NOT NULL` in the QUERY, which broke the LOCAL
    release path entirely: on Docker and E0 there is no pod and no
    container_id, and the lease is a file on this filesystem that must
    still be unlinked. CI caught it --
    `test_the_sweep_releases_finished_runs_and_spares_live_ones`.

    The wedge is a property of DIALLING, not of selecting. So the query
    still returns every terminal run, the local unlink still happens, and
    only the network call is guarded."""
    src = _src(credential_sweep)
    assert 'if not row.get("container_id"):' in src
    assert "AND container_id IS NOT NULL" not in _sql_lines(src), (
        "guarding the query blocks the local release path too")


# ── and every skip is reported, not swallowed ──────────────────────────

def test_a_verifying_run_with_no_pod_says_so():
    """Dropping it silently would trade a wedge for an invisible stall."""
    body = _src(adjudicator)
    assert "async def stranded_verifying" in body
    assert "the execution pod is gone" in body
    assert "stall_reason" in body


def test_the_stranded_report_runs_before_anything_is_dialled():
    body = _src(adjudicator)
    assert body.index("stranded_verifying(conn, goal_id)") < body.index(
        "if supervisor is not None and acquire is not None:")


def test_both_adjudicate_paths_carry_the_stranded_rows():
    """`adjudicate_pass` has a concurrent path and a serial one. A report
    that only one of them returns is a report nobody sees in the other
    deployment shape."""
    body = _src(adjudicator)
    assert body.count("return stranded + actions") == 2


# ── the ones that are allowed to dial a maybe-dead thing ───────────────

def test_reclaim_and_promote_are_deliberately_exempt():
    """Not every dial needs the guard, and pretending otherwise would be
    cargo cult:

      * `reclaim` exists to DESTROY things, so a pod that has already
        gone is a success, and the driver treats not-found as done.
      * `promote` probes a pod it has just created -- 'is it ready yet'
        is the question, and absence is a legitimate answer it handles.

    Both are bounded by `k8s_call`'s (5s connect, 30s read), so neither
    can hold a thread the way an unbounded podfs call did.
    """
    # Reclaim delegates destruction to the driver, and the DRIVER is
    # where not-found is forgiven -- `destroy` and `release_workspace`
    # both swallow it, because a thing already gone is the outcome
    # reclaim wanted.
    from kernel.drivers import kubernetes

    assert "driver.destroy(" in _src(reclaim)
    assert "driver.release_workspace(" in _src(reclaim)
    assert "_not_found" in _src(kubernetes)
    # Promote probes a pod it just created; absence is an answer it
    # handles, and it is bounded by its own startup budget.
    assert "STARTUP_BUDGET_S" in _src(promote)


# ── the timeout half, which is the backstop for whatever this misses ───

def test_discovering_a_dead_address_is_cheap():
    """Whatever slips past the guards above must not cost two minutes of
    a pool thread."""
    from kernel.workspace import podfs_client

    assert podfs_client.timeout_for("revoke_credentials") <= 15
    assert podfs_client.timeout_for("heads") <= 15


def test_kubernetes_calls_bound_connect_separately_from_read():
    """The same lesson, learned first: a scalar timeout leaves a dropped
    SYN hanging."""
    from kernel.drivers import k8s_call

    assert k8s_call.CONNECT_S <= 10
    assert k8s_call.DEFAULT_S >= k8s_call.CONNECT_S
