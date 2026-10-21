"""An approved autonomous errand gets a GO thread, not an assistant one.

WHAT WAS THERE BEFORE. `_open_story_thread` copied `agent_id` off
whichever thread the approver last used, so the errand landed on that
person's assistant. That was never a decision -- it was the only agent id
in reach at the moment the row had to be written. The GO proposed the
errand, the GO is already woken on every park, and the assistant's part
is over once a person has said yes.

THE TWO THINGS THAT MUST NOT MOVE WITH IT, and each has its own test
below because each was found by following the change rather than by
reading the plan:

  `created_by` STAYS THE APPROVER. The ledger derives `human_uid` from
  `threads.created_by`, never from `agent_id`, so the person who
  approved remains the principal on every run.

  THE OPERATOR WORKSPACE STAYS THE ASSISTANT'S. It was resolved by
  reading `threads.agent_id` back off the row, and
  `provision_story_workspace` branches on `is_kernel_assistant` -- false
  for a GO -- so the row-read would have silently upgraded every errand
  from a docs-only workspace to a full clone with a token and a
  credential helper.
"""
from __future__ import annotations

import inspect
import pathlib
import re

from app.services import errand_grants, sidebar_threads
from app.routers import go_thread_pick

_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _flat(src: str) -> str:
    return " ".join(src.split())


# ── the thread itself ─────────────────────────────────────────────────

def test_the_thread_names_the_goals_go():
    src = inspect.getsource(errand_grants._open_story_thread)
    assert "SELECT go_agent_id FROM goals WHERE id = $1" in src, (
        "the errand thread is still bound to whichever assistant the "
        "approver last used")


def test_a_goal_with_no_go_keeps_working():
    """`go_agent_id` defaults to '' and older goals predate the
    orchestrator. Those must keep getting a thread, not lose one."""
    src = inspect.getsource(errand_grants._open_story_thread)
    assert "_approver_assistant(conn, goal_id, uid, like_thread)" in src
    assert "has no GO" in src


def test_the_approver_is_still_the_principal():
    """`kernel/ledger/actors.py` derives `human_uid` from `created_by`.
    Moving the conversation must not move the accountability."""
    src = inspect.getsource(errand_grants._open_story_thread)
    insert = src.split("INSERT INTO threads", 1)[1]
    assert "created_by" in insert
    assert "thread_id, goal_id, agent_id, story_id, uid" in _flat(insert)

    # PARTICIPANTS AND ACCOUNTABILITY ARE NOW DIFFERENT PEOPLE, and that
    # is the point of this assertion rather than a relaxation of it.
    #
    # This used to be `ARRAY[$5]::text[]` -- the approver alone, which
    # made the errand invisible to everybody else in the goal. An
    # autonomous errand is the goal's work, so the thread is goal-wide:
    # empty participants is mode one of the visibility rule (see
    # `app/channels/slack/membership.py`), meaning every goal member.
    #
    # `created_by` stays $5 regardless. `kernel/ledger/actors.py` reads
    # it for `human_uid`, so widening who can WATCH the errand must not
    # widen who ANSWERED for it.
    assert "'{}'::text[]" in insert, (
        "participants is no longer goal-wide; the errand is invisible to "
        "the rest of the goal again")
    assert "$5,$6,'{}'::text[]" in _flat(insert).replace(" ", ""), (
        "created_by is no longer $5 (uid) -- the approver has stopped "
        "being the principal on the ledger")


def test_only_grant_approval_opens_one():
    """The whole reason this change is safe for hand-started errands: a
    single caller, and it is the autonomous path. A second caller added
    later would widen it silently."""
    src = (_ROOT / "app/services/errand_grants.py").read_text(
        encoding="utf-8")
    calls = re.findall(r"^\s*\w*\s*=?\s*await _open_story_thread\(",
                       src, re.M)
    assert len(calls) == 1, (
        f"_open_story_thread now has {len(calls)} callers; each one moves "
        f"another kind of errand onto the GO")


# ── the workspace, which must NOT move ────────────────────────────────

def test_the_workspace_is_told_the_assistant_rather_than_reading_the_row():
    src = inspect.getsource(errand_grants._schedule_operator_workspace)
    assert "assistant_id or await pool.fetchval" in src, (
        "the provisioner reads threads.agent_id again, which is now the "
        "GO -- every errand would take the WORKSPACE_CLONE branch")


