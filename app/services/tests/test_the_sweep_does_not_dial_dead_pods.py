"""The wedge, found by the probe's own stack dump.

MEASURED ON conductor-5bbcd48dd5-lht5g, 2026-09-17 11:58. The liveness
probe took a stack dump on its way to failing, and it named the fault in
one frame:

    podfs_client.py:210  revoke_credentials
    podfs_client.py:91   call
    urllib.request.urlopen
    http.client.connect
    socket.create_connection          <- stuck here

...while the MAIN thread sat in `selectors.select` with nothing to do.
The event loop was idle. A pool thread was not.

THE MECHANISM, end to end:

  * the credential sweep selects executions in TERMINAL states, on
    purpose -- that is when a lease should be revoked
  * but `destroy()` nulls `container_id` when it deletes the pod, and
    reclaim has normally been there first, so those pods are GONE
  * so every four seconds the sweep opened a TCP connection to an
    unrouted pod IP
  * `podfs_client` had one 120s timeout covering connect AND read, so
    each of those held a pool thread for two full minutes
  * threads accumulated far faster than they drained; before Phase R the
    pool was TWELVE, sized from the node's CPU count by accident
  * once it emptied, every Kubernetes call and every podfs call queued
    behind it, no pass could finish, no heartbeat was written -- and the
    liveness probe killed a process whose event loop was perfectly fine

That is the thirty-minute wedge, and every recurrence since. It is also
the same defect `k8s_call` was written for the same morning: no separate
connect bound. Fixed there, missed here.

TWO LAYERS, because either alone is thin. Not dialling a pod that is
gone removes the cause; a short timeout means the next thing that dials
a dead address costs ten seconds of one thread instead of two minutes.
"""
from __future__ import annotations

import pathlib

from kernel.credentials import sweep as cs
from kernel.workspace import podfs_client

SWEEP = pathlib.Path(cs.__file__).read_text(encoding="utf-8")


# ── layer 1 · do not dial what is not there ────────────────────────────

def test_the_sweep_skips_dialling_a_pod_that_is_gone():
    """`destroy()` nulls `container_id`. A lease inside a container that
    no longer exists died with it and needs no revoking.

    AT THE DIAL, NOT IN THE QUERY. The first version guarded the query
    and broke the LOCAL release path -- on Docker and E0 there is no pod
    and the lease is a file here that must still be unlinked."""
    assert 'if not row.get("container_id"):' in SWEEP


def test_it_still_selects_every_terminal_run():
    """The skip must not turn into 'never release anything'. The query is
    unchanged in what it RETURNS; only the network call is guarded, so
    the local unlink still happens for every terminal run."""
    assert "state = ANY($1::text[])" in SWEEP
    assert "TERMINAL" in SWEEP
    assert "os.remove(path)" in SWEEP


# ── layer 2 · a dead address must be cheap to discover ─────────────────

def test_a_small_operation_gives_up_quickly():
    """Two minutes to learn that a pod is absent is what filled the
    thread pool."""
    for op in ("revoke_credentials", "heads", "digest", "read",
               "credentials"):
        assert podfs_client.timeout_for(op) <= 15, op


def test_moving_a_tree_still_gets_its_time():
    """`archive` legitimately takes minutes -- its size is the work's
    business, not the network's. Shortening this would turn a big
    repository into a verification failure."""
    for op in ("archive", "archive_mounts", "publish"):
        assert podfs_client.timeout_for(op) >= 120, op


def test_an_unknown_operation_is_treated_as_quick():
    """Fail towards the cheap answer: a new op that genuinely needs
    minutes will say so by being added to the slow set, and until then a
    typo costs ten seconds rather than two minutes."""
    assert podfs_client.timeout_for("something_new") <= 15


def test_the_timeout_is_actually_used():
    src = pathlib.Path(podfs_client.__file__).read_text(encoding="utf-8")
    assert "timeout=timeout_for(op)" in src
    assert "timeout=TIMEOUT)" not in src


# ── why this was invisible for a day ───────────────────────────────────

def test_the_dumper_is_what_found_it():
    """Recorded so the next person does not re-derive it. The event loop
    was IDLE -- every diagnostic that looked at the loop, the database or
    the tick said healthy, because they all were. Only the stack of a
    non-event-loop thread showed the fault."""
    from kernel.obs import alive

    assert callable(alive.dump_the_wedged_process)
    src = pathlib.Path(alive.__file__).read_text(encoding="utf-8")
    assert "if not healthy:" in src
