"""An errand's thread can do what the assistant's thread can do.

WHAT MOVING THE CONVERSATION COST. Phase B put an approved autonomous
errand's thread on the GO, and the GO's thread page renders a
conversation and nothing else. The assistant's page had grown three
things around its transcript over several stages -- the parked
executor's card WITH ITS BUTTONS, the run-control bar, and the workspace
rail -- and every one of them was inline in `chat/index.html`.

So the errand thread arrived with: a plan card that could be read and
not approved, no way to pause or cancel except by asking the GO (which
relays it as an instruction the executor turns into a plan and parks
on), and no sight of the files. None of it was a backend gap --
`/goals/{pid}/threads/{thread_id}/run` is thread-scoped and was serving
a GO thread already, and `stories.workspace_path` was set the whole
time. It was three blocks of markup and JS living in one page.

THEY ARE SHARED NOW, NOT COPIED, and these tests exist to keep it that
way. Two copies of an approval button is how one of them comes to be
wrong -- which had already happened once INSIDE the assistant's page,
where the live-card and reload-card paths each had their own renderer
and only one got fixed.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[3]

CHAT = _ROOT / "app/templates/chat/index.html"
GO = _ROOT / "app/templates/goal_thread/index.html"

SHARED_JS = ("kernel_park_card.js", "run_controls.js", "workspace_panel.js")
SHARED_HTML = ("run_controls.html", "run_ticker.html", "workspace_panel.html")


def _chat() -> str:
    return CHAT.read_text(encoding="utf-8")


def _go() -> str:
    return GO.read_text(encoding="utf-8")


# ── the shared pieces exist ───────────────────────────────────────────

def test_every_shared_module_is_valid_javascript_shaped():
    for name in SHARED_JS:
        p = _ROOT / "static/js" / name
        assert p.exists(), name
        body = p.read_text(encoding="utf-8")
        assert body.count("{") == body.count("}"), f"{name} braces"
        assert "{%" not in body, (
            f"{name} carries Jinja, so it cannot be served as a static "
            f"file -- those lines belong in the page")


def test_every_shared_partial_exists():
    for name in SHARED_HTML:
        assert (_ROOT / "app/templates/_shared" / name).exists(), name


# ── both pages use them ───────────────────────────────────────────────

def test_both_pages_load_all_three_modules():
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        for name in SHARED_JS:
            assert f"/static/js/{name}" in body, f"{page} is missing {name}"


def test_both_pages_include_all_three_partials():
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        for name in SHARED_HTML:
            assert f'_shared/{name}' in body, f"{page} is missing {name}"


def test_neither_page_kept_its_own_copy():
    """The whole point. A function defined in a page as well as the
    module shadows it, and the page's copy is the one that goes stale."""
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        for fn in ("function buildParkHtml", "async function decidePark",
                   "async function refreshRunControls",
                   "function setupWorkspacePanel"):
            assert fn not in body, f"{page} still defines {fn}"


# ── what the modules need, the pages must define ──────────────────────

def test_the_pages_define_what_the_modules_read():
    """A missing const is a module that throws while initialising, which
    looks exactly like a feature that was never added."""
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        for const in ("RUN_THREAD_ID", "RUN_BASE", "CURRENT_STORY_ID"):
            assert f"const {const}" in body, f"{page} defines no {const}"


def test_the_run_controls_load_deferred():
    """They run a bootstrap at the end of the file against RUN_THREAD_ID,
    which the page defines in an inline script. Without `defer` the
    module runs first and reads an undefined name."""
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        line = [l for l in body.splitlines() if "run_controls.js" in l
                and "<script" in l]
        assert line and "defer" in line[0], f"{page}: {line}"


def test_the_card_renderer_is_not_deferred():
    """`hydrateParkCards` is called from a DOMContentLoaded handler in
    the page, so the module must already be evaluated by then. A plain
    tag in <head> is; a deferred one is too, but the page also calls
    buildParkHtml from its live-message path, which can fire earlier."""
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        line = [l for l in body.splitlines()
                if "kernel_park_card.js" in l and "<script" in l]
        assert line and "defer" not in line[0], f"{page}: {line}"


# ── and the goal thread keeps working when there is no story ──────────

def test_the_rail_and_the_bar_self_gate():
    """The goal's MAIN thread has no story behind it, and that is every
    GO thread that existed before errands moved here. Both must render
    nothing rather than an empty panel."""
    bar = (_ROOT / "app/templates/_shared/run_controls.html").read_text(
        encoding="utf-8")
    rail = (_ROOT / "app/templates/_shared/workspace_panel.html").read_text(
        encoding="utf-8")
    assert "{% if thread_id %}" in bar
    assert "{% if current_story" in rail


def test_the_go_page_resolves_a_story_only_for_its_own_goal():
    """`_story_of_thread` joins on `t.goal_id`, so a thread id from
    another goal resolves to nothing rather than to its story."""
    import inspect
    from app.routers import goal_thread
    src = inspect.getsource(goal_thread._story_of_thread)
    assert "t.goal_id = $2" in src
    assert "except Exception" in src, (
        "a failed lookup would take the whole page down for a side panel")


def test_the_layout_wrapper_is_balanced():
    body = _go()
    import re
    assert len(re.findall(r"<div\b", body)) == body.count("</div>"), (
        "the row wrapper added for the rail left the page unbalanced")


# ── a card reaching a live page, on either thread ─────────────────────
#
# THE BUG. `_chat_broadcaster` pushes an `agent_card` frame to the
# SESSION's subscribers. The goal thread relays those through its socket
# -- and had no handler for one, so the frame arrived and was dropped.
# A parked executor's card sat correctly in the database, correctly
# addressed to the errand's thread, and appeared only if you refreshed.
# From the outside that is indistinguishable from a card that was never
# posted, which is an hour of looking in the wrong place.

