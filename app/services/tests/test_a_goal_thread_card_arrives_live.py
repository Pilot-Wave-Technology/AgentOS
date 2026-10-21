"""A card posted into the goal thread reaches the page without a reload.

THE SYMPTOM. GO proposes an autonomous errand, the approval card is
written into the goal thread, and it appears only when somebody
refreshes. Reported from the live UI on 2026-09-24.

WHERE IT WAS NOT. `errand_proposal_cards` is byte-identical in shape to
`kernel_park_cards`, whose cards land live on the assistant page: same
INSERT with `card_data`, same `notify_new_message` fan-out. The poster
was never at fault, which is why staring at it explained nothing.

THERE ARE TWO SUBSCRIBER REGISTRIES. A browser on the assistant chat
page is registered with `session_manager`; a browser on the GOAL THREAD
page is registered with `app/routers/goal_thread_subs`, which
`session_manager` knows nothing about. The cross-instance fan-out
delivered to the first only, so a goal-thread card was pushed at an
empty audience -- a correct push to nobody, which looks exactly like no
push at all.

WHY BOTH RATHER THAN THE RIGHT ONE. Deciding which page owns a thread is
a decision that would be wrong the first time a thread is opened
somewhere nobody anticipated, and it would be wrong silently, in the
same way. Both registries no-op when empty, so delivering to both costs
nothing and cannot be wrong.
"""

from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
MAIN = io.open(ROOT / "app/main.py", encoding="utf-8").read()


def _code(text: str) -> str:
    no_doc = re.sub(r'""".*?"""', "", text, flags=re.S)
    return "\n".join(line for line in no_doc.splitlines()
                     if not line.lstrip().startswith("#"))


def _broadcaster_block() -> str:
    """Just the fan-out broadcaster, comments and docstrings stripped."""
    code = _code(MAIN)
    start = code.index("async def _deliver(")
    return code[start:code.index("_chat_fanout.set_broadcaster(")]


def test_the_fanout_reaches_the_goal_thread_registry():
    """THE BUG. `session_manager` alone never reaches the goal thread."""
    block = _broadcaster_block()
    assert "goal_thread_subs" in block, (
        "the fan-out delivers to session_manager only, so a card posted "
        "into the goal thread is pushed at nobody")


def test_every_frame_goes_through_one_delivery_point():
    """Two frame shapes are built -- the card frame and the message
    frame -- and both must deliver the same way. Fixing one and not the
    other is how a card lands live and a message does not, which is
    harder to notice than neither working."""
    block = _broadcaster_block()
    assert block.count("await _deliver(") == 2, (
        "a frame path bypasses _deliver and reaches one registry only")
    # The only direct call left should be inside _deliver itself.
    assert block.count("session_manager.broadcast_to_thread") == 1


def test_a_goal_thread_failure_cannot_kill_the_fanout():
    """The listener serves every thread on the instance. An exception
    from one registry must not stop the other, nor the next message --
    the row is committed and a reload still shows it."""
    block = _broadcaster_block()
    fn = block[:block.index("async def _chat_broadcaster")]
    assert "try:" in fn and "except" in fn, (
        "the goal-thread delivery is unguarded, so one bad subscriber "
        "takes down cross-instance delivery for everybody")
    assert "session_manager.broadcast_to_thread" in fn[:fn.index("try:")], (
        "session_manager must be delivered to BEFORE the guarded call, "
        "or a goal-thread failure would skip the assistant page too")


def test_the_assistant_path_is_not_disturbed():
    """The registry that already worked must keep working, and must be
    reached first -- assistant cards are the common case and the one
    with no reported fault."""
    block = _broadcaster_block()
    fn = block[:block.index("async def _chat_broadcaster")]
    assert fn.index("session_manager.broadcast_to_thread") < fn.index(
        "goal_thread_subs")


