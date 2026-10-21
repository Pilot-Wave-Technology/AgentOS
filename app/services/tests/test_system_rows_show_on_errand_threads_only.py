"""System and kernel rows are visible on an autonomous errand's thread.

WHY THEY MUST BE. `[ERRAND PARKED]`, `[ERRAND PROGRESS]` and the
kernel's answers are how an autonomous errand actually proceeds: GO is
woken by them, acts, and reports. Hidden, a person watching the thread
sees work happen and decisions get made with no account of either --
the black box the feature exists not to be. Run 308 ran for fourteen
minutes with eleven check-ins and a plan approved by GO, and none of
that machinery was visible.

WHY THEY MUST NOT BE VISIBLE ANYWHERE ELSE. The same rows carry memory
flushes, context injections and routing wakes on a goal's main thread,
an assistant's chat and the councils. Rendering them there buries the
conversation a person came to read. The existing fan-out filter is right
for every surface except this one.

WHICH IS WHY THE TEST IS THAT THE RULE IS PER-THREAD. The filter used to
be a property of the row, in SQL; it is a property of the THREAD, and
the thread's grant is what decides it. Revoke the grant and the thread
goes quiet again on the next message, with nothing to redeploy.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# -- the predicate -----------------------------------------------------

def test_the_grant_is_what_decides_it():
    """Not "is this an errand" -- an errand somebody starts by hand is
    not autonomous and its thread stays quiet."""
    src = _read("app/services/errand_thread_visibility.py")
    for clause in ("approved_by IS NOT NULL", "revoked_at IS NULL",
                   "expires_at IS NULL OR s.expires_at > now()"):
        assert clause in src, clause


def test_it_keys_on_the_thread_the_grant_names():
    """`driving_thread_id` is set when the errand is approved, so this
    cannot drift from the grant -- it IS the grant."""
    assert "s.driving_thread_id = $1" in _read(
        "app/services/errand_thread_visibility.py")


def test_it_fails_closed():
    """Unable to tell means hide. Showing plumbing on an assistant's
    chat by mistake is a visible regression on a page that was fine."""
    src = _read("app/services/errand_thread_visibility.py")
    fn = src[src.index("async def is_autonomous_errand_thread("):]
    assert "except Exception" in fn
    assert fn.rstrip().endswith("return False")


# -- the three surfaces, which must agree ------------------------------

def test_the_live_fan_out_asks_per_thread():
    """The filter was in the SQL, where it could only see the row. A
    row-level test can never express "except on this thread"."""
    src = _read("app/chat_module/fanout.py")
    assert "is_autonomous_errand_thread" in src
    assert "AND NOT (kind = 'system_wake'" not in src, (
        "the row-level filter is back, so errand threads are silent "
        "again whatever the grant says")


def test_the_page_render_asks_too():
    src = _read("app/routers/goal_thread.py")
    assert "show_system = await is_autonomous_errand_thread(" in src
    # The grant is one of THREE ways a row survives now: not a system
    # sender, carrying a card, or an errand thread. The card arm was
    # added after the verification card vanished on refresh.
    assert "m.sender <> 'system'" in src
    assert "$3::bool" in src


def test_the_catch_up_asks_the_same_question():
    """A reconnect must not quietly change what the thread shows."""
    src = _read("app/routers/goal_thread.py")
    body = src[src.index("    # SAME RULE AS THE PAGE RENDER"):]
    assert "is_autonomous_errand_thread(pool, thread_id)" in body
    assert "m.sender <> 'system'" in body
    assert "m.card_data IS NOT NULL" in body
    assert "$3::bool" in body


def test_a_card_bearing_row_is_still_never_filtered():
    """A row with `card_data` is a visible bubble on every surface, and
    always was. The new rule only reaches the card-less system wake."""
    src = _read("app/chat_module/fanout.py")
    guard = src[src.index("if (row is not None and row[\"kind\"]"):]
    guard = guard[:guard.index("if not row:")]
    assert 'row["card_data"] is None' in guard


def test_context_injections_are_still_dropped_everywhere():
    """`system_inject` rides the next turn and has never been a bubble.
    It stays filtered in SQL, on every thread including errands."""
    src = _read("app/chat_module/fanout.py")
    assert "kind <> 'system_inject'" in src


def test_paging_backwards_uses_the_same_system_row_rule():
    """THE SAME CONVERSATION HAD TWO HISTORIES. The page render and the
    socket catch-up both show `sender = 'system'` rows on an autonomous
    errand's thread -- there they are the errand talking, not plumbing --
    and hide them elsewhere. `get_thread_messages_earlier` hid them
    unconditionally, so the first page showed them and scrolling back
    made them vanish.

    The catch-up path states the rule in its own comment: "it has to be
    the same or a reconnect would quietly change what the thread shows".
    Paging backwards changes it just as quietly."""
    import io
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/routers/goal_thread.py", encoding="utf-8").read()
    fn = src[src.index("async def get_thread_messages_earlier"):]
    fn = fn[:fn.index("async def", 10)] if "async def" in fn[10:] else fn
    assert "is_autonomous_errand_thread" in fn, (
        "paging backwards hides system rows the first page showed")
    assert "m.sender <> 'system'" in fn
    assert "m.card_data IS NOT NULL" in fn
    assert "$5::bool" in fn


def test_all_three_readers_agree():
    """Named together so the next reader added has an obvious rule to
    follow, and so a fix to one that misses the others fails here."""
    import io
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/routers/goal_thread.py", encoding="utf-8").read()
    assert src.count("is_autonomous_errand_thread(") >= 3, (
        "a path reads this thread's messages without the errand rule")


def test_a_card_is_never_hidden_as_plumbing():
    """THE VERIFICATION CARD WENT AWAY ON REFRESH, and so did the
    completion card before it.

    The live fanout and the page render disagreed about what a `system`
    row is. Fanout filters only a SYSTEM-sender `system_wake` with NO
    card, and states the rule outright: "a card-bearing row (card_data
    present) is a visible bubble even when its kind became system_wake,
    so it is NOT filtered".

    The readers looked at the sender alone. So a card posted as `system`
    was broadcast live and hidden on reload -- correct in the database,
    absent from the page, which is indistinguishable from never having
    been posted.

    One thread cannot have two truths about what is on it."""
    import io
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/routers/goal_thread.py", encoding="utf-8").read()
    assert src.count("m.card_data IS NOT NULL") == 3, (
        "a reader of this thread still hides cards by their sender")


def test_the_fanout_rule_is_the_one_being_matched():
    """If the fanout ever starts dropping card-bearing rows, the readers
    above become wrong in the other direction -- showing what the live
    path hides. Pinned here so the pair moves together."""
    import io
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    fan = io.open(root / "app/chat_module/fanout.py", encoding="utf-8").read()
    assert 'row["card_data"] is None' in fan, (
        "the fanout no longer exempts card-bearing rows; the readers "
        "were matched to a rule that has changed")
