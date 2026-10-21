"""I5a · you must be in the goal to see or act on its questions.

THE HOLE. `pid` was in the path of all three kernel-park endpoints and
used by none of them for authorisation. `decide` looked a question up by
id alone; `parked` listed a goal's open questions AND their plan text;
`retry_run` cleared a run's counters. **Any authenticated user could read
or act on any goal** — including approving a plan that lets an executor
run `terraform apply` against an account they have never been given.

Found while designing autonomous errands, whose stated rule is "any human
IN THE GOAL may approve". That is *narrower* than what shipped, so the
check had to be built before the approval it guards became a standing one
— otherwise Phase 5 would have been autonomy on top of an open door.

AND THE FIFTH DECLARATION. While fixing it: which question kinds need a
person was written out in five places, three of them omitting
`escalation`, `direction` and — since Ec3b — `permission`. So a
destructive-command park read as needing nobody in the assistant's inbox,
in the notifier and in the parked listing, while the card builder knew
better. One frozenset now, beside the question kinds it describes.
"""
from __future__ import annotations

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _router() -> str:
    return io.open(ROOT / "app/routers/kernel_park.py", encoding="utf-8").read()


# ── the guard exists, on every endpoint ──────────────────────────────────

def test_all_three_endpoints_require_membership():
    """One shared helper rather than three copies — three copies is how
    they came to share one hole in the first place."""
    src = _router()
    assert "async def _require_member(" in src

    # THE PER-GOAL ENDPOINTS, which are the ones `pid` authorises. The
    # other two handlers in this file are the system-wide executions
    # board — deliberately every goal, and a separate question from this
    # one. Narrowing that is a policy decision, not a bug fix, and it is
    # recorded in docs/refactor/errand_integration.md rather than done
    # quietly here.
    per_goal = [line for line in src.splitlines()
                if line.startswith("async def ")
                and "pid: str" in line
                and "_require_member" not in line]
    assert len(per_goal) == 3, per_goal
    # One call per endpoint, plus the definition.
    assert src.count("_require_member(") == 4, (
        "an endpoint taking a goal does not check membership in it")


def test_the_decide_endpoint_also_checks_the_question_belongs_to_the_goal():
    """Membership alone would let a member of goal A approve goal B's
    question by passing their own goal in the path. Ownership alone would
    let a stranger approve. Both, or neither works."""
    src = _router()
    body = src[src.index("async def decide_question"):]
    body = body[:body.index("@router.post", 10)]
    # THE HELPER, not an inline copy — three copies is how these three
    # endpoints came to share one hole.
    assert "_require_member(" in body, "no membership check"
    assert "JOIN kernel.questions" in body, "no ownership check"
    assert "owner != pid" in body


def test_the_refusal_is_404_and_not_403():
    """Confirming a goal exists and has parked questions is itself a
    disclosure to somebody who cannot see it — the same reasoning the
    chat IDOR fix applied to threads."""
    src = _router()
    guard = src[src.index("async def _require_member"):]
    guard = guard[:guard.index("@router.get")]
    assert "status_code=404" in guard
    assert "status_code=403" not in guard


def test_the_goal_in_the_path_is_actually_used():
    """It was decoration. A test that only checked the helper exists
    would pass on a router that never called it with `pid`."""
    src = _router()
    assert "_require_member(str(user.get(\"uid\") or \"\"), pid)" in src


# ── one declaration of who must answer ───────────────────────────────────

def test_there_is_exactly_one_source_for_which_kinds_need_a_person():
    """It was five, and three were wrong.

    Two joined when a coding story stopped finishing at acceptance:

      `closeout`  — the work is on trunk, the workspace is still up, and
                    a person decides whether anything more belongs in
                    THIS story. The same decision `acceptance` protects,
                    asked about a different fact.
      `pr_closed` — somebody closed the PR without merging it. That is
                    an act, not a fault, and only they can say what they
                    meant by it. Guessing costs either a story they
                    meant to keep or a pod held for one they abandoned.
      `landing`   — the merge card. Unusual: the merge resolves it with
                    nobody touching the card, so what makes it a
                    person's is the OTHER answer, "actually, more work".
    """
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    assert HUMAN_ANSWERED_KINDS == frozenset({
        "authorization", "acceptance", "escalation", "direction",
        "permission", "closeout", "pr_closed", "landing"})