def test_both_registries_are_written_and_not_one_as_a_fallback():
    """THE MIDDLE VERSION, AND WHY IT WAS WRONG. Frames were arriving
    twice, so this delivered to the goal-thread registry only when the
    session registry reached nobody -- `broadcast_to_thread` returns a
    count, so it was easy and it did stop the double.

    It also introduced a worse fault. A STALE session subscriber, a
    socket whose browser is gone and which has not been reaped yet,
    counts as reached; the goal thread page then never gets the frame.
    Intermittent, invisible, and indistinguishable from the original
    complaint -- having to refresh to see a card that is already there.

    The double was never here. The client dedupes on the row id and the
    guard was reading the wrong field name; see the frame-id tests at
    the foot of this file."""
    fn = _broadcaster_block()
    fn = fn[:fn.index("async def _chat_broadcaster")]
    assert "if reached:" not in fn, (
        "the goal-thread delivery is conditional again, so a stale "
        "session socket silently starves the goal thread page")
    assert "await session_manager.broadcast_to_thread" in fn
    assert "goal_thread_subs" in fn


def test_the_frame_carries_an_id_the_client_can_dedupe_on():
    """Both registries may hold the same viewer, so every frame carries
    a message id."""
    block = _broadcaster_block()
    assert '"msg_id": row.get("id")' in block
    assert '"message_id": row.get("id")' in block


def test_the_client_actually_dedupes_on_it():
    """AND THE CLIENT USES IT, which the test above does not prove and
    which I asserted without checking. Delivering to both registries
    made every errand card in the GO chat arrive TWICE, live on
    2026-09-24 -- the frame carried an id the whole time and nothing
    looked at it.

    The dedupe belongs on the client: the delivery side cannot know how
    many registries a given viewer happens to sit in."""
    html = io.open(ROOT / "app/templates/goal_thread/index.html",
                   encoding="utf-8").read()
    handler = html[html.index("msg.type === 'agent_card'"):]
    handler = handler[:handler.index("buildCardElement(msg)")]
    head = html[html.index("function handleMessage(msg) {"):]
    assert "__alreadySeen" in head[:head.index("if (msg.type ===")]


def test_a_card_with_no_id_is_still_shown():
    """Dropping every frame that lacks an id would be a worse bug than
    showing one twice."""
    html = io.open(ROOT / "app/templates/goal_thread/index.html",
                   encoding="utf-8").read()
    handler = html[html.index("msg.type === 'agent_card'"):]
    handler = handler[:handler.index("buildCardElement(msg)")]
    page = _page()
    helper = page[page.index("function __alreadySeen"):]
    helper = helper[:helper.index("}")]
    assert "if (!id) return false" in helper, (
        "the dedupe is unconditional, so an id-less frame is dropped")


def test_messages_are_deduped_too_and_not_only_cards():
    """EVERY frame arrives twice, not just cards -- `_deliver` sends all
    of them to both registries. Deduping the card branch alone left
    messages doubling on the GO thread, reported live on 2026-09-24,
    which is what fixing one branch and calling it done looks like."""
    page = _page()
    # ONE GUARD, BEFORE THE CHAIN. Per-branch guards were tried twice
    # and failed twice: the first covered only cards, the second put
    # branches for the other types AFTER the handlers that already
    # matched them, so they never ran. A guard at the top of
    # `handleMessage` cannot be half-applied.
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "__alreadySeen" in head, (
        "the dedupe is per-branch again, so some frame type doubles")


# ── and it can be APPROVED without a reload ──────────────────────────

def _page() -> str:
    return io.open(ROOT / "app/templates/goal_thread/index.html",
                   encoding="utf-8").read()


def test_the_proposal_card_renders_its_buttons_live():
    """DELIVERED IS NOT USABLE. Getting the frame to the page was only
    half of it: `errand_proposal` has no branch in the shared card
    library, so the generic path rendered a title and a body and NO
    buttons -- the card arrived live and still could not be approved
    without a reload, which is the thing that was actually asked for."""
    page = _page()
    assert "function buildProposalCard" in page
    handler = page[page.index("msg.type === 'agent_card'"):]
    handler = handler[:handler.index("const box =")]
    assert "errand_proposal" in handler and "buildProposalCard(msg)" in handler