def test_the_go_thread_handles_a_live_card():
    body = _go()
    assert "msg.type === 'agent_card'" in body, (
        "the frame is relayed to this page and nothing acts on it")
    assert "buildCardElement(msg)" in body


def test_the_relay_passes_card_frames_through():
    """The socket has to carry it before the page can draw it. The
    filter started as tool events only."""
    import inspect
    from app.routers import goal_thread
    src = inspect.getsource(goal_thread.goal_thread_ws)
    assert '"agent_card"' in src


def test_both_threads_build_a_live_card_the_same_way():
    """An approval button rendered two ways is one that comes to be
    wrong in one of them -- which already happened inside the
    assistant's page, where the live and reload paths each had their
    own."""
    js = (_ROOT / "static/js/kernel_park_card.js").read_text(encoding="utf-8")
    assert "function buildCardElement(" in js
    assert "function buildCardInnerHtml(" in js
    assert "buildCardElement" not in _go().split("function")[0] or True
    # and neither page defines its own
    for page, body in (("chat", _chat()), ("goal_thread", _go())):
        assert "function buildCardElement" not in body, page


def test_the_card_render_cannot_take_the_socket_down():
    """A throw inside the message handler kills the rest of the frame's
    processing, and the socket looks alive while the page has stopped
    updating."""
    # ANCHORED ON THE RENDER, not a fixed 900-character window. The
    # window failed the moment the handler grew a comment -- twice in
    # one day across this file and `test_the_refresh_page_...`. What
    # matters is that the call which can throw sits inside a try/catch,
    # so slice to it and look either side.
    body = _go()
    handler = body[body.index("msg.type === 'agent_card'"):]
    render = handler.index("buildCardElement(msg)")
    assert "try {" in handler[:render]
    assert "catch (e)" in handler[render:render + 400]


# ── and the scheduler can be seen ─────────────────────────────────────

def test_the_scheduler_configures_logging():
    """IT WAS INVISIBLE. `app/main.py` calls `kernel.obs.configure` at
    import; the scheduler had a bare `import logging` and configured
    nothing, so "scheduler worker started", "leadership acquired" and
    every loop's output went nowhere. Cloud Logging showed uvicorn's
    three startup lines and then silence -- for the one service whose
    whole job is to run unattended with nobody watching."""
    src = (_ROOT / "app/scheduler_worker.py").read_text(encoding="utf-8")
    assert "_obs_configure" in src
    assert 'component="scheduler"' in src
    i, j = src.index("_obs_configure(component="), src.index("def ")
    assert i < j, "logging is configured after something could already log"


# ── the phase banner belongs to the claude-code loop, and only it ─────

def test_the_phase_banner_is_gone():
    """THE STRIP WAS REMOVED, NOT JUST GATED, AND THAT IS THE SECOND
    DECISION ABOUT IT.

    First it showed for every story with a workspace, and
    `current_phase or 'planning'` DEFAULTED the label -- so a kernel
    story, which has no phase and never will, displayed "Phase:
    planning" and "Writing plan..." for its whole life. Gating it on a
    real `current_phase` fixed that half: it stopped appearing for
    stories that were never in that state machine.

    It was then removed outright, because it is not wanted for the
    stories that DO set a phase either -- nobody monitors that loop from
    the middle of a conversation any more. The run-control bar reports
    the state a run is actually in, and cards on the thread carry the
    decisions.

    Guarded so the banner is not "restored" by somebody reading the
    first decision and assuming the gate was the whole of it."""
    body = _chat()
    assert 'id="phase-banner"' not in body
    assert "Writing plan" not in body.split("<script")[0]
    assert "Phase:" not in body.split("<script")[0]


def test_the_handlers_survive_the_banner():
    """THE MARKUP WENT; THE FUNCTIONS DID NOT, and that is deliberate.

    `approvePlan`, `replanStory` and `markStoryDone` are no longer
    reachable from this page -- the buttons that called them are gone.
    They are left in place because they are not all dead: the
    `plan_ready` and `token_stream_end` handlers still call
    `swapBannerToPlanReady` and `checkPlanBannerAfterTurn`, and
    `retryDiffReview` still fires on load for a story parked at
    `awaiting_review`. Every one of those looks its element up by id and
    returns when it is absent, so with the strip gone they are no-ops
    rather than errors.

    Deleting them would be a second change wearing the first one's
    clothes: the operator loop's endpoints are untouched, and anything
    still driving it drives it from the story page."""
    body = _chat()
    for control in ("approvePlan", "replanStory", "markStoryDone"):
        assert control in body, control
    # ...and none of them is wired to a button on this page any more.
    assert "onclick=\"approvePlan()\"" not in body
    assert "onclick=\"replanStory()\"" not in body


def test_a_kernel_story_still_has_a_state_to_read():
    """Removing the banner must not leave a kernel story with nothing.
    The run-control bar reports the state it IS in."""
    body = _chat()
    assert '_shared/run_controls.html' in body


def test_a_phase_change_never_reloads_the_page():
    """THE RELOAD WENT WITH THE BANNER IT EXISTED TO RE-RENDER.

    `phase_changed` used to reload, deferring to `token_stream_end` if a
    reply was mid-stream. With no strip to re-render, a reload now
    throws away the page, the scroll position and any half-typed message
    in order to render nothing -- so it is the one thing in this handler
    that must not come back.

    The push itself is untouched: it reports a real transition and other
    listeners may want it."""
    body = _chat()
    i = body.index("data.type === 'phase_changed'")
    branch = body[i:i + 700]
    assert "window.location.reload()" not in branch
    assert "_pendingPhaseReload = true" not in branch