def test_no_module_writes_the_set_out_by_hand():
    """The specific pair that was copied around. Catching the literal is
    cruder than catching the concept, but it is the exact shape the bug
    took three times."""
    import re

    offenders = []
    # THE SOURCE TREES, not the whole checkout. `ROOT.glob("**/*.py")`
    # walks -- and this test then READS -- every Python file under
    # local-development/vendor, which carries a complete second Python
    # installation, plus .venv, graphify-out and infra/e0/artifacts. On a
    # warm local disk that is merely slow; over a container bind mount it
    # took longer than the 300s CI deadline and killed the whole session
    # before pytest could write its report. test_gateway_surface.py had
    # already learned this and says so in its own comment: "a repo-wide
    # glob walks local-development/vendor and takes two minutes, and a
    # test that slow stops being run."
    sources = [f for d in ("app", "kernel", "infra", "scripts")
               for f in (ROOT / d).glob("**/*.py")]
    for path in sources:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if ("__pycache__" in rel or "/tests/" in rel or ".venv" in rel
                or rel.startswith("infra/e0/ci/")
                # The module that DEFINES the set necessarily writes the
                # names out; excluding it is the difference between "one
                # source" and "no source".
                or rel == "kernel/mailbox/schema.py"):
            continue
        text = io.open(path, encoding="utf-8", errors="replace").read()
        if re.search(r'\(\s*"authorization"\s*,\s*"acceptance"\s*\)', text):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} write out which kinds need a person instead of "
        f"importing HUMAN_ANSWERED_KINDS")


@pytest.mark.parametrize("module,attr", [
    ("app.services.kernel_park_cards", "HUMAN_ONLY_KINDS"),
])
def test_every_reader_agrees_with_the_source(module, attr):
    import importlib

    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    got = getattr(importlib.import_module(module), attr)
    assert set(got) == set(HUMAN_ANSWERED_KINDS)


def test_a_permission_park_needs_a_person_everywhere_it_is_shown():
    """The concrete regression. Three surfaces said it needed nobody."""
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS
    from app.services.kernel_park_cards import build_card

    assert "permission" in HUMAN_ANSWERED_KINDS
    card = build_card({"id": 1, "execution_id": 1, "goal_id": "g",
                       "story_id": "s", "kind": "permission",
                       "question_text": "terraform apply"}, None, "")
    assert card["needs_human"] is True

    # THE SHARED SOURCE IS NOW A FUNCTION, not the set. Since
    # 2026-09-24 the answer depends on the RUN as well as the kind -- an
    # autonomous errand's plan is the GO's -- so every surface calls
    # `needs_a_person(kind, go_driven)` instead of testing membership
    # itself. What this still guards is that none of them re-grew a
    # local copy, which is the regression it was written for.
    for rel in ("kernel/assistant/read.py", "kernel/assistant/notifier.py",
                "app/routers/kernel_park.py"):
        src = io.open(ROOT / rel, encoding="utf-8").read()
        assert "needs_a_person(" in src, f"{rel} does not use the one source"
        assert "in HUMAN_ANSWERED_KINDS" not in src, (
            f"{rel} tests the set itself and so cannot know about an "
            f"autonomous errand")


def test_an_autonomous_errands_plan_is_not_reported_as_needing_a_person():
    """WHAT THIS COST, live on 2026-09-24. The GO gained the authority
    to settle an autonomous errand's plan, and this label did not move
    -- so `inbox` still said `needs_human: true`, the GO read its own
    inbox, believed it, and told the person "the system has marked this
    one as needing a person, so I haven't decided it".

    It was RIGHT to. The data said so. An agent behaving correctly on a
    platform that lied to it looks exactly like an agent being timid,
    which is the worst kind of bug to debug."""
    from kernel.mailbox.schema import needs_a_person

    assert needs_a_person("authorization", go_driven=True) is False
    assert needs_a_person("authorization", go_driven=False) is True
    # Everything else stays a person's on every run.
    for kind in ("acceptance", "escalation", "direction", "permission"):
        assert needs_a_person(kind, go_driven=True) is True, kind
    # And the default is the safe one: a caller that cannot cheaply
    # establish `go_driven` gets the pre-2026-09-24 answer.
    assert needs_a_person("authorization") is True


def test_a_clarification_is_still_answerable_by_an_assistant():
    """Deliberately absent from the set. Adding it would park every
    `ask_human` on a person and make asking expensive — which is the
    thing the evidence floor exists to keep cheap."""
    from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

    assert "clarification" not in HUMAN_ANSWERED_KINDS
    assert "decision" not in HUMAN_ANSWERED_KINDS
