"""Interceptor chain — Python implementation (ported from TS in cycle 1b).

Six stages numbered 1–6 wrapping the execute step. The chain is a pipeline:
each stage receives a ToolCall + InterceptorContext and returns proceed/deny.
A denial short-circuits — later stages do not fire.

Denials carry the corrective form: not "denied" but "denied because X; the
allowed scope is Y."

Stage 1 (envelope) uses path-prefix containment via is_path_inside — scope
entries are path prefixes, not globs. This aligns with P0's decision document
and is what E7b's attenuation is specified against.

Stage 2 (policy): allowed_tools=None means open surface (no policy, all tools
allowed). allowed_tools=[] means deny-all (fail closed), matching writable_scope's
default. This gives attenuation one consistent meaning for both fields.

See docs/refactor/decisions/p0-execution-envelope.md for the port rationale.
"""

from __future__ import annotations

import json

from kernel.harness.effects import classify, requires_approval
from dataclasses import dataclass, field
from typing import Optional, Callable

from kernel.envelope import is_path_inside


# ── Types (port of types.ts) ────────────────────────────────────────────────

@dataclass
class ToolCall:
    """A tool call entering the interceptor chain."""
    tool: str
    args: dict
    target: Optional[str] = None  # target path for write operations


@dataclass
class InterceptorContext:
    """Context carried through the chain — set by the harness, read by stages."""
    writable_scope: list[str]     # glob patterns; stage 1 checks containment
    allowed_tools: Optional[list[str]] = None  # None = open surface; [] = deny all
    budget_remaining: int = 0     # stage 4 checks sufficiency (named gap: always 0)
    execution_id: int = 0         # audit logging
    depth: int = 0                # 0 = parent, 1 = sub-agent (E7b)

    # H2. `effect_policy` may loosen `contained` and `reversible` and can
    # NEVER loosen `irreversible` — see effects.requires_approval, where
    # that is an early return rather than a lookup with a default.
    effect_policy: Optional[dict] = None
    # What a person has already said yes to for this execution. Empty by
    # default: approval is a thing that happened, not a thing assumed.
    #
    # Ec3c — WAS `approved_effects`, A FIELD NOTHING EVER WROTE. Stage 3
    # read it, found the empty tuple every time, and refused; the
    # corrective said to ask a human, the human said yes, and the next leg
    # built the same context and refused identically. Twelve legs, then
    # FAILED. An approval that can be demanded and never granted is not a
    # gate, it is a wall.
    #
    # It also holds CALL signatures rather than effect-class names, which
    # narrows what the plan proposed. A class-wide grant would mean
    # approving one `terraform apply` also approves every `aws s3 rm` for
    # the rest of the run — the card shows one thing and grants another.
    # Standing grants are a separate decision with a separate design.
    approved_calls: frozenset = frozenset()


@dataclass
class Denial:
    """A denial carries the corrective form."""
    code: str
    message: str
    corrective: str
    stage: int


@dataclass
class StageLogEntry:
    """Entry in the chain's stage log."""
    stage: int
    name: str
    proceed: bool
    denial: Optional[Denial] = None


@dataclass
class ChainResult:
    """Final result of running the full chain."""
    stages: list[StageLogEntry]
    proceed: bool
    call: ToolCall
    denial: Optional[Denial] = None


# Stage result: either proceed with (possibly modified) call, or deny
@dataclass
class _Proceed:
    proceed: bool = True
    call: ToolCall = None  # type: ignore[assignment]


@dataclass
class _Deny:
    proceed: bool = False
    denial: Denial = None  # type: ignore[assignment]


_StageResult = _Proceed | _Deny


STAGE_NAMES: dict[int, str] = {
    1: "envelope",
    2: "policy",
    3: "hitl",
    4: "budget",
    5: "execute",
    6: "audit",
}


# ── Stage 1: Envelope containment ───────────────────────────────────────────

def _stage_envelope(call: ToolCall, ctx: InterceptorContext) -> _StageResult:
    """writable_scope check — is the target path inside the allowed scope?

    Uses path-prefix containment (is_path_inside), not glob matching.
    Scope entries are path prefixes: ["/ws/src"] means "anything under
    /ws/src is writable." No wildcard syntax.
    """
    if not call.target:
        return _Proceed(call=call)

    if len(ctx.writable_scope) == 0:
        return _Deny(denial=Denial(
            code="E_ENVELOPE_VIOLATION",
            message=f'Write target "{call.target}" denied: writable_scope is empty.',
            corrective="No paths are writable. writable_scope: []",
            stage=1,
        ))

    # Path-prefix containment: target is allowed if it is inside any scope entry.
    allowed = any(is_path_inside(scope, call.target) for scope in ctx.writable_scope)

    if not allowed:
        return _Deny(denial=Denial(
            code="E_ENVELOPE_VIOLATION",
            message=f'Write target "{call.target}" is outside the allowed scope.',
            corrective=f"Allowed writable_scope: {json.dumps(ctx.writable_scope)}",
            stage=1,
        ))
    return _Proceed(call=call)


# ── Stage 2: Policy check ───────────────────────────────────────────────────

def _stage_policy(call: ToolCall, ctx: InterceptorContext) -> _StageResult:
    """Tool allow/deny from contract — is this tool on the surface?

    None = no policy (open surface, all tools allowed).
    [] = deny all (fail closed, matching writable_scope's default).
    """
    if ctx.allowed_tools is None:
        # No policy declared — open surface
        return _Proceed(call=call)

    if len(ctx.allowed_tools) == 0:
        # Empty allowlist — deny all (fail closed)
        return _Deny(denial=Denial(
            code="E_TOOL_DENIED",
            message=f'Tool "{call.tool}" denied: allowed_tools is empty (deny-all).',
            corrective="No tools are allowed. allowed_tools: []",
            stage=2,
        ))

    if call.tool not in ctx.allowed_tools:
        return _Deny(denial=Denial(
            code="E_TOOL_DENIED",
            message=f'Tool "{call.tool}" is not on the allowed surface.',
            corrective=f"Allowed tools: {json.dumps(ctx.allowed_tools)}",
            stage=2,
        ))
    return _Proceed(call=call)


