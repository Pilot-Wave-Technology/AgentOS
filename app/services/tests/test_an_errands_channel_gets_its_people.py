"""An errand's channel is created with the people already in it.

THE SAME TRANSACTION-VISIBILITY TRAP, FOR THE THIRD TIME, and this one
was introduced by the fix for the second.

`approve` runs inside `conn.transaction()` in all three of its callers
(Slack buttons, the `/errand` command, the kernel route), so the
thread row `_open_story_thread` inserts is NOT COMMITTED while that
function is still running. Handing the channel creator `get_pool()`
gave it a different connection:

  * it created the Slack channel          -- fine, that is not the DB
  * it wrote the binding on the pool      -- fine, and it committed
  * `sync_channel_members` then read
    `threads WHERE id = $1` to learn who
    should be invited                     -- AND SAW NOTHING

An empty set of expected members is not an error, so it invited
nobody and said nothing. Measured on CALY5: channel C0CA1138EG2,
correctly named, correctly bound, containing exactly one member --
the bot. Slack does not show a non-member a private channel, so to
the person who had just approved the errand it had simply not
appeared.

WHY THE STORY PATH WAS FINE AND THIS ONE WAS NOT. `ensure_story_thread`
inserts its thread on the pool in autocommit, so the row is visible to
anything that looks afterwards. `_open_story_thread` is the errand
equivalent and runs inside its caller's transaction -- the same
property that made `errand_proposal_cards` the one card poster whose
card never reached Slack.
"""
from __future__ import annotations

import inspect

from app.services import errand_grants


def test_the_channel_is_created_on_the_callers_connection():
    """`get_pool()` here is a different connection and cannot see the
    thread this function just inserted."""
    src = inspect.getsource(errand_grants._open_story_thread)
    i = src.index("ensure_channel_for_thread(")
    call = src[i:i + 200]
    assert "get_pool()" not in call, (
        "the errand channel is created on a pooled connection again; it "
        "will be made, bound, and left with nobody in it")
    assert "conn," in call, (
        "the caller's connection is not passed, so sync_channel_members "
        "cannot see the thread row")


def test_the_pool_is_not_imported_for_it_any_more():
    """A leftover import is the next person's invitation to use it."""
    src = inspect.getsource(errand_grants._open_story_thread)
    i = src.index("ensure_channel_for_thread")
    assert "from app.database import get_pool" not in src[:i][-400:]


def test_approve_really_does_run_in_a_transaction():
    """The whole reason the connection matters. If a caller stopped
    wrapping it, passing `conn` would still be correct — but this is
    the fact the comment in the source rests on, so it is pinned."""
    from app.channels.slack import go_actions
    src = inspect.getsource(go_actions.handle_errand)
    tx = src.index("conn.transaction()")
    call = src.index("await approve(")
    assert tx < call, (
        "approve is no longer inside a transaction; the reasoning in "
        "_open_story_thread's comment needs revisiting")


def test_the_whole_path_works_on_a_plain_connection():
    """Everything the channel creator touches must be available on a
    Connection, not just a Pool — `acquire`/`transaction` would raise
    on one, and only at runtime, inside an except that swallows it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("app/channels/slack/thread_channels.py",
                "app/channels/slack/membership.py"):
        src = (root / rel).read_text(encoding="utf-8")
        body = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        for pool_only in ("pool.acquire(", "pool.transaction("):
            assert pool_only not in body, (
                f"{rel} calls {pool_only}, so it cannot be handed the "
                f"caller's connection")


def test_the_members_are_synced_at_creation_at_all():
    """The invite is what makes the channel visible. Creating it and
    leaving it empty is indistinguishable from not creating it."""
    from app.channels.slack import thread_channels
    src = inspect.getsource(thread_channels.ensure_channel_for_thread)
    assert "sync_channel_members" in src
    # After the binding insert, or there is no binding to read.
    assert src.index("INSERT INTO platform_channel_bindings") < src.index(
        "sync_channel_members")


def test_an_empty_expected_set_is_the_silent_failure_mode():
    """Pinning the shape of the bug: `expected_uids` answers with a set,
    and an empty one means "nobody is entitled", which is a legitimate
    answer — so nothing downstream can tell it apart from "the row was
    invisible". That is why the connection has to be right here rather
    than defended there."""
    from app.channels.slack import membership
    src = inspect.getsource(membership.expected_uids)
    assert "return set()" in src
    sync = inspect.getsource(membership.sync_channel_members)
    i = sync.index("want_uids")
    assert "raise" not in sync[i:i + 300]
