"""The page has to teach the one thing that is not obvious.

A person arriving at "Refresh Agents" reasonably expects it to refresh
agents. It does not, quite: prompts land on disk and stay invisible until
the gateway restarts, because the gateway caches an agent's bootstrap
files per session. Somebody who applies, sees "ok" against every agent
and walks away has changed nothing they can observe, and will conclude
the tool is broken when the old prompt keeps answering.

So the restart requirement is on the page, above the button, in prose --
not in a tooltip and not only in the result. And the page must say why
`sessions.reset` is not used instead, because that is the obvious
shortcut and it destroys the conversations this whole design exists to
preserve.

Asserting on the template text is the right level here: there is no
logic to test, and the failure being guarded against is somebody
tidying the warnings away.
"""

from __future__ import annotations

import io

PAGE = "app/templates/agent_refresh/index.html"

# THE TEMPLATE `/settings` ACTUALLY RENDERS, which is not the obvious
# one. `control_panel/index.html` exists and carries a near-identical
# grid of the same cards, but `control_panel.py:17` renders
# `settings/index.html`. The first version of this test asserted against
# the other file, so it passed while the link was invisible to every
# user -- a false green that took a person looking at the page to catch.
SETTINGS = "app/templates/settings/index.html"


def _page() -> str:
    return io.open(PAGE, encoding="utf-8").read()


def test_the_page_says_prompts_need_a_restart():
    body = _page()
    assert "restart" in body.lower()
    assert "caches" in body.lower(), (
        "say WHY a restart is needed -- the per-session cache is the "
        "reason, and without it the requirement reads as superstition")


def test_the_page_promises_conversations_survive():
    """The fear this tool has to answer. Everyone who has deleted and
    recreated an agent knows it loses everything."""
    body = _page().lower()
    assert "preserved" in body or "keeps every conversation" in body


def test_the_page_explains_why_not_sessions_reset():
    """The obvious shortcut, and the one that would quietly destroy
    every conversation."""
    body = _page()
    assert "sessions.reset" in body
    assert "ends the conversation" in body


def test_the_page_warns_that_tools_go_live_immediately():
    """Apply and restart belong in the same window. Somebody who applies
    on Monday and restarts on Friday has spent four days with new tools
    and old prompts."""
    body = _page().lower()
    assert "immediately" in body
    assert "quiet window" in body or "same quiet" in body


def test_the_apply_button_asks_first():
    """Native `confirm()` rather than the app's `data-confirm`, because
    that attribute routes into `submitFormWithToast`, which redirects to
    a POST-only URL and 404s after a successful write."""
    body = _page()
    assert "confirm(" in body, (
        "a fleet-wide write should not be one misclick away")


def test_the_partial_warning_is_on_the_page():
    body = _page()
    assert "Do not restart yet" in body


def test_the_refusal_explains_the_memory_risk():
    """A 'refused, fleet busy' message that does not say why reads as an
    arbitrary lock, and the next person adds a force flag."""
    body = _page()
    assert "MEMORY.md" in body


def test_it_is_reachable_from_settings():
    """The first version shipped with no link at all, which is how a
    tool gets built and never used. The second put the link in a
    template nothing renders, which is worse, because the test passed."""
    import app.routers.control_panel as cp

    rendered = io.open(SETTINGS, encoding="utf-8").read()
    assert "/settings/agents/refresh" in rendered

    # And the route really does render THAT file. Without this, the
    # assertion above is satisfied by any template that happens to hold
    # the string.
    src = io.open(cp.__file__, encoding="utf-8").read()
    i = src.index('@router.get("/settings"')
    assert '"settings/index.html"' in src[i:i + 400], (
        "the settings index route no longer renders settings/index.html; "
        "this test is now checking a file nobody sees")


def test_the_router_passes_user_to_the_template():
    """THE BLANK SCREEN, guarded at the seam where it happened.

    `base.html` wraps the sidebar, the chrome and every script in
    `{% if user %}`, and Jinja's default undefined is FALSY rather than
    an error. So a context missing `user` renders a near-empty document
    with a 200 -- no exception, no log line, nothing for a source-reading
    test to notice. The page shipped exactly that way and a person had
    to open it to find out.

    Rendering the real template here would be better, but base.html
    reaches deep into the request object and stubbing it convincingly
    produced a test that skipped -- and a skipped guard is what let this
    through in the first place. So this checks the two facts that
    together make the bug impossible: base.html gates on `user`, and the
    router puts `user` in every context it builds.
    """
    import app.routers.agent_refresh_routes as routes

    base = io.open("app/templates/base.html", encoding="utf-8").read()
    assert "{% if user %}" in base, (
        "base.html no longer gates on `user`; if that is deliberate, this "
        "guard is obsolete -- but check the page still renders first")

    src = io.open(routes.__file__, encoding="utf-8").read()
    i = src.index("def _page(")
    body = src[i:src.index("async def _run(")]
    assert '"user": user' in body, (
        "the page context must carry `user`, or base.html renders "
        "nothing and the route returns a blank 200")

    # ...and every caller must supply it, not just the signature.
    assert "_page(request, goal_id" not in src, (
        "a _page call is missing user=; that caller renders blank")
    assert src.count("_page(request, user=user") >= 4, (
        "expected every _page call site to pass user")