# ── Stage 3: HITL gate ──────────────────────────────────────────────────────

def _stage_hitl(call: ToolCall, ctx: InterceptorContext) -> _StageResult:
    """H2 — may this call act on the world without asking a person?

    THE POLICY DECIDES FOR TWO CLASSES AND NOT FOR THE THIRD.
    `irreversible` always needs approval, and no profile, environment
    variable or contract can say otherwise — a setting that can downgrade
    it is one somebody changes during an incident and never changes back,
    and the failure is unrecoverable by construction.

    A CHILD CANNOT BE APPROVED HERE. An `effect`-class spawn parks the
    PARENT, because the child has no leg to park on (SA1) — and inside a
    child, a call whose effect class was not approved at the spawn is
    REFUSED, never escalated. Escalation implies somebody to escalate to,
    and at depth 1 there is nobody: the parent is mid-step waiting on
    this very call.
    """
    effect = classify(call.tool, call.args)
    if not requires_approval(effect, ctx.effect_policy):
        return _Proceed(call=call)

    if ctx.depth > 0:
        return _Deny(denial=Denial(
            code="E_EFFECT_UNAPPROVED",
            message=f'"{call.tool}" is {effect} and was not approved when '
                    f"this sub-agent was dispatched.",
            corrective="A child cannot ask — it has no leg to park on. "
                       "Report what you found and let the parent do this, "
                       "or ask for it at the spawn.",
            stage=3,
        ))

    from kernel.harness.permission import signature

    if signature(call.tool, call.args) in ctx.approved_calls:
        return _Proceed(call=call)

    # THE EXECUTOR TURNS THIS INTO A PARK. The chain is synchronous and
    # holds no connection, so it cannot open a question itself — it says
    # what is needed and `_call_inner` asks. The corrective is what the
    # model sees only when the park could not happen, which is why it
    # still reads as an instruction rather than as a dead end.
    return _Deny(denial=Denial(
        code="E_APPROVAL_REQUIRED",
        message=f'"{call.tool}" is {effect} and needs a person to approve '
                f"it before it runs.",
        corrective="The run parks and a person is shown this exact call. "
                   "If they approve it, the same call will succeed on the "
                   "next leg — so retry it then, unchanged. Changing it "
                   "makes it a different call needing its own approval.",
        stage=3,
    ))


# ── Stage 4: Budget gate ────────────────────────────────────────────────────

def _stage_budget(call: ToolCall, ctx: InterceptorContext) -> _StageResult:
    """Sufficient budget remaining for this call?"""
    if ctx.budget_remaining <= 0:
        return _Deny(denial=Denial(
            code="E_BUDGET_EXHAUSTED",
            message="Insufficient budget remaining for tool call.",
            corrective=f"Budget remaining: {ctx.budget_remaining}",
            stage=4,
        ))
    return _Proceed(call=call)


# ── Stage 5: Execute ────────────────────────────────────────────────────────

def _stage_execute(call: ToolCall, _ctx: InterceptorContext) -> _StageResult:
    """Passthrough — the caller does the actual execution."""
    return _Proceed(call=call)


# ── Stage 6: Post-exec audit ────────────────────────────────────────────────

def _stage_audit(call: ToolCall, _ctx: InterceptorContext) -> _StageResult:
    """Record the call. Real audit writes wired when bridge carries results."""
    return _Proceed(call=call)


# ── Chain runner ─────────────────────────────────────────────────────────────

_PRE_EXEC_STAGES: list[tuple[int, Callable]] = [
    (1, _stage_envelope),
    (2, _stage_policy),
    (3, _stage_hitl),
    (4, _stage_budget),
]

_POST_EXEC_STAGES: list[tuple[int, Callable]] = [
    (5, _stage_execute),
    (6, _stage_audit),
]


def run_chain(call: ToolCall, ctx: InterceptorContext) -> ChainResult:
    """Run a tool call through all six interceptor stages.

    Stages 1–4 are pre-execution gates. A denial at any stage short-circuits:
    later stages do not fire.

    Stage 5 (execute) and stage 6 (audit) are post-execution and always fire
    if the pre-execution stages all proceed.
    """
    stages: list[StageLogEntry] = []
    current_call = call

    # Pre-execution stages (1–4): short-circuit on denial
    for num, fn in _PRE_EXEC_STAGES:
        result = fn(current_call, ctx)
        entry = StageLogEntry(
            stage=num,
            name=STAGE_NAMES[num],
            proceed=result.proceed,
        )

        if not result.proceed:
            entry.denial = result.denial
            stages.append(entry)
            return ChainResult(
                stages=stages,
                proceed=False,
                call=current_call,
                denial=result.denial,
            )

        current_call = result.call
        stages.append(entry)

    # Post-execution stages (5–6): always fire after pre-exec passes
    for num, fn in _POST_EXEC_STAGES:
        result = fn(current_call, ctx)
        stages.append(StageLogEntry(
            stage=num,
            name=STAGE_NAMES[num],
            proceed=result.proceed,
        ))
        if result.proceed:
            current_call = result.call

    return ChainResult(
        stages=stages,
        proceed=True,
        call=current_call,
    )
