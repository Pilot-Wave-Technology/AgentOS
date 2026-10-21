"""The live ticker: what it shows, and the two things it must never do.

WHY IT EXISTS. A story runs for minutes between cards and, from the thread,
that is indistinguishable from a story that has stalled. The ticker says "read
this, wrote that, ran this" — enough to see movement, not enough to be a second
copy of the work.

THE TWO PROPERTIES, and both are structural rather than promised:

  it never enters the assistant's context — nothing writes to `messages`, so
  no tool line is ever in the assistant's next prompt. Forty lines a leg would
  crowd out the conversation the ticker is meant to sit beside, and the
  assistant would start narrating a feed instead of talking to a person.

  it never wakes the assistant — no gateway call, no dispatch, no NOTIFY. A
  person watching costs one indexed query per poll and the assistant does not
  learn that anybody is watching.

The tests below assert those against the SOURCE, because they are properties of
what the code does not do, and nothing in a return value can show an absence.
"""
from __future__ import annotations

from app.services.executor_activity import HIDDEN, summarise

REPO = "repos/Pilot-Wave-Technology__multi_agent_dummy_repo"


# ── what a person sees ───────────────────────────────────────────────────

def test_a_read_shows_its_path():
    got = summarise("read", {"path": f"{REPO}/app/main.py"})
    assert got["tool"] == "read"
    assert got["detail"] == f"{REPO}/app/main.py"
    assert got["family"] == "read"


def test_a_write_shows_its_path_and_never_its_content():
    """The one that matters most. `write` carries the whole file in `args`."""
    got = summarise("write", {"path": f"{REPO}/README.md",
                              "content": "SECRET BODY TEXT " * 200})
    assert got["detail"] == f"{REPO}/README.md"
    assert "SECRET BODY TEXT" not in got["detail"]
    assert got["family"] == "write"


def test_bash_shows_the_command_clipped():
    got = summarise("bash", {"command": "pytest " + "x" * 300})
    assert got["detail"].startswith("pytest")
    assert len(got["detail"]) <= 88
    assert got["detail"].endswith("…")
    assert got["family"] == "exec"


def test_a_token_in_a_command_is_redacted():
    """A bash command is the one field carrying whatever the model typed, and
    `git push https://x-access-token:ghs_...` is a real shape."""
    for secret in ("ghs_" + "a" * 36, "sk-" + "b" * 32,
                   "AIza" + "c" * 30, "eyJab" + "c" * 20 + ".dddddddddd.eeee"):
        got = summarise("bash", {"command": f"curl -H 'Bearer {secret}' x"})
        assert secret not in got["detail"], secret
        assert "<redacted>" in got["detail"], secret


def test_the_bulky_tools_are_summarised_not_dumped():
    """`register_plan` and `propose_completion` carry the whole plan and the
    whole claim. A ticker that printed them would be a wall."""
    got = summarise("register_plan", {"steps": [1, 2, 3], "checks": [1],
                                      "rationale": "z" * 500})
    assert got["detail"] == "3 step(s), 1 check(s)"
    assert "z" not in got["detail"]

    got = summarise("propose_completion", {"reason_set": ["a" * 400],
                                           "learned": [{"fact": "x" * 400}]})
    assert got["detail"] == "claimed the work is done"


def test_internal_bookkeeping_is_hidden():
    """Showing these would make the ticker mostly noise, which is how a live
    view stops being read."""
    for tool in HIDDEN:
        assert summarise(tool, {}) is None
    assert summarise("", {}) is None


def test_an_unknown_tool_is_named_but_not_described():
    """A default that guessed at the first argument is how a future tool's
    secret argument ends up on a screen."""
    got = summarise("some_future_tool", {"api_key": "ghs_" + "z" * 36,
                                         "path": "/x"})
    assert got["tool"] == "some_future_tool"
    assert got["detail"] == ""
    assert "ghs_" not in str(got)


def test_no_tool_whatsoever_can_make_the_ticker_fail():
    """SHOWN, NEVER AN ERROR. A tool added to the kernel next month must appear
    in the ticker the day it ships, without anyone remembering to come here.

    So the shapes below are not hypothetical inputs — they are what a caller
    can actually put in `step_journal.tool` and `args`, and each one must come
    back as a renderable line rather than an exception.
    """
    weird = [
        ("brand_new_tool", {"whatever": 1}),
        ("mcp__server__do_thing", {"deep": {"nested": [1, 2]}}),
        ("Tool With Spaces", None),
        ("emoji_🚀", []),                  # args not a dict at all
        ("x" * 300, {"a": "b"}),                   # absurd name
        ("t", 12345),                              # args not even JSON
        ("t2", '{"broken": '),                     # truncated JSON text
    ]
    for tool, args in weird:
        got = summarise(tool, args)
        assert got is not None, tool
        assert got["tool"] == tool.strip()
        assert got["detail"] == ""                 # never a guessed argument
        assert got["family"] == "other"            # the one fixed colour