def test_the_approver_assistant_is_captured_before_the_thread_moves():
    src = inspect.getsource(errand_grants.approve)
    i = src.index("_approver_assistant(conn, pid, uid, thread_id)")
    j = src.index("_open_story_thread(")
    assert i < j, ("the assistant is resolved after the thread has already "
                   "been pointed at the GO")


# ── which GO thread a request may render ──────────────────────────────

class _Pool:
    def __init__(self, row=None):
        self._row = row

    async def fetchrow(self, *_a):
        return self._row


_GOAL = {
    "id": "KER12",
    "go_thread_id": "main",
    "orchestrator_thread_id": "mesh",
    "council_thread_id": "review",
    "security_thread_id": "sec",
}


async def _pick(pool, thread_id):
    return await go_thread_pick.resolve(pool, _GOAL, thread_id)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_no_thread_named_is_the_main_one():
    assert _run(_pick(_Pool(), None)) == "main"


def test_the_three_fixed_channels_are_refused():
    """Agent Mesh, Goal Review Council and Security Council fold into the
    GO's MAIN session and are rendered by their own routes with their own
    framing. This page is not where they live."""
    for named in ("mesh", "review", "sec"):
        assert _run(_pick(_Pool(), named)) is None, named


def test_a_thread_from_another_goal_is_refused():
    """The query is scoped to the goal, so a foreign id returns no row --
    the cross-tenant shape `_guard_thread_in_goal` exists to stop."""
    assert _run(_pick(_Pool(None), "somebody-elses")) is None


def test_an_assistant_thread_is_refused():
    """This page opens a GO session and forwards what you type to the GO.
    Pointed at an assistant's thread it would relay the wrong agent's
    history and post the GO's replies into somebody else's conversation."""
    row = {"agent_id": "abhishek-shca", "story_id": "abc123"}
    assert _run(_pick(_Pool(row), "t1")) is None


def test_an_errand_thread_on_the_go_is_allowed():
    row = {"agent_id": "go-ker12", "story_id": "abc123"}
    assert _run(_pick(_Pool(row), "t1")) == "t1"


def test_a_bad_id_is_refused_rather_than_defaulted():
    """Falling back to the main thread would show somebody a conversation
    they did not ask for and let them type into it believing it was the
    errand's -- and a caller probing ids would get a 200 every time."""
    src = inspect.getsource(go_thread_pick.resolve)
    assert "return main or None" in src
    assert src.count("return None") >= 3


# ── the sidebar, or the thread exists on no page ──────────────────────

def test_errand_rows_link_to_the_go_thread_page():
    """THE HREF, NOT THE MENTIONS. The first version of this searched the
    whole function for "/chat/" and failed on the docstring explaining
    that it must not be used -- a guard that forbids explaining itself.
    Only the anchor decides where a click goes."""
    src = inspect.getsource(sidebar_threads.errand_thread_row)
    hrefs = re.findall(r'<a href="([^"]+)"', src)
    assert hrefs == ["/goals/{goal_id}/thread?thread_id={tid}"], hrefs


def test_only_errands_are_listed_under_the_go():
    src = inspect.getsource(sidebar_threads.go_errand_threads)
    assert "COALESCE(s.work_kind, 'story') = 'errand'" in src


def test_the_errand_list_is_not_scoped_to_one_person():
    """An assistant thread is private between one member and their own
    agent. The GO belongs to the goal, so an errand it runs is the goal's
    business and any member who can see the goal thread can see it."""
    src = inspect.getsource(sidebar_threads.go_errand_threads)
    assert "created_by" not in src


def test_the_generic_conversation_list_still_excludes_go_threads():
    """That list links to `/chat/{agent}`. A GO thread rendered there is
    the same wrong-agent bug from the other direction."""
    src = (_ROOT / "app/routers/goals.py").read_text(encoding="utf-8")
    assert "NOT t.agent_id LIKE 'go-%'" in src


def test_the_waiting_lookup_is_shared_with_the_assistant_list():
    """Two copies of a fail-soft kernel lookup is how one of them quietly
    stops failing soft."""
    src = inspect.getsource(sidebar_threads)
    # `kernel.questions`, since the lookup was corrected off the
    # never-created `human_requests`. What this asserts is unchanged:
    # ONE copy of the lookup, not which table it reads.
    assert src.count("FROM kernel.questions") == 1
    assert "_mark_waiting" in inspect.getsource(
        sidebar_threads.assistant_story_threads)