def test_the_live_markup_matches_the_ids_decideProposal_reads():
    """THE IDS ARE THE CONTRACT. `decideProposal` looks up
    `proposal-actions-<id>` and `proposal-note-<id>` by id. Markup that
    differs renders buttons that do nothing -- worse than no buttons,
    because it looks like it worked."""
    page = _page()
    live = page[page.index("function buildProposalCard"):
                page.index("async function decideProposal")]
    assert "'id=\"proposal-actions-'" in live or \
           'id="proposal-actions-' in live
    assert "proposal-note-" in live
    assert "decideProposal(" in live
    # Both verdicts, or a decline is unreachable live.
    assert "'approve')" in live and "'decline')" in live


def test_the_live_card_and_the_partial_agree_on_the_note_field():
    """A decline with no reason gets re-proposed next week, so the note
    input has to exist on the live card too -- `decideProposal` refuses
    a decline without one and the person would have no box to type in."""
    live = _page()
    live = live[live.index("function buildProposalCard"):
                live.index("async function decideProposal")]
    partial = io.open(ROOT / "app/templates/goal_thread/_errand_proposal.html",
                      encoding="utf-8").read()
    assert "required to decline" in partial
    assert "required to decline" in live


def test_the_fanout_records_how_many_heard_it():
    """`broadcast` is a silent no-op on an empty subscriber set, which
    looks exactly like a client that got the frame and failed to render
    it. Those need opposite fixes, and three rounds of guessing at the
    errand-proposal card went by for want of one number."""
    block = _broadcaster_block()
    assert "watcher_count" in block, (
        "nothing records whether the goal-thread broadcast reached "
        "anybody, so a delivery failure and a render failure are "
        "indistinguishable in the logs")
    assert "watcher(s)" in block


# ── and the guard has to look at the field the frame actually sets ────

def test_the_guard_reads_the_id_a_message_frame_carries():
    """THE THIRD SPELLING. Frames carry their row id under three names:
    cards use `msg_id`, some paths use `message_id`, and a plain
    `message` frame uses `msg.id` -- which the very next line hands to
    `appendMessage`. The guard checked the first two only, so cards
    stopped doubling and every human message kept doubling.

    Reported as "my messages appear twice when I click send, and a
    refresh shows one". One row, two frames, a guard reading a field
    neither of them sets."""
    page = _page()
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "msg.id" in head, (
        "the dedupe key omits `msg.id`, so `message` frames are never "
        "deduped however many registries deliver them")
    # And the branch below must still be reading the same field, or the
    # two have drifted apart again.
    body = page[page.index("function handleMessage(msg) {"):]
    body = body[:body.index("} else if (msg.type === 'thinking')")]
    assert "msg.sender === currentUid, msg.id)" in body


def test_the_key_is_the_bare_row_id_and_not_namespaced_by_type():
    """NAMESPACING BY TYPE WAS TRIED AND IS WHAT BROKE THIS.

    One row reaches the page as two frames with DIFFERENT types --
    `human_message` from the cross-instance broadcaster, and the chat
    socket's own echo. A key of `<type>:<id>` makes those two distinct
    and both render. Reported as "now I see my messages appear twice",
    the moment the fan-out was repaired so both registries delivered
    for the first time; the previous NameError had been hiding it by
    suppressing the second delivery outright.

    The collision the namespace was defending against cannot occur:
    `msg_id`, `message_id` and `id` are all `messages.id` -- one table,
    UUIDs, no second numbering."""
    page = _page()
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "__alreadySeen(__fid)" in head, (
        "the dedupe key is decorated with something, so the same row "
        "arriving under two frame types renders twice")
    assert "msg.type" not in head.split("const __fid")[1], (
        "the frame type is still part of the key")