def test_every_family_has_a_colour_and_other_is_the_catch_all():
    """The server can emit a family the page has no colour for, and the page
    can be older than the server. Both directions must land somewhere."""
    import pathlib
    import re

    from app.services.executor_activity import EXTRA_FAMILIES, FAMILY

    src = _template()
    block = re.search(r"const TICKER_TONE = \{(.*?)\};", src, re.S)
    assert block, "the ticker palette is gone"
    toned = set(re.findall(r"(\w+):\s*'text-", block.group(1)))

    missing = sorted((set(FAMILY.values()) | EXTRA_FAMILIES) - toned)
    assert not missing, f"these families render unstyled: {missing}"
    assert "other" in toned, "unrecognised tools have no colour"
    # And the page falls back rather than rendering a bare class.
    assert "TICKER_TONE[s.family] || TICKER_TONE.other" in src


def test_args_may_arrive_as_json_text():
    """`step_journal.args` is jsonb but a caller may hand over the raw text."""
    got = summarise("read", '{"path": "a/b.py"}')
    assert got["detail"] == "a/b.py"
    assert summarise("read", "not json at all")["detail"] == ""


def test_families_group_by_kind_not_by_tool():
    assert summarise("glob", {"pattern": "**/*"})["family"] == "read"
    assert summarise("list_dir", {"path": "x"})["family"] == "read"
    assert summarise("env_install", {"manager": "pip",
                                     "packages": ["pytest"]})["family"] == "exec"
    assert summarise("story_commit", {"message": "m"})["family"] == "commit"



def _code_of_activity_path() -> list[str]:
    """The activity path's CODE, with comments and docstrings removed.

    The first version of these guards grepped the raw source and failed on the
    module's own docstring, which says "no gateway call, no dispatch, no
    NOTIFY" — a test defeated by the prose describing the property it checks.
    Stripping to executable text is the fix: what matters is what the code
    calls, not what it says about itself.
    """
    import ast
    import inspect
    import textwrap

    from app.services import executor_activity as act
    from app.services import executor_feed as feed
    from app.services.kernel_run_control import activity

    out = []
    for src in (inspect.getsource(act), inspect.getsource(feed),
                inspect.getsource(activity)):
        # `textwrap.dedent`, never `inspect.cleandoc` — cleandoc strips the
        # leading whitespace from EVERY line, which flattens a function body
        # into an IndentationError. It is for docstrings, not for source.
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            # Drop every docstring; comments never reach the AST at all.
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:] or [ast.Pass()]
        out.append(ast.unparse(tree))
    return out


# ── the two guarantees ───────────────────────────────────────────────────

def test_nothing_in_the_activity_path_writes_anything():
    """A row in `messages` is a row in the assistant's next prompt. This is the
    guarantee the whole feature rests on, so it is checked rather than
    described."""
    for src in _code_of_activity_path():
        lowered = src.lower()
        for forbidden in ("insert into", "update ", "delete from"):
            assert forbidden not in lowered, (
                f"the activity path must only READ — found {forbidden!r}")


def test_the_activity_path_never_reaches_the_gateway():
    """No dispatch, no NOTIFY, no session. A person watching must not cost the
    assistant a turn, and must not tell it anybody is watching."""
    for src in _code_of_activity_path():
        for forbidden in ("gateway", "pg_notify", "session_manager",
                          "dispatch", "notify"):
            assert forbidden not in src.lower(), (
                f"the ticker must not wake anything — found {forbidden!r}")


def test_the_ticker_is_off_by_default_and_renders_client_side_only():
    """Read off the template: the strip is built in the DOM and posted
    nowhere."""
    src = _template()
    assert "let runTickerOn = false;" in src, "it must start off"
    assert 'id="run-ticker" class="hidden' in src, "and start hidden"
    # Model-written text into the DOM — textContent, never innerHTML.
    assert "detail.textContent = s.detail" in src
    assert "detail.innerHTML" not in src


