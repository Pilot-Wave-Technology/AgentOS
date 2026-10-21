"""The refresh page must read the repository, not only the table.

WHAT HAPPENED, because it is the whole reason this file exists. The
chain from a prompt edit to a live agent has two links:

    repo AGENTS.md --[sync_all]--> template_files --[refresh]--> gateway

`sync_all` ran only inside the boot resync in `app/main.py`. That resync
was switched off on 2026-09-23, on the stated grounds that the refresh
page "now does the same push" -- true of the push, false of the
reconcile. The first link was gone and nothing said so.

On 2026-09-24 a fleet refresh reported `12 ok, 0 failed, 0 PARTIAL` and
delivered the PREVIOUS build's prompts: six GOs without the section
written that morning, six assistants still telling people they had four
stories in flight. It was found by reading the files on the gateway VM,
which is the only place the truth was visible.

WHY NOTHING COULD HAVE CAUGHT IT. `template_files` was internally
consistent; the push succeeded; every byte the page promised arrived.
A push of stale rows and a push of fresh ones render identically --
"2 file(s), ok" either way. The defect was not in anything the page
did, it was in what no longer happened before it.

So these tests assert the ORDER and the SAYING-SO, not the outcome of
one render: reconcile before read, and a report that distinguishes
"the source moved" from "the source was already the same".
"""

from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
RUN = io.open(ROOT / "app/services/agent_refresh/run.py",
              encoding="utf-8").read()


def _code(text: str) -> str:
    """Source with comments and docstrings stripped.

    A test in this repo has fired on the prose DESCRIBING an
    anti-pattern more than once, and this file's own docstring names
    every symbol it asserts on.
    """
    no_doc = re.sub(r'""".*?"""', "", text, flags=re.S)
    return "\n".join(line for line in no_doc.splitlines()
                     if not line.lstrip().startswith("#"))


def test_the_refresh_reconciles_from_disk():
    """The link that went missing. Without this call the page renders
    whatever `template_files` last held, for ever."""
    assert "sync_all" in _code(RUN), (
        "nothing pulls the repository's prompts into template_files, so "
        "the refresh cannot deliver a prompt change from this build")


def test_it_reconciles_BEFORE_it_renders():
    """Order is the whole fix. Reconciling after `desired_for` has read
    the table updates the rows and pushes the previous ones -- the same
    failure with an extra write."""
    code = _code(RUN)
    assert code.index("_reconcile_from_disk(conn)") < code.index(
        "desired_for(conn, a)"), (
        "the render reads template_files before the reconcile writes it")


def test_the_plan_reconciles_too_not_only_the_apply():
    """The plan is what a person reads before deciding. One rendered
    from rows the apply is about to replace describes a refresh that
    will not happen."""
    code = _code(RUN)
    body = code[code.index("async def refresh("):]
    before_apply = body[:body.index("if not apply:")]
    assert "_reconcile_from_disk(conn)" in before_apply, (
        "the reconcile is gated behind `apply`, so the preview lies")


def test_the_reconcile_cannot_take_the_page_down():
    """An unreadable prompt file must not 500 the page: the rows already
    in the table are still serviceable, and a refresh that only pushes
    is better than a page nobody can open. (That exact shape --- an
    exception escaping a helper on this page --- already 500'd it once,
    via an ImportError outside a try.)"""
    code = _code(RUN)
    fn = code[code.index("async def _reconcile_from_disk"):]
    fn = fn[:fn.index("async def refresh(")]
    assert "try:" in fn and "except" in fn
    assert "return False, []" in fn, (
        "the failure path must still return, and must say it failed")


def test_the_report_says_whether_the_source_moved():
    """THE LINE THAT WOULD HAVE CAUGHT IT. A push of stale rows reads
    exactly like a push of fresh ones, so the report has to name what
    the reconcile pulled in -- or say plainly that nothing did."""
    code = _code(RUN)
    assert "Reconciled from the repository first" in code
    # A CONTIGUOUS FRAGMENT. The sentence is split across source lines,
    # so a phrase that spans the wrap is never present in the file.
    assert "Nothing to reconcile" in code, (
        "a no-op reconcile must say so; silence is what made the stale "
        "push indistinguishable from a real one")


def test_main_no_longer_claims_the_page_only_pushes():
    """The comment that justified switching the boot resync off said the
    page 'does the same push'. It did -- and the reconcile, which is the
    other half of `resync`, was what went missing. A wrong comment is
    how the next person switches something else off."""
    main = io.open(ROOT / "app/main.py", encoding="utf-8").read()
    block = main[main.index("OFF BY DEFAULT SINCE 2026-09-23"):]
    block = block[:block.index("RESYNC_ON_BOOT_KEY, resync as _resync")]
    assert "RECONCILE AND PUSH" in block, (
        "main.py still describes the refresh page as push-only, which is "
        "the belief that made this switch quietly load-bearing")


# ── an empty result has two meanings ─────────────────────────────────

def test_a_failed_reconcile_is_not_reported_as_a_clean_one():
    """THE SAME BUG IN MINIATURE. `_reconcile_from_disk` returned a bare
    list, so "the repository already matched" and "the reconcile blew
    up" were both `[]` -- and the reassuring line written for the first
    would have been printed for the second. A page that says nothing
    changed when it could not look is exactly how the original defect
    survived a green apply."""
    code = _code(RUN)
    assert "tuple[bool, list[str]]" in code, (
        "the reconcile still cannot distinguish 'nothing to do' from "
        "'could not look'")
    assert "return True, changed" in code and "return False, []" in code
    assert "COULD NOT RECONCILE" in code, (
        "a failed reconcile must say so in the report, not only the logs")


def test_the_no_change_line_does_not_claim_the_agents_are_current():
    """It used to end "...so this refresh delivers what the agents may
    well already hold" -- an inference about the GATEWAY from the state
    of a TABLE, in a report that says in the same breath there is no
    read-back. The ordinary case is a second plan click after the first
    reconciled, where that sentence talks a person out of the apply
    that was the whole point."""
    code = _code(RUN)
    assert "agents may well already hold" not in code
    assert "a fact about the" in code and "not about the agents" in code