# ── what the GO is told about the thread it woke up in ────────────────

def test_an_errand_thread_says_which_errand():
    from app.services import go_thread_scope
    src = inspect.getsource(go_thread_scope.errand_block)
    assert "## This thread" in src
    assert "Approved by:" in src


def test_a_goal_thread_is_told_nothing_extra():
    """Every turn the GO takes today is a goal-thread turn; it must read
    exactly as it did before or this is a regression dressed as a
    feature."""
    from app.services import go_thread_scope
    src = inspect.getsource(go_thread_scope.errand_block)
    assert 'if not thread_id:\n        return ""' in src


def test_a_story_thread_is_not_claimed_by_the_go():
    from app.services import go_thread_scope
    src = inspect.getsource(go_thread_scope.errand_block)
    assert '(row["work_kind"] or "story") != "errand"' in src


# ── every call the page makes must name the thread ────────────────────
#
# THE BUG THIS EXISTS FOR, found on a live errand. The page resolved the
# errand's thread server-side and rendered its history correctly -- and
# then the websocket, the new-message poll, the load-earlier fetch and
# the run-state poll all connected WITHOUT naming a thread, resolved to
# `goals.go_thread_id`, and sent everything to the main conversation.
# So the errand thread displayed its own past and wrote its present
# somewhere else, and the GO's replies came back on the main thread.
#
# Half-wiring a parameter is worse than not adding it: the page LOOKS
# right, which is why it took somebody running an errand to notice.

def _page_js() -> str:
    return (_ROOT / "app/templates/goal_thread/index.html").read_text(
        encoding="utf-8")


def test_the_page_knows_which_thread_it_is():
    js = _page_js()
    assert 'const THREAD_ID = "{{ thread_id }}";' in js, (
        "the client has no thread to name, so every call falls back to "
        "the goal's main one")


def test_the_websocket_names_it():
    """This is the one that mattered. Messages are sent over the socket,
    so a socket bound to the main thread sends there whatever the page
    is showing."""
    js = _page_js()
    line = [l for l in js.splitlines() if "/thread/ws?token=" in l]
    assert line and "THREAD_Q" in line[0], line


def test_the_message_poll_and_run_state_name_it():
    js = _page_js()
    for frag in ("/thread/messages?after=", "/thread/run-state?thread_id="):
        assert frag in js, frag
    poll = [l for l in js.splitlines() if "/thread/messages?after=" in l]
    assert poll and "THREAD_Q" in poll[0], poll


def test_load_earlier_is_named_server_side():
    from app.routers import goal_thread
    src = inspect.getsource(goal_thread.goal_thread_page)
    assert "thread_id={go_thread_id}" in src


def test_every_endpoint_the_page_calls_accepts_a_thread():
    """A client that sends one and a server that ignores it is the same
    bug with an extra step."""
    from app.routers import goal_thread as gt
    for fn in (gt.goal_thread_page, gt.get_go_thread_run_state,
               gt.get_thread_messages_earlier, gt.get_thread_messages_after,
               gt.goal_thread_ws):
        assert "thread_id" in inspect.signature(fn).parameters, fn.__name__


def test_they_all_resolve_through_the_same_guard():
    """Three endpoints each grew their own `SELECT go_thread_id`. One
    resolver, so a thread the PAGE refuses cannot be reachable through
    the poll."""
    from app.routers import goal_thread as gt
    for fn in (gt.get_go_thread_run_state, gt.get_thread_messages_earlier,
               gt.get_thread_messages_after):
        assert "_thread_for(pool, pid, thread_id)" in inspect.getsource(fn), \
            fn.__name__
    assert "go_thread_pick" in inspect.getsource(gt._thread_for)


def test_the_three_fixed_channels_are_untouched():
    """Agent Mesh, Goal Review Council and Security Council keep their
    own routes and their own templates. `go_thread_pick` refuses them by
    id, so nothing here can render or write one."""
    from app.routers import go_thread_pick
    src = inspect.getsource(go_thread_pick.resolve)
    for named in ("orchestrator_thread_id", "council_thread_id",
                  "security_thread_id"):
        assert named in src, named
    assert "named" in src and "return None" in src


