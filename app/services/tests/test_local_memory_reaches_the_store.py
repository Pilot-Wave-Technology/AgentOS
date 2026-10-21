"""A local story must not finish with no assistant checkpoint anywhere.

THE HOLE. When a local story goes terminal while checked out, the control
plane skipped the gateway assistant's story-complete flush, reasoning that
the extension "authors its own" and a gateway flush would clobber a curated
write with a thread distillation. The reasoning is right. Its premise was
never checked: the extension's flush is BEST-EFFORT -- the human can cancel
it, the model can fail to emit the sentinel, and the 180s timeout is
indistinguishable from either. A skipped local flush met a suppressed
gateway flush and the story reached done with no checkpoint in either lane.

Not a degradation. A hole, and a silent one, because each half looks correct
on its own.
"""

import pathlib

import pytest

from app.services.tests._source import code, source as src

ROOT = pathlib.Path(__file__).resolve().parents[3]


# ── suppression now needs proof ──────────────────────────────────────

def test_the_gateway_flush_is_suppressed_on_proof_not_on_the_checkout():
    body = code("app/memory_v2/event_bridge.py")
    assert "local_memory_consolidated_at" in body or \
           "_local_consolidation_stamp" in body, (
        "the flush is still suppressed for any checked-out local story, "
        "whether or not the local one happened")


def test_no_stamp_means_the_fallback_runs():
    body = code("app/memory_v2/event_bridge.py")
    i = body.index("_local_consolidation_stamp(story_id)")
    window = body[i:i + 900]
    assert "elif not consolidated_at" in window or "not consolidated_at" in window, (
        "nothing runs the gateway flush when the local one did not happen")


def test_a_failed_stamp_read_runs_the_fallback_too():
    """Fail-safe direction matters: 'cannot tell' must mean 'run it', not
    'skip it'. The cost of a redundant flush is a distillation nobody reads;
    the cost of a skipped one is the story's memory."""
    body = src("app/memory_v2/event_bridge.py")
    fn = body[body.index("async def _local_consolidation_stamp"):]
    fn = fn[:fn.index("\nasync def", 10)] if "\nasync def" in fn[10:] else fn
    assert "return None" in fn and "except" in fn, (
        "a database error is not turned into 'no consolidation'")


def test_a_new_checkout_clears_the_stamp():
    """The next session owes its own checkpoint; a stamp from a previous
    cycle would suppress the gateway flush for work it never saw."""
    body = code("app/services/local_checkout.py")
    i = body.index("SET checked_out_to_uid")
    assert "local_memory_consolidated_at = NULL" in body[i:i + 700]


# ── the threshold is served, not guessed ─────────────────────────────

def test_the_consolidation_threshold_is_served_to_the_client():
    from app.services.local_dispatch import (MEMORY_FILE_MAX_CHARS,
                                             MEMORY_FILE_TARGET_CHARS)
    assert MEMORY_FILE_MAX_CHARS > MEMORY_FILE_TARGET_CHARS > 0
    assert '"memory"' in code("app/services/local_dispatch.py"), (
        "get_state does not tell the session its memory budget")


def test_the_threshold_matches_the_gateways_own():
    """Restated, because the control plane cannot import the gateway -- so
    pinned against the gateway's source, or the two drift the day one
    changes and nobody notices the local lane kept the old number."""
    gw = ROOT.parent / "agent_os_openclaw/src/auto-reply/reply/memory-flush.ts"
    if not gw.exists():
        pytest.skip("the gateway checkout is not beside this one")
    text = gw.read_text(encoding="utf-8")
    from app.services.local_dispatch import (MEMORY_FILE_MAX_CHARS,
                                             MEMORY_FILE_TARGET_CHARS)
    assert f"DEFAULT_MEMORY_FILE_MAX_CHARS = {MEMORY_FILE_MAX_CHARS}" in text, (
        "the local cap no longer matches the gateway's")
    assert f"DEFAULT_MEMORY_FILE_TARGET_CHARS = {MEMORY_FILE_TARGET_CHARS}" in text, (
        "the local target no longer matches the gateway's")


# ── and a 200 that wrote nothing is not a success ────────────────────

def test_the_silent_no_op_is_still_reported_honestly():
    """`skipped: no_assistant_instance` means the lease lapsed and the
    reaper cleared the key -- so the memory is on disk and nowhere else.
    It is returned with ok=True because nothing went wrong HERE, which is
    exactly why the caller has to check it."""
    body = src("app/memory_v2/local_snapshot.py")
    assert "no_assistant_instance" in body
    fn = body[body.index("async def write_local_memory"):]
    assert "IS A FAILURE" in fn, (
        "nothing tells the reader that this ok=True is not a success")
