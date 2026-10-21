"""Everything added on 2026-09-24 stops at the autonomous errand.

Three new capabilities were given to the GO:

    it decides an errand's plan alone      decide_plan with no human_uid
    the kernel lets that approval through  resolve_request's exemption
    it is woken about a running errand     go_errand_watch

Each one is a reduction in how much a person sees, justified ONLY by the
fact that somebody already consented to this particular errand running
unattended. Applied to anything else they are just less oversight.

THE THREE THINGS THEY MUST NOT REACH:

    a STORY               it has an assistant whose whole job is that
                          story, and two agents answering one question
                          is how a person is told something was settled
                          that was not
    a HAND-STARTED errand somebody is sitting with it; that is what
                          hand-started means
    a REVOKED grant       consent withdrawn, and withdrawing it is the
                          lever a person has

ONE DEFINITION, REUSED. `kernel/go/drivable` owns what an autonomous
errand is, and every surface here defers to it or repeats its grant
clause verbatim. Two definitions would disagree the first time one
moved, and the disagreement would be authority applied where nobody
granted it.
"""

from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _code(path: str) -> str:
    text = io.open(ROOT / path, encoding="utf-8").read()
    no_doc = re.sub(r'""".*?"""', "", text, flags=re.S)
    return "\n".join(line for line in no_doc.splitlines()
                     if not line.lstrip().startswith("#"))


def _sql(path: str, name: str) -> str:
    """A triple-quoted SQL constant, which `_code` strips as a docstring."""
    text = io.open(ROOT / path, encoding="utf-8").read()
    body = text[text.index(f'{name} = """'):]
    return body[:body.index('"""', len(name) + 8)]


# ── the gate every surface goes through ──────────────────────────────

def test_the_dispatch_gate_covers_decide_plan():
    """`go_may_drive` refuses a story and a hand-started errand, and it
    is applied at dispatch for the whole gated family -- so the
    self-decision path cannot be reached by anything else."""
    code = _code("kernel/assistant/api.py")
    gated = code[code.index("go_may_drive"):]
    assert "decide_plan" in code
    block = code[:code.index("refused = await go_may_drive")]
    assert '"decide_plan"' in block, (
        "decide_plan is not in the go_may_drive-gated tool list")
    assert gated


def test_go_may_drive_wants_an_errand_and_a_live_grant():
    code = _code("kernel/go/drivable.py")
    assert "work_kind" in _sql("kernel/go/drivable.py", "_IS_ERRAND")
    grant = _sql("kernel/go/drivable.py", "_HAS_GRANT")
    assert "approved_by IS NOT NULL" in grant
    assert "revoked_at IS NULL" in grant
    assert "expires_at IS NULL OR s.expires_at > now()" in grant
    assert code


# ── and each new surface checks it AGAIN, at the moment of use ───────

def test_the_self_decision_rereads_the_grant():
    """Dispatch checked it when the turn began. A person may revoke
    between then and the call, and revoking has to bite immediately --
    it is the only lever they have once the errand is running."""
    grant = _sql("kernel/go/self_decide.py", "_GRANT")
    assert "errand_schedules" in grant
    assert "approved_by IS NOT NULL" in grant
    assert "revoked_at IS NULL" in grant
    assert "expires_at IS NULL OR s.expires_at > now()" in grant


def test_the_kernel_gate_does_not_trust_the_caller():
    """`go_autonomous` says "no person is behind this one". If that flag
    WERE the authority, it would be `answered_by` all over again -- a
    free field an agent fills in with its own claim, which is the hole
    H8a exists to close."""
    code = _code("kernel/harness/park_resolve.py")
    block = code[code.index("if is_plan and go_autonomous"):]
    block = block[:block.index("if ((is_plan")]
    assert "alone_ok" in block, (
        "the exemption trusts the caller's flag instead of re-checking")


def test_only_a_plan_is_exempt_never_an_acceptance():
    """Done-or-more-work is the decision the person kept, and it is what
    makes handing over the plan tolerable at all."""
    code = _code("kernel/harness/park_resolve.py")
    block = code[code.index("if is_plan and go_autonomous"):]
    block = block[:block.index("if ((is_plan")]
    assert "is_acceptance" not in block
    assert "is_permission" not in block
    assert "is_escalation" not in block


def test_the_wake_only_watches_autonomous_errands():
    live = _sql("app/services/go_errand_watch.py", "_LIVE")
    assert "'errand'" in live
    assert "errand_schedules" in live
    assert "approved_by IS NOT NULL" in live
    assert "revoked_at IS NULL" in live
    assert "terminal_states" in live, (
        "a finished errand would be woken about for ever")


def test_the_wake_never_reaches_a_story():
    """A story has an assistant. Waking GO about one is both waste and
    interference -- and `story_progress` refuses GO on a story for the
    same reason."""
    live = _sql("app/services/go_errand_watch.py", "_LIVE")
    assert "COALESCE(s.work_kind, 'story') = 'errand'" in live


# ── the prompt says the same thing the code enforces ─────────────────

def test_the_prompt_scopes_the_new_authority():
    """A prompt that promised more than the code allows produces a GO
    that tries, is refused, and tells a person something is broken."""
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "AUTONOMOUS errand's plan is yours to settle" in md
    assert "A STORY is not yours either, nor is a hand-started errand" in md
    assert "Three plan cycles per run is the ceiling" in md
    assert "Revoking the grant takes this back immediately" in md
    assert "Done-or-more-work at the end is never yours" in md


def test_the_prompt_explains_the_wake_it_will_receive():
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "[ERRAND CHECK-IN]" in md
    assert "Silence means nothing is wrong" in md, (
        "GO will read a quiet errand as a stalled one and go looking")


def test_the_prompt_still_separates_steering_from_asking():
    """Unchanged by this build and worth keeping that way: `steer` is
    for when it is doing the wrong thing and you know the right one;
    `ask_executor` is for when you need the reasoning."""
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "Never send a steer to ask a question" in md
    assert "`ask_executor` changes nothing by asking" in md