def test_apply_does_not_use_the_toast_submit_helper():
    """THE 404 AFTER A SUCCESSFUL WRITE.

    `submitFormWithToast` in static/js/app.js assumes POST endpoints
    follow POST-redirect-GET: on `resp.ok` it sets
    `window.location.href = resp.url`, and for a 200 that is the URL it
    just posted to. The apply endpoint renders its report directly
    instead of redirecting, so the helper navigated to
    /settings/agents/refresh/apply as a GET, found no handler, and
    showed a 404 -- after both agents had already been written.

    The failure was entirely cosmetic and entirely misleading: the
    operator saw an error page and concluded nothing had happened.
    """
    body = _page()
    i = body.index('action="/settings/agents/refresh/apply"')
    form = body[max(0, i - 400):i + 400]
    assert "data-toast-submit" not in form, (
        "the apply form must submit plainly -- the toast helper "
        "redirects to a URL that only accepts POST")
    assert "confirm(" in form, (
        "a fleet-wide write still needs to ask first")


def test_the_boot_resync_is_off_unless_someone_turns_it_on():
    """OFF IS THE DEFAULT AND IT NEEDS NO MIGRATION.

    `platform_settings` reads a missing row as False, so not seeding the
    row IS the off switch. The boot path must therefore gate on the
    setting rather than on the row existing -- a `get_setting(...)
    ["value"]` check, not a `WHERE key = ...` presence test.
    """
    import io as _io

    from app.services.kernel_prompt_resync import RESYNC_ON_BOOT_KEY

    main = _io.open("app/main.py", encoding="utf-8").read()
    # ANCHORED ON THE IMPORT, not a fixed window around the first
    # mention of the module. It used to slice main[i-1500:i+1500], and
    # the first mention is in the PROSE above the code -- so adding a
    # paragraph of comment there pushed the actual gate out of the
    # window and failed a test about behaviour that had not changed.
    i = main.index("from app.services.kernel_prompt_resync import")
    block = main[i:i + 1200]
    assert "RESYNC_ON_BOOT_KEY" in block, (
        "the boot resync must be gated on the switch, not run "
        "unconditionally")
    assert "await _resync(" in block, (
        "the gate must sit between the import and the call, or it gates "
        "nothing")
    assert block.index("RESYNC_ON_BOOT_KEY") < block.index("await _resync("), (
        "the resync runs before the switch is read")
    assert RESYNC_ON_BOOT_KEY == "kernel_prompt_resync_on_boot"


def test_the_switch_says_it_does_not_take_effect_now():
    """It only ever runs at startup, so flipping it on mid-life changes
    nothing until the service restarts. A toggle that appears to do
    something immediately and does not is how people conclude a switch
    is broken."""
    body = _page()
    i = body.index("/settings/agents/refresh/resync")
    panel = body[max(0, i - 2000):i + 400]
    assert "next control-panel boot" in panel


def test_the_switch_explains_what_it_is_for():
    """Kept, not deleted, for one reason: an unplanned restart picking
    up current prompts. If the page does not say that, the next person
    reads it as dead code and removes it."""
    # Whitespace-normalised: the template wraps prose across lines, so a
    # raw substring match tests the line width rather than the wording.
    body = " ".join(_page().split())
    assert "does not make them live" in body
    assert "some other reason" in body or "unplanned" in body.lower()


def test_the_names_the_switch_imports_actually_exist():
    """A 500 ON EVERY PAGE LOAD, and the source-reading tests above all
    passed while it shipped.

    `_boot_on` imported `get_setting` from `app.services.platform_settings`,
    where the function is called `get`. The import sat OUTSIDE the
    try/except, so it was not a degraded switch reading -- it was an
    ImportError on every GET of /settings/agents/refresh.

    The guard above asserts `RESYNC_ON_BOOT_KEY` appears in main.py's
    source. Text was present; the symbol was not. So this one IMPORTS,
    which is the only way to find out whether a name resolves.
    """
    from app.services.kernel_prompt_resync import RESYNC_ON_BOOT_KEY
    from app.services.platform_settings import get, set_value

    assert RESYNC_ON_BOOT_KEY
    assert callable(get) and callable(set_value)

    # And the router must call them by those names, not by a name that
    # merely reads plausibly.
    import io as _io
    src = _io.open("app/routers/agent_refresh_routes.py",
                   encoding="utf-8").read()
    assert "import get_setting" not in src, (
        "platform_settings exposes `get`, not `get_setting`")


def test_the_switch_read_degrades_instead_of_raising():
    """Whatever goes wrong reading an operator switch, the page has to
    render. It reports OFF on failure, matching what the boot path does
    with the same failure -- the two must not disagree."""
    import inspect

    import app.routers.agent_refresh_routes as routes

    body = inspect.getsource(routes._boot_on)
    assert "try:" in body
    # The imports must be INSIDE it. Outside, an ImportError skips the
    # guard entirely -- which is exactly what happened.
    assert body.index("try:") < body.index("import"), (
        "the imports belong inside the try, or a bad name 500s the page "
        "instead of degrading to OFF")
