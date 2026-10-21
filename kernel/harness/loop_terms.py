"""The terms of a leg: what it may call, how big it is, how it is counted.

THREE PRECEDENCE RULES, AND PRECEDENCE IS WHY THEY SHARE A FILE. None of
these computes anything hard; each decides which of three opinions wins,
and a rule like that is only reviewable next to its siblings.

  THE SURFACE (E6f)   comes from the PROFILE, not from the caller. A
                      caller that chooses the tool list chooses the
                      security boundary, and chain stage 2 then enforces
                      whatever it was handed. With a profile, the
                      envelope and the schemas shown to the model are
                      the same object, so the model is never offered a
                      tool the envelope refuses. `tools=` survives for
                      the null-executor and deterministic tests, which
                      need a two-tool surface with no profile behind it
                      -- and passing BOTH is refused rather than
                      resolved, because one of them would silently lose.

  THE SIZE (LR7)      explicit argument, then the profile's opinion,
                      then the model's own figure. `step_budget=None` in
                      the signature is not laziness: 200 in a signature
                      is a number nobody can change without editing
                      code, which is the shape of the model id that was
                      wrong for the whole of Stage G. An explicit
                      `token_budget` names the CONTEXT budget -- that is
                      what callers have always meant by "how big is a
                      leg", even while the code summed spend.

  THE BASIS           derived from the adapter's declared reasoning
                      capability, never defaulted. An adapter that says
                      `unreported` and will not say whether thinking is
                      inside output RAISES. A default here would silently
                      mis-count every leg that model ever runs, and the
                      error would look like a budget that behaves
                      differently on one provider.

Nothing in this file touches the database, the model or the clock. Keep
it that way -- it is the part of a leg's setup that can be reasoned
about without a fixture.
"""

from __future__ import annotations

from kernel.harness.models.types import normalise_tool_schema
from kernel.tools.catalog import schemas_for


def resolve_surface(profile, tools: list[dict] | None) -> list[dict]:
    """The tool schemas this leg shows the model."""
    # ── E6f: the surface comes from the profile, not from the caller ─────
    # A caller that chooses the tool list chooses the security boundary,
    # and chain stage 2 then enforces whatever it was handed. With a
    # profile, the surface and the schemas shown to the model are the same
    # object — so the model is never offered a tool the envelope refuses.
    #
    # `tools=` remains for the null-executor and deterministic tests, which
    # need a two-tool surface with no profile behind it. Passing both is a
    # programming error: one of them would silently lose.
    if profile is not None:
        if tools is not None:
            raise ValueError(
                "run_leg: pass profile= or tools=, not both — the profile "
                "defines the surface and an explicit list would shadow it"
            )
        tools = schemas_for(profile.tools)
    elif tools is None:
        raise ValueError("run_leg: one of profile= or tools= is required")

    # ── Normalise tool schemas — one key, not a silent difference ────────
    tools = [normalise_tool_schema(t) for t in tools]
    return tools


def resolve_budget(profile, adapter, token_budget, leg_budget, step_budget):
    """`(leg, token_budget, step_budget)` -- the three the loop reads."""
    # ── LR7: token budget from adapter capabilities, not a constant ──────
    # LEG SIZE, resolved once: explicit argument, then the profile's
    # opinion, then the model's LR7 figure. A caller that passes one
    # means it — that is how a test pins a two-leg task.
    from kernel.harness.models.config import LegBudget, choose_leg

    profile_budget = None
    if profile is not None and (profile.leg_tokens or profile.leg_steps
                                or profile.max_legs):
        base = LegBudget()
        profile_budget = LegBudget(
            context_tokens=profile.leg_tokens or base.context_tokens,
            spend_tokens=base.spend_tokens,
            steps=profile.leg_steps or base.steps,
            max_legs=profile.max_legs or base.max_legs)
    leg = leg_budget or choose_leg(
        profile_budget=profile_budget,
        model_tokens=adapter.capabilities.leg_token_budget)
    # An explicit argument still wins, and it names the CONTEXT budget —
    # that is what callers passing `token_budget` have always meant by
    # "how big is a leg", even while the code summed spend.
    if token_budget is not None:
        leg = type(leg)(context_tokens=token_budget,
                        spend_tokens=leg.spend_tokens,
                        steps=leg.steps, max_legs=leg.max_legs)
    token_budget = leg.context_tokens
    if step_budget is None:
        step_budget = leg.steps

    return leg, token_budget, step_budget


def resolve_basis(adapter) -> str:
    """"measured" or "estimated". Raises rather than guessing."""
    # ── Budget basis from adapter's reasoning capability ─────────────────
    reasoning = adapter.capabilities.reasoning
    if reasoning in ("none", "reported"):
        budget_basis = "measured"
    elif reasoning == "unreported":
        # Derived from thinking_in_output — not hardcoded, not defaulted.
        # None → raise (forces declaration). True → measured. False → estimated.
        if adapter.capabilities.thinking_in_output is None:
            raise ValueError(
                f"adapter {adapter.name!r} declares reasoning='unreported' but "
                "does not declare thinking_in_output; it must state whether the "
                "provider includes thinking in output"
            )
        budget_basis = "measured" if adapter.capabilities.thinking_in_output else "estimated"
    else:
        raise ValueError(f"unknown reasoning capability: {reasoning!r}")
    return budget_basis