def _template() -> str:
    """The ticker's markup AND its script, which are now two files.

    Both were inline in chat/index.html, so the ticker existed on the
    assistant's thread and nowhere else -- and an approved errand, whose
    conversation lives on the GO's thread, had no sight of its executor
    at all. They are shared now: the markup is a partial both pages
    include, the behaviour a module both pages load.

    Read together, because what these tests check is a PROPERTY of the
    ticker -- where it sits, that it is off by default, that switching
    it on shows something -- and that property is still one thing even
    though it is written in two places.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    page = root.joinpath("app", "templates", "chat",
                         "index.html").read_text(encoding="utf-8")
    partial = root.joinpath("app", "templates", "_shared",
                            "run_ticker.html").read_text(encoding="utf-8")
    script = root.joinpath("static", "js",
                           "run_controls.js").read_text(encoding="utf-8")
    # EXPANDED WHERE THE INCLUDE SITS, not concatenated in front of the
    # page. One of these tests asserts DOCUMENT ORDER -- the ticker
    # between the transcript and the composer -- and that is a real
    # property of the rendered page, so the text this reads has to be in
    # the order a browser would see it.
    marker = '{% include "_shared/run_ticker.html" %}'
    assert marker in page, "the page no longer includes the ticker"
    return page.replace(marker, partial, 1) + chr(10) + script


def test_the_ticker_sits_at_the_FOOT_of_the_transcript():
    """Placement is the feature, not decoration.

    It first shipped in the header stack, above `#messages`, and from the
    page that reads as a banner laid over the conversation rather than as
    part of it. At the foot it is the tail of the chat: the eye goes down
    the transcript and straight into what the executor is doing now.

    Three positions, in order: the message list, then the ticker, then the
    composer. Asserted by offset because the template has no other handle on
    document order, and the ordering is exactly what regressed.
    """
    src = _template()
    messages = src.index('id="messages"')
    ticker = src.index('id="run-ticker"')
    composer = src.index('id="msg-input"')
    assert messages < ticker < composer, (
        "the ticker must sit between the transcript and the composer")

    # And it must not reintroduce a divider — a rule above it is what made it
    # read as a separate panel instead of as more chat. Scoped to the ticker's
    # OWN tag: a 400-char window past it caught the composer's `border-t`,
    # which is a rule the composer is supposed to have.
    tag = src[ticker:src.index(">", ticker)]
    assert "border-t" not in tag and "border-b" not in tag, (
        f"no rule around the ticker, it should read as part of the "
        f"transcript — found: {tag!r}")
    assert "px-5" in tag, "same gutter as the messages, or it reads as a panel"


def test_toggling_off_removes_it_from_the_page():
    """Off means gone, not empty. An empty bordered strip still takes space
    and still says something is there."""
    src = _template()
    assert "strip.classList.toggle('hidden', !runTickerOn);" in src
    # ...and the lines are dropped, so turning it back on does not show a
    # stale tail from ten minutes ago.
    assert "tickerLines().innerHTML = '';" in src


def test_every_tool_this_database_has_seen_is_mapped():
    """The catalogue must keep up with the executor.

    An unmapped tool renders as its bare name with no detail — safe, and
    useless. Worse, an internal one renders as noise: `null_step` alone is 95
    rows in this database and would have been most of what a person saw.

    This is the only test here that needs a database, because the question it
    asks is "what does the executor ACTUALLY emit" and no static list can
    answer that.
    """
    import asyncio
    import contextlib

    import pytest

    from app.services.executor_activity import FAMILY, HIDDEN
    from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

    async def run():
        conn = await _connect_or_skip()
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='kernel' AND table_name='step_journal'"):
                pytest.skip("no kernel schema here")
            tools = {r["tool"] for r in await conn.fetch(
                "SELECT DISTINCT tool FROM kernel.step_journal "
                "WHERE tool IS NOT NULL AND tool <> ''")}
        finally:
            await conn.close()

        unmapped = sorted(tools - set(FAMILY) - HIDDEN)
        assert not unmapped, (
            f"these tools render as a bare name with no detail: {unmapped}. "
            f"Give each one a family and a summary, or add it to HIDDEN if it "
            f"is internal bookkeeping.")
    asyncio.run(run())


def test_a_turn_that_called_nothing_is_shown_not_hidden():
    """`no_tool` is a turn where the model SPOKE and called nothing.

    Hidden, a run doing only that shows a ticker that has gone quiet — which
    reads exactly like being between legs. That is the failure loop.py:568
    records: "a story spent TWELVE legs looking like a model sitting on its
    hands". The ticker exists to tell those two apart, so this is the last
    thing it should suppress.

    It is 2% of this database's steps and never more than five in one run, so
    showing it costs almost nothing.
    """
    from app.services.executor_activity import HIDDEN

    assert "no_tool" not in HIDDEN
    got = summarise("no_tool", {})
    assert got is not None
    assert got["family"] == "idle"
    assert "without calling a tool" in got["detail"]

    # And the provider fault behind that twelve-leg run.
    got = summarise("provider_empty", {})
    assert got["family"] == "idle"
    assert "returned nothing" in got["detail"]


# ── the verification gap ─────────────────────────────────────────────────

def test_the_ticker_covers_verification_not_just_the_executor():
    """The reported bug: it went blank at VERIFYING.

    `step_journal` is the EXECUTOR's log. When it claims completion the run
    moves to VERIFYING, the executor stops, and the adjudicator works in a
    clean container writing nothing to that table. On run 1749 that gap ran
    four minutes — and it lands exactly when a person is waiting to hear
    whether the work passed, so a blank strip there reads as a dead feature.

    So `activity` must read BOTH tables, and the page must render both.
    """
    import inspect

    from app.services import executor_feed as feed
    from app.services.kernel_run_control import activity

    src = inspect.getsource(activity)
    assert "checks_since" in src, "verification outcomes are not being read"
    assert "criteria_count" in src, "no count to say how many checks are running"

    assert "criterion_results" in inspect.getsource(feed.checks_since)
    assert "step_journal" in inspect.getsource(feed.steps_since)

    page = _template()
    assert "data.checks" in page, "the page ignores the verification feed"
    assert "VERIFYING:" in page, "no wording for the phase that went blank"


def test_a_rerun_check_collapses_to_one_line():
    """A re-run check writes one row per attempt — criterion 537 on run 1749
    has three identical failures. Three lines say nothing the first does not,
    so the attempt that settled it is the one shown."""
    import inspect

    from app.services import executor_feed as feed

    assert "run_index" in inspect.getsource(feed.checks_since), (
        "reruns are not being collapsed")


def test_the_cursors_never_go_backwards():
    """Two ways this bit.

    A tail whose newest row is HIDDEN — a `boundary_prompt` closing a leg,
    the most common last row there is — must still advance the cursor, or
    that row is re-read on every poll for as long as the leg lasts.

    And a story between runs answers `available: false`; resetting the
    cursors there would make the browser replay the whole journal of the
    NEXT run from its first row.
    """
    import inspect

    from app.services import executor_feed as feed
    from app.services.kernel_run_control import _no_activity, activity

    assert _no_activity(1234, 56) == {
        "available": False, "steps": [], "checks": [],
        "last_seq": 1234, "last_check": 56}
    assert "_no_activity" in inspect.getsource(activity)

    # The scanned row, not the shown one.
    assert 'rows[-1]["seq"]' in inspect.getsource(feed.steps_since)


def test_switching_it_on_shows_something_immediately():
    """It used to jump to the tail cursor and wait for the next tool call.
    Parked, between legs, or verifying, that call never comes — which is how
    a working ticker looks broken."""
    import inspect

    from app.services import executor_feed as feed
    from app.services.kernel_run_control import activity

    assert "seed" in inspect.getsource(activity)
    src = inspect.getsource(feed.steps_tail)
    assert "ORDER BY seq DESC" in src, "the backlog must come off the tail"
    assert "NOT (tool = ANY(" in src, (
        "hidden tools must be excluded in SQL, or `count` is not a count of "
        "lines anybody sees")

    assert "seed=8" in _template(), "the page never asks for a backlog"


def test_a_check_line_shows_which_check_and_how_it_went():
    """Colour alone cannot say WHICH of five checks failed."""
    from app.services.executor_feed import _as_check

    row = {"id": 7, "criterion_id": 537, "claim_seq": 138, "run_index": 2,
           "passes": "fail", "label": "guard",
           "spec": '{"run": "cd repos/x@cal_backend && pytest tests/t.py"}'}
    got = _as_check(row)
    assert got["family"] == "fail"
    assert "guard" in got["tool"]
    assert "pytest tests/t.py" in got["detail"]

    row["passes"], row["label"] = "pass", "goal"
    assert _as_check(row)["family"] == "pass"

    # A spec that is already a dict, and one that is neither.
    row["spec"] = {"run": "make test"}
    assert _as_check(row)["detail"] == "make test"
    row["spec"] = None
    assert _as_check(row)["detail"] == ""