def test_a_frame_with_no_id_is_still_shown():
    """Token frames carry no id at all and must never be dropped. The
    guarantee lives in the helper -- `if (!id) return false` -- rather
    than in a ternary at the call site, so it holds for every caller
    and cannot be forgotten by one of them."""
    page = _page()
    helper = page[page.index("function __alreadySeen"):]
    helper = helper[:helper.index("}")]
    assert "if (!id) return false" in helper, (
        "the dedupe is unconditional, so an id-less frame is dropped")


def test_messages_are_deduped_too_and_not_only_cards():
    """EVERY frame arrives twice, not just cards -- `_deliver` sends all
    of them to both registries. Deduping the card branch alone left
    messages doubling on the GO thread, reported live on 2026-09-24,
    which is what fixing one branch and calling it done looks like."""
    page = _page()
    # ONE GUARD, BEFORE THE CHAIN. Per-branch guards were tried twice
    # and failed twice: the first covered only cards, the second put
    # branches for the other types AFTER the handlers that already
    # matched them, so they never ran. A guard at the top of
    # `handleMessage` cannot be half-applied.
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "__alreadySeen" in head, (
        "the dedupe is per-branch again, so some frame type doubles")


# ── and it can be APPROVED without a reload ──────────────────────────

def _page() -> str:
    return io.open(ROOT / "app/templates/goal_thread/index.html",
                   encoding="utf-8").read()


def test_the_proposal_card_renders_its_buttons_live():
    """DELIVERED IS NOT USABLE. Getting the frame to the page was only
    half of it: `errand_proposal` has no branch in the shared card
    library, so the generic path rendered a title and a body and NO
    buttons -- the card arrived live and still could not be approved
    without a reload, which is the thing that was actually asked for."""
    page = _page()
    assert "function buildProposalCard" in page
    handler = page[page.index("msg.type === 'agent_card'"):]
    handler = handler[:handler.index("const box =")]
    assert "errand_proposal" in handler and "buildProposalCard(msg)" in handler


def test_the_live_markup_matches_the_ids_decideProposal_reads():
    """THE IDS ARE THE CONTRACT. `decideProposal` looks up
    `proposal-actions-<id>` and `proposal-note-<id>` by id. Markup that
    differs renders buttons that do nothing -- worse than no buttons,
    because it looks like it worked."""
    page = _page()
    live = page[page.index("function buildProposalCard"):
                page.index("async function decideProposal")]
    assert "'id=\"proposal-actions-'" in live or \
           'id="proposal-actions-' in live
    assert "proposal-note-" in live
    assert "decideProposal(" in live
    # Both verdicts, or a decline is unreachable live.
    assert "'approve')" in live and "'decline')" in live


def test_the_live_card_and_the_partial_agree_on_the_note_field():
    """A decline with no reason gets re-proposed next week, so the note
    input has to exist on the live card too -- `decideProposal` refuses
    a decline without one and the person would have no box to type in."""
    live = _page()
    live = live[live.index("function buildProposalCard"):
                live.index("async function decideProposal")]
    partial = io.open(ROOT / "app/templates/goal_thread/_errand_proposal.html",
                      encoding="utf-8").read()
    assert "required to decline" in partial
    assert "required to decline" in live


def test_the_fanout_records_how_many_heard_it():
    """`broadcast` is a silent no-op on an empty subscriber set, which
    looks exactly like a client that got the frame and failed to render
    it. Those need opposite fixes, and three rounds of guessing at the
    errand-proposal card went by for want of one number."""
    block = _broadcaster_block()
    assert "watcher_count" in block, (
        "nothing records whether the goal-thread broadcast reached "
        "anybody, so a delivery failure and a render failure are "
        "indistinguishable in the logs")
    assert "watcher(s)" in block


# ── and the guard has to look at the field the frame actually sets ────

