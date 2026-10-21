"""Ec1a/Ec1b — the errand profile can compute AND persist.

THE STRUCTURAL GAP, which was not about credentials. `errand-ops` held
`bash` and no `write`; `story-business` held `write` and no `bash`. No
profile held both, so the errand an errand is FOR — run the report
generator locally, keep the output, do not send the email — could not be
expressed by either.

Ec1a grants `write`, `edit`, `env_setup`, `env_install` and `web_fetch`.
This file checks the grant landed and, more importantly, that everything it
implies is true at the same time: each tool routed, each classified, none
pending, none demanding a human, and the workspace actually writable.

Every check runs on the host. The profile loader, the catalog, the effects
table and the chain all import without a provider SDK.
"""
from __future__ import annotations

import os

import pytest

from kernel.harness.chain import InterceptorContext, ToolCall, run_chain
from kernel.harness.effects import classify, requires_approval
from kernel.profiles.loader import load_all
from kernel.tools.catalog import CATALOG, PENDING, SIDES, tools_by_side

GRANTED = ("write", "edit", "env_setup", "env_install", "web_fetch")


@pytest.fixture(scope="module")
def errand():
    return load_all()[("errand", "ops")]


def _ctx(scope, tools, **kw):
    base = dict(writable_scope=list(scope), allowed_tools=list(tools),
                budget_remaining=1000, execution_id=1, depth=0)
    base.update(kw)
    return InterceptorContext(**base)


# ── Ec1a: the grant ──────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", GRANTED)
def test_the_errand_holds_the_tool(errand, tool):
    assert tool in errand.tools, (
        f"{tool} is not on the errand surface: {sorted(errand.tools)}")


@pytest.mark.parametrize("tool", GRANTED)
def test_the_granted_tool_is_routed_classified_and_built(errand, tool):
    """Three things a grant needs to be worth anything, and each has been
    the missing one at some point: `web_fetch` was routed with no function,
    `goal_knowledge` was routed with no class, `web_search` is declared
    with neither."""
    assert tool in CATALOG, "not declared"
    assert tool not in PENDING, "declared but not built"
    assert any(tool in tools_by_side(s) for s in SIDES), "declared but not routed"
    assert CATALOG[tool]["_effect"], "declared but not classified"


def test_nothing_on_the_errand_surface_needs_a_person(errand):
    """A tool that demands approval for an ordinary call is a tool the
    errand cannot use — `approved_effects` is never populated, so the
    demand can be made and never satisfied. That is the 12-leg deadlock."""
    blocked = sorted(t for t in errand.tools
                     if requires_approval(classify(t, {})))
    assert not blocked, f"{blocked} would park every errand that calls them"


def test_the_surface_is_whole(errand):
    """No pending, no unrouted, no unclassified — checked over the WHOLE
    surface rather than the five added, because a grant that widens the
    profile is exactly when the other twelve stop being examined."""
    assert not set(errand.tools) & PENDING
    routed = set().union(*(tools_by_side(s) for s in SIDES))
    assert not set(errand.tools) - routed
    assert all(CATALOG[t]["_effect"] for t in errand.tools)


def test_egress_was_granted_deliberately_and_only_here(errand):
    """`web_fetch` is the widest thing added. It must be a profile choice,
    never something every executor inherits — an errand that reaches the
    network by default is a different security posture than the one the
    base surface describes."""
    base = load_all()[("errand", "ops")].base_tools
    assert "web_fetch" not in base, "egress leaked into the base surface"
    assert "web_fetch" in errand.added_tools


def test_the_errand_now_plans_and_claims_and_the_flags_agree(errand):
    """Ec2 REVERSES Eb3, deliberately.

    Eb3 removed `propose_completion` because an errand had no way to define
    done — a claim nothing could measure is worse than no claim. Ec2d gave
    it one that suits it (`artifact[role=report]`), so the tool is back and
    the flag says so.

    The flag and the surface must agree in both directions, which
    `test_profiles.py` asserts for every profile: a profile that says it
    can claim completion and does not hold the tool would loop until its
    legs ran out, and one holding the tool with the flag off gets a typed
    refusal for the thing it was told to do.
    """
    assert errand.allows_completion is True
    assert "propose_completion" in errand.tools
    assert errand.requires_plan is True
    assert "register_plan" in errand.tools
    # `record.py` refuses the impossible combination outright: a profile
    # that must plan and can never claim completion could never finish.
    assert not (errand.requires_plan and not errand.allows_completion)


def test_the_errand_still_has_no_verifier(errand):
    """Deliberate rather than missing. A Verifier rebuilds a committed tree
    in a fresh container to judge code that will merge; an errand has no
    branch and its tree is discarded. Its deliverable is an artifact
    already in the store under its own sha256, and the check is whether
    that row exists — which the executor could not fake without emitting
    the thing."""
    assert errand.has_verifier is False