# ── the card has to be approvable, not just readable ──────────────────
#
# THE BLOCKING BUG. An approved errand's plan card reached the GO thread
# correctly -- `kernel_park_cards` resolved `driving_thread_id` and the
# body rendered with its steps and its verified-by. And there was no
# Approve button, because `goal_thread/_message.html` renders text and
# the assistant's `chat/_message.html` is where the card actions live.
# The only way to approve one was a POST typed into a browser console.

def test_the_park_card_survives_normalisation():
    """`_normalize_messages` popped `card_data` and kept only the errand
    proposal, so a kernel card reached the template as prose."""
    from app.routers import goal_thread as gt
    src = inspect.getsource(gt._normalize_messages)
    assert 'msg["kernel_card"] = _kernel_card(card)' in src


def test_every_card_kind_is_passed_through():
    """THIS TEST USED TO ASSERT THE OPPOSITE, and it was wrong in the
    same way the code was: a four-name allowlist written from the kinds
    I happened to have read. There are sixteen -- approval, completion,
    question, review, progress, failure, kernel_answer, git_pr_opened,
    git_node_error and the rest -- and a kind not on the list reached
    the thread and rendered as nothing at all."""
    from app.routers.goal_thread import _kernel_card
    assert _kernel_card(None) == ""
    assert _kernel_card({"no_card_type": 1}) == ""
    for kind in ("kernel_park", "diff_review", "approval", "completion",
                 "question", "review", "progress", "failure",
                 "kernel_answer", "git_pr_opened", "git_node_error",
                 "something_added_next_year"):
        out = _kernel_card({"card_type": kind, "question_id": 1})
        assert f'"{kind}"' in out, kind


def test_the_errand_proposal_is_the_one_exception():
    """It already has a server-rendered partial with its own form, so
    passing it to the client renderer would draw it twice."""
    from app.routers.goal_thread import _kernel_card
    assert _kernel_card({"card_type": "errand_proposal"}) == ""


def test_a_closing_script_tag_cannot_escape_the_json():
    """It rides in a `<script type="application/json">`, so `</` has to
    be escaped or a card body could close the tag and inject markup."""
    from app.routers.goal_thread import _kernel_card
    out = _kernel_card({"card_type": "kernel_park",
                        "body": "</script><img src=x onerror=alert(1)>"})
    assert "</script>" not in out
    assert "<" + chr(92) + "/script>" in out


def test_the_row_mounts_the_card():
    body = (_ROOT / "app/templates/goal_thread/_message.html").read_text(
        encoding="utf-8")
    assert 'class="card-container' in body
    assert 'script class="card-json"' in body
    assert "msg.kernel_card" in body


def test_the_renderer_is_shared_and_not_copied():
    """Two copies of an approval button is how one of them comes to be
    wrong -- which already happened once inside the assistant's page,
    where the live and reload paths each had their own."""
    shared = _ROOT / "static/js/kernel_park_card.js"
    assert shared.exists()
    js = shared.read_text(encoding="utf-8")
    for fn in ("function buildParkHtml", "function parkOptionsHtml",
               "async function decidePark", "function hydrateParkCards"):
        assert fn in js, fn

    chat = (_ROOT / "app/templates/chat/index.html").read_text(
        encoding="utf-8")
    assert "function buildParkHtml" not in chat, (
        "the assistant page kept its own copy of the renderer")
    assert "/static/js/kernel_park_card.js" in chat

    go = (_ROOT / "app/templates/goal_thread/index.html").read_text(
        encoding="utf-8")
    assert "/static/js/kernel_park_card.js" in go
    assert "hydrateParkCards()" in go


def test_the_go_page_supplies_what_the_renderer_needs():
    """PID, escapeHtml and renderMd. A missing one is a card that throws
    while rendering, which looks exactly like a card that never came."""
    go = (_ROOT / "app/templates/goal_thread/index.html").read_text(
        encoding="utf-8")
    assert "const PID = pid;" in go
    assert "function renderMd(" in go
    assert "function escapeHtml(" in go


def test_the_card_styles_are_shared_too():
    """They were inline in the assistant's page while their `.light`
    overrides were already in app.css -- half a rule set in each place,
    so the card rendered here unstyled."""
    css = (_ROOT / "static/css/app.css").read_text(encoding="utf-8")
    assert ".agent-card.card-approval" in css
    assert ".agent-card .card-options" in css
    chat = (_ROOT / "app/templates/chat/index.html").read_text(
        encoding="utf-8")
    assert ".agent-card.card-approval {" not in chat


