"""GO's prompt and its toolset, and what I got wrong checking them.

I read `kernel/go/tools.py` (twenty-nine tools), compared it with the
prompt, and concluded two things that were both false:

  - that `record_decision` was a phantom, because it appears nowhere in
    the codebase. It is a GATEWAY tool, allowed by the `go-v2` template
    and stored in `templates.tools_allow`, so GO does hold it. Removing
    its row meant GO held a tool it was never told how to use.
  - that "Thirty-six tools" in the opening line was stale against a
    registry of twenty-nine. The count is of the TABLE, not that
    registry, and `test_the_stated_tool_count_matches_the_table` has
    been checking it all along.

Both were caught by tests that already existed. The authority on GO's
toolset is the template allowlist PLUS the kernel registry, and
`test_gateway_surface` already reads the first from the database -- so
the "is every tabled tool real" direction belongs there, with the data,
not here. This file keeps what source alone can prove: that every kernel
tool is documented, and that the check-in says what it must.
"""

from __future__ import annotations

import ast
import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
MD = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()


def _kernel_tools() -> set[str]:
    src = io.open(ROOT / "kernel/go/tools.py", encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "TOOLS" for t in n.targets):
            return {e.value for e in n.value.elts
                    if isinstance(e, ast.Constant)}
    raise AssertionError("kernel/go/tools.py no longer defines TOOLS")


def test_every_kernel_tool_is_documented():
    """A tool GO holds and has never been told about is a capability it
    will not use. The reverse direction -- a tabled tool that does not
    exist -- needs the template allowlist and lives in
    `test_gateway_surface`, which reads it from the database."""
    undocumented = sorted(t for t in _kernel_tools() if f"`{t}`" not in MD)
    assert not undocumented, (
        "GO holds kernel tools its prompt never mentions: "
        + ", ".join(undocumented))


# -- the check-in sequence ---------------------------------------------

def _section() -> str:
    sec = MD[MD.index("On that check-in"):]
    return sec[:sec.index("Silence means nothing is wrong")]


def test_the_check_in_orders_ask_before_the_journal():
    """RUNS 307 AND 308. The section said to ask; the journal was read
    both times, and both times the thread was told "I checked in with
    the executor". A journal read is cheaper and never blocks, so it
    wins unless the order is explicit."""
    sec = _section()
    assert "1. **Call `ask_executor`.**" in sec
    assert sec.index("ask_executor") < sec.index("story_progress")


def test_the_journal_is_named_as_the_fallback():
    assert "FALLBACK only" in _section()


def test_it_says_the_answer_arrives_as_its_own_wake():
    """THE FIRST WORDING ASKED FOR SOMETHING IMPOSSIBLE. It said "wait
    for the answer" -- but `ask_executor` is asynchronous: it returns
    the moment the question is proposed, and the executor replies at its
    next step boundary (four and a half minutes, measured on run 310).
    GO cannot wait inside the turn.

    Watched live, GO worked out the only sensible behaviour unaided --
    ask, report the journal as an interim and say so, then report again
    when the real answer woke it. The prompt now says that."""
    sec = _section()
    assert "asynchronous" in sec
    assert "[EXECUTOR REPLIED]" in sec
    assert "do not sit on the turn" in sec


def test_a_fallback_must_be_declared():
    """A journal reading and an executor's answer are not the same
    evidence, and a report that does not say which it used invites the
    reader to assume the stronger one."""
    assert "say so in your report" in _section()


def test_the_wake_text_carries_the_same_sequence():
    """The prompt is read once a session; the wake arrives at the moment
    it applies. Both have to say it, and say the same thing."""
    src = io.open(ROOT / "app/services/go_errand_watch.py",
                  encoding="utf-8").read()
    assert "`ask_executor`" in src
    assert "story_progress" in src
    assert "interim only" in src


# -- and nothing else in the prompt may contradict it ------------------

def test_the_tool_table_does_not_say_the_journal_comes_first():
    """THE ACTUAL REASON GO WOULD NOT ASK, and it was not a weak
    preference. The tool table said `story_progress` is "**first,
    always.** ... answers most questions", while the check-in section
    said to ask the executor. Two instructions, the more general one
    winning, and GO read the journal on runs 307 and 308 exactly as the
    table told it to.

    Rewriting the check-in section alone would have changed nothing."""
    row = MD[MD.index("| `story_progress` |"):].splitlines()[0]
    assert "first, always" not in row, (
        "the tool table still overrides the check-in sequence")
    assert "NOT first on a scheduled" in row


def test_the_tool_table_sends_a_check_in_to_the_executor():
    row = MD[MD.index("| `ask_executor` |"):].splitlines()[0]
    assert "first on a scheduled check-in" in row


def test_the_wake_table_names_the_ask():
    """`[ERRAND PROGRESS]` listed `decide_plan`, `relay_answer`, `steer`
    and `run_control` and never mentioned asking -- so the one row GO
    consults when a check-in arrives did not contain the instruction the
    check-in is for."""
    row = MD[MD.index("| `[ERRAND CHECK-IN]` |"):].splitlines()[0]
    assert "ask the executor" in row
    assert "test the answer against the plan" in row


def test_the_wake_table_asks_for_a_report():
    """A check-in nobody hears about is a turn spent for nothing, and on
    run 308 GO approved a plan and said nothing at all."""
    row = MD[MD.index("| `[ERRAND CHECK-IN]` |"):].splitlines()[0]
    assert "report" in row