def test_the_prompt_teaches_planning_before_acting(errand):
    """A model that does not know the surface is gated will spend its
    first leg collecting `E_PLAN_NOT_APPROVED` refusals."""
    prompt = errand.prompt_preamble.lower()
    assert "register_plan" in prompt
    assert "propose_completion" in prompt
    assert "expect" in prompt and "artifact" in prompt
    for label in ("goal", "guard"):
        assert label in prompt


def test_the_road_out_is_emit_not_publish(errand):
    """Ec2c closed the gap Ec1a left open, and the two tools are not
    interchangeable.

    `publish_artifact` requires the story to already hold the target
    resource with a write relation. An errand usually holds nothing, so
    granting it would have handed the model a tool that answers
    `E_NOT_GRANTED` every time — worse than one that is absent, which is
    the whole reason `PENDING` exists.

    `emit_artifact` writes to the execution's own manifest and needs no
    grant, because a row saying "this run produced this" is a claim about
    the run rather than a change to something the goal owns.
    """
    assert "emit_artifact" in errand.tools
    assert "publish_artifact" not in errand.tools


def test_the_prompt_teaches_the_road_out(errand):
    """A model given `write` and no instruction produces a perfect file and
    reports "done, see report.md" — the tree is then discarded and the run
    has produced nothing. The prompt has to say both halves: the workspace
    is thrown away, and emitting is what survives it."""
    prompt = errand.prompt_preamble.lower()
    assert "discardable" in prompt or "thrown away" in prompt
    assert "emit_artifact" in prompt
    # Every role, so a model does not invent a sixth and get a refusal.
    for role in ("report", "dataset", "method", "log", "figure"):
        assert role in prompt, f"the prompt never mentions role {role!r}"
    assert "summary" in prompt, "the required field is not taught"


def test_the_prompt_tells_it_to_skip_the_side_effect(errand):
    """The scenario this profile exists for: run the code that computes,
    not the code that announces. A model that emails forty people because
    the script always does is the failure that cannot be undone."""
    prompt = errand.prompt_preamble.lower()
    assert "email" in prompt and "dry-run" in prompt


# ── Ec1b: the workspace ──────────────────────────────────────────────────

def test_an_errand_with_no_grants_can_write_to_its_own_root():
    """Ec1b's test, at the layer that decides it.

    THE PLAN SAID `NoneDriver` CREATES NOTHING, AND THAT IS NOT WHY WRITES
    WOULD FAIL. `prepare_workspace` calls `os.makedirs(root)` before any
    driver runs — it has to, because Docker refuses to start a container
    whose volume subpath is missing — and `writable_scope_for` returns
    `[root]` when there are no mounts. So the tree exists and stage 1
    permits writes in it; the only thing missing was `write` on the
    surface, which is Ec1a.

    No separate scratch driver is needed. What `NoneDriver` genuinely does
    not do is CHECKPOINT, and under "an errand is discardable, only what it
    reports survives" that is correct rather than missing.
    """
    from kernel.workspace.mounts import writable_scope_for

    root = "/workspace/exec-42"
    scope = writable_scope_for(root, ())
    assert scope == [root], "an errand with no mounts must own its root"

    errand_tools = load_all()[("errand", "ops")].tools
    for target in (f"{root}/report.md", f"{root}/sub/dir/data.csv"):
        call = ToolCall(tool="write", args={"path": target}, target=target)
        decision = run_chain(call, _ctx(scope, errand_tools))
        assert decision.proceed, f"{target}: {decision}"


def test_the_scope_still_stops_a_write_outside_it():
    """The opposite failure. Granting `write` must not have widened the
    envelope — stage 1 confines it, and that is the containment the
    `contained` classification is claiming."""
    from kernel.workspace.mounts import writable_scope_for

    root = "/workspace/exec-42"
    scope = writable_scope_for(root, ())
    errand_tools = load_all()[("errand", "ops")].tools
    for target in ("/etc/passwd", "/workspace/exec-43/steal.md",
                   f"{root}/../exec-43/steal.md"):
        call = ToolCall(tool="write", args={"path": target}, target=target)
        assert not run_chain(call, _ctx(scope, errand_tools)).proceed, target


def test_an_errand_profile_declares_a_real_workspace_driver():
    """REVERSED BY D1. This asserted `none`, on the reasoning that an
    errand composes no mounts so there is nothing to acquire.

    That reasoning held only while an errand was a special case with a
    scratch tree. It is now allocated what a story is allocated, and
    `none` was actively harmful: it never sets `workspace_path`, and the
    workspace panel resolves its tree FROM `workspace_path` — so the
    panel was empty for the entire life of every errand ever run. An
    errand that DID get a mount could not start at all, because `none`
    clones nothing while the completeness check demands a checkout.

    The root is still made by `prepare_workspace` regardless, which is
    what the second half of this test guards."""
    assert load_all()[("errand", "ops")].workspace_driver != "none"

    src = open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
        "kernel", "worker", "runner.py"), encoding="utf-8").read()
    assert "os.makedirs(root, exist_ok=True)" in src, (
        "the root is no longer created before the driver runs, so an "
        "errand has nowhere to write and the sandbox will not start")