def test_the_guard_reads_the_id_a_message_frame_carries():
    """THE THIRD SPELLING. Frames carry their row id under three names:
    cards use `msg_id`, some paths use `message_id`, and a plain
    `message` frame uses `msg.id` -- which the very next line hands to
    `appendMessage`. The guard checked the first two only, so cards
    stopped doubling and every human message kept doubling.

    Reported as "my messages appear twice when I click send, and a
    refresh shows one". One row, two frames, a guard reading a field
    neither of them sets."""
    page = _page()
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "msg.id" in head, (
        "the dedupe key omits `msg.id`, so `message` frames are never "
        "deduped however many registries deliver them")
    # And the branch below must still be reading the same field, or the
    # two have drifted apart again.
    body = page[page.index("function handleMessage(msg) {"):]
    body = body[:body.index("} else if (msg.type === 'thinking')")]
    assert "msg.sender === currentUid, msg.id)" in body


def test_the_key_is_the_bare_row_id_and_not_namespaced_by_type():
    """NAMESPACING BY TYPE WAS TRIED AND IS WHAT BROKE THIS.

    One row reaches the page as two frames with DIFFERENT types --
    `human_message` from the cross-instance broadcaster, and the chat
    socket's own echo. A key of `<type>:<id>` makes those two distinct
    and both render. Reported as "now I see my messages appear twice",
    the moment the fan-out was repaired so both registries delivered
    for the first time; the previous NameError had been hiding it by
    suppressing the second delivery outright.

    The collision the namespace was defending against cannot occur:
    `msg_id`, `message_id` and `id` are all `messages.id` -- one table,
    UUIDs, no second numbering."""
    page = _page()
    head = page[page.index("function handleMessage(msg) {"):]
    head = head[:head.index("if (msg.type ===")]
    assert "__alreadySeen(__fid)" in head, (
        "the dedupe key is decorated with something, so the same row "
        "arriving under two frame types renders twice")
    assert "msg.type" not in head.split("const __fid")[1], (
        "the frame type is still part of the key")


def test_the_broadcast_happens_before_the_logging():
    """THE WORST BUG OF THE THREE, and it was in the instrumentation.

    The watcher-count log was added to tell a delivery failure apart
    from a render failure. Reverting the fallback then removed the
    `reached = ...` assignment and left the log line that reads it, so
    `NameError: reached` raised BEFORE `_goal_bc` was ever called. The
    except caught it, wrote a warning, and the goal-thread registry
    received NOTHING -- every frame, for the whole deploy. Observed on
    ERR07: GO's plain reply arrived live (it comes down the chat
    socket) and the errand proposal card never did.

    Restoring the variable is not the fix. Ordering is: the frame goes
    out first and the measurement happens after, so no future mistake
    in a log line can stop a delivery."""
    fn = _broadcaster_block()
    fn = fn[:fn.index("async def _chat_broadcaster")]
    assert fn.index("_goal_bc(goal_id") < fn.index('"fanout: %s'), (
        "the diagnostic runs before the broadcast it measures, so a "
        "fault in it swallows the frame")


def test_every_name_the_log_line_reads_is_assigned():
    """The specific slip, pinned. `reached` and `_n` are both computed
    in this function; a log referring to anything else would raise at
    exactly the point delivery is meant to happen."""
    fn = _broadcaster_block()
    fn = fn[:fn.index("async def _chat_broadcaster")]
    assert "reached = await session_manager.broadcast_to_thread" in fn
    assert "_n = _goal_n(" in fn


def test_the_session_broadcast_is_not_inside_the_guard():
    """It must run even if the goal-thread import or broadcast throws.
    That is the only reason anything reached the page at all while this
    was broken, and it is worth keeping on purpose."""
    fn = _broadcaster_block()
    fn = fn[:fn.index("async def _chat_broadcaster")]
    assert fn.index("session_manager.broadcast_to_thread") < fn.index("try:")