def test_thread_q_is_not_trapped_inside_connect():
    """It was declared in `connect()` and used by
    `catchUpMissedMessages()` too -- a ReferenceError on every
    reconnect, which is the one path that only fires once the socket has
    already dropped."""
    go = (_ROOT / "app/templates/goal_thread/index.html").read_text(
        encoding="utf-8")
    decl = [i for i, l in enumerate(go.splitlines())
            if l.strip().startswith("const THREAD_Q")]
    assert len(decl) == 1, decl
    body = go.splitlines()[decl[0]]
    assert not body.startswith("  "), "THREAD_Q is indented inside a function"


# ── an errand's traffic stays out of the main session ─────────────────
#
# THE COST IF IT DOES NOT. The gateway keys a session by thread, so the
# GO's main conversation is one context window that has to last the
# whole goal -- and it is the one a person actually reads. An errand
# parks many times over its life: a plan, a question, a destructive
# command, another plan after a steer. Every one of those used to be
# delivered to `goals.go_thread_id`.

def test_an_errand_park_wakes_the_errands_own_thread():
    from app.services import go_wake_events as ev
    src = inspect.getsource(ev.on_execution_parked)
    # HOISTED INTO A VARIABLE, because the approver notice needs the
    # same thread and resolving it twice invites the two disagreeing.
    assert "thread_id = await _errand_thread(pool, execution)" in src
    assert "thread_id=thread_id)" in src


def test_the_errand_thread_is_resolved_from_the_run_not_guessed():
    """`driving_thread_id` is what `errand_grants.approve` set, so it is
    the errand's own thread by construction rather than by search."""
    from app.services import go_wake_events as ev
    src = inspect.getsource(ev._errand_thread)
    assert "e.driving_thread_id" in src


def test_it_refuses_a_driving_thread_that_is_not_the_gos():
    """A HAND-STARTED errand's driving thread is an ASSISTANT's, and a
    GO wake delivered there would put the GO's voice in somebody else's
    conversation."""
    from app.services import go_wake_events as ev
    src = inspect.getsource(ev._errand_thread)
    assert "lower(t.agent_id) LIKE 'go-%'" in src


def test_a_failed_lookup_falls_back_rather_than_dropping_the_wake():
    """A wake that never arrives is an errand parked on a question
    nobody is told about."""
    from app.services import go_wake_events as ev
    assert "except Exception" in inspect.getsource(ev._errand_thread)
    deliver = inspect.getsource(ev._deliver)
    assert 'thread_id or go["go_thread_id"]' in deliver


def test_goal_level_wakes_still_go_to_the_main_thread():
    """A node event, a trunk advance and a resource are about the GOAL.
    Only the errand-scoped wake names a thread."""
    from app.services import go_wake_events as ev
    src = inspect.getsource(ev)
    # COUNTS THE RESOLUTION, not the call shape: `on_execution_parked`
    # hoists it into a variable now (the approver notice needs the same
    # thread), so counting `thread_id=await ...` found nothing and read
    # that as "no wake is errand-scoped" -- the opposite of the truth.
    named = src.count("_errand_thread(pool, execution)")
    assert named == 1, (
        f"{named} wakes name an errand thread; only the park should")


def test_a_finished_errand_still_reports_to_the_goal_thread():
    """Deliberate, and the exception that proves the rule: `story_done`
    fires ONCE per run, its own text tells the GO to acknowledge in the
    goal thread, and a completed errand is news the team wants. It is
    the repeated parks that were the cost, not the conclusion."""
    from app.services import go_wake_events as ev
    src = inspect.getsource(ev.on_story_done) if hasattr(
        ev, "on_story_done") else inspect.getsource(ev)
    i = src.index('"story_done"')
    assert "_errand_thread" not in src[i:i + 200]


def test_the_humans_own_message_uses_the_errand_session():
    """The gateway session key carries the thread, so a message sent
    with the main thread lands in the main session however the page
    looked."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    router = (root / "app/routers/goal_thread.py").read_text(encoding="utf-8")
    i = router.index('"type": "message", "content": content')
    assert '"thread_id": go_thread_id' in router[i:i + 200]
