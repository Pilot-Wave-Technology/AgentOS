"""Three ceilings were being consumed with nothing collecting them.

Phase C. Each of these silently eats a limit, and the symptom always
arrives somewhere else, later, as "nothing runs".

1. EPHEMERAL CLAIMS. `list_workspaces` reaps only `ws-exec-<digits>` --
   everything else is skipped so an operator's hand-made claim survives a
   sweep. That rule is right; the hole is that `podcheck` writes
   `ws-podcheck-<ts>` through the same manifest builder, so it is
   KERNEL-made and unreapable, and its only collector is a best-effort
   `kubectl delete` skipped under `--keep` and absent if the script is
   killed. Measured: one claim Pending for nineteen hours against a
   120-claim quota. A claim outlives its pod on purpose, so that quota is
   not "120 at once", it is 120 ever.

2. THE ENVCACHE. `_cache_get` expires on READ -- returns None, leaves the
   file -- and `_cache_put` only overwrites its own key. Every image-tag
   bump and every edited setup left a tar behind for ever, each bounded
   only by MAX_PREPARED_BYTES (1400MB).

3. ADMISSION SLOTS. The release is a TRIGGER on `AFTER UPDATE OF state`
   into a terminal state, which covers every run that ends and nothing
   else. A worker killed between claiming a slot and writing a state
   leaves a row that never updates again: the trigger never fires and the
   slot is held for ever. Four of those close a goal permanently.

And one thing that was not a leak but a silence: `_release_in_pod`
caught `(WorkspaceUnreachable, Exception)` under a single bare
`return False`, so a genuinely broken revoke was indistinguishable from
a pod that had simply exited -- for the one failure in that module that
actually matters.
"""
from __future__ import annotations

import ast
import os
import pathlib
import time

import pytest

from kernel.conductor import admit, ephemeral
from kernel.verifier import prepare

SWEEP = (pathlib.Path(ephemeral.__file__).parent
         / "sweep.py").read_text(encoding="utf-8")


# ── 1 · ephemeral claims ───────────────────────────────────────────────

def test_podcheck_labels_its_claim_for_collection():
    """A name can say what MADE a claim; it cannot say whether anybody
    still wants it. The label is the maker saying nobody will."""
    root = pathlib.Path(ephemeral.__file__).parents[2]
    src = (root / "scripts" / "podcheck.py").read_text(encoding="utf-8")
    assert '"agentos-ephemeral"' in src


def test_unlabelled_claims_stay_protected():
    """`list_workspaces` refuses to reap what it cannot identify, so an
    operator's hand-made claim is safe. This must not weaken that."""
    assert ephemeral.EPHEMERAL_LABEL == "agentos-ephemeral=true"


def test_the_reaper_judges_by_age_not_state():
    """A claim stuck Pending -- no node, no capacity, an unbindable
    storage class -- is the exact case that leaks, and it never reaches a
    state a state-based sweep could recognise. Pending claims count
    against the quota like any other."""
    assert ephemeral.MAX_AGE_S >= 3600


@pytest.mark.asyncio
async def test_docker_is_left_alone():
    class _Driver:
        name = "docker"

    assert await ephemeral.sweep_ephemeral(None, _Driver()) == []


def test_a_claim_with_no_age_is_kept():
    """Same rule as an unparseable name: a sweep that cannot identify
    something must not destroy it."""
    src = pathlib.Path(ephemeral.__file__).read_text(encoding="utf-8")
    at = src.index("if created is None:")
    # The guard, then its comment, then the `continue`.
    assert "continue" in src[at:at + 400]


# ── 2 · the envcache ───────────────────────────────────────────────────

def test_the_envcache_is_finally_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "CACHE_TTL_S", 100)

    fresh = tmp_path / "aaa.tar"
    fresh.write_bytes(b"x")
    stale = tmp_path / "bbb.tar"
    stale.write_bytes(b"x")
    old = time.time() - 1000
    os.utime(stale, (old, old))

    assert prepare.evict_cache() == 1
    assert fresh.exists() and not stale.exists()


def test_a_half_written_entry_is_not_evicted(tmp_path, monkeypatch):
    """`_cache_put` writes `.key.pid.tmp` and renames. A sweep that took
    those would race a verification that is mid-write."""
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prepare, "CACHE_TTL_S", 1)
    tmp = tmp_path / ".aaa.123.tmp"
    tmp.write_bytes(b"x")
    old = time.time() - 1000
    os.utime(tmp, (old, old))
    assert prepare.evict_cache() == 0
    assert tmp.exists()


def test_eviction_never_raises(monkeypatch):
    """It runs inside the reconcile pass; a failed eviction must not fail
    the sweep it lives in."""
    monkeypatch.setattr(prepare, "CACHE_DIR", "\0//nope")
    assert prepare.evict_cache() == 0


# ── 3 · admission slots ────────────────────────────────────────────────

def test_dead_slots_are_freed_by_state_not_by_age():
    """A slot held by a LIVE run is doing its job however long it takes.
    An age limit here would be a second concurrency ceiling nobody
    configured."""
    fn = next(n for n in ast.walk(ast.parse(
        pathlib.Path(admit.__file__).read_text(encoding="utf-8")))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "release_dead_slots")
    sql = ast.get_source_segment(
        pathlib.Path(admit.__file__).read_text(encoding="utf-8"), fn) or ""
    assert "terminal_states @> to_jsonb(e.state)" in sql
    assert "interval" not in sql, "must not reap by age"


def test_a_freed_slot_is_reported_loudly():
    """The trigger not firing is a fault, not routine housekeeping."""
    src = pathlib.Path(admit.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def release_dead_slots"):]
    assert "logger.warning" in body


# ── 4 · the silence that was not a leak ────────────────────────────────

def test_a_broken_revoke_is_no_longer_mistaken_for_a_dead_pod():
    from kernel.credentials import sweep as cs

    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    assert "except (WorkspaceUnreachable, Exception)" not in src
    assert "could not revoke the lease" in src


# ── the wiring ─────────────────────────────────────────────────────────

def test_all_three_run_in_reconcile():
    for name in ("sweep_ephemeral", "release_dead_slots", "evict_cache"):
        assert name in SWEEP, name


def test_a_dry_run_destroys_nothing():
    block = SWEEP[SWEEP.index("sweep_ephemeral(conn"):]
    assert "if not dry_run:" in block[:400]
