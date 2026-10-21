"""Ec3b/Ec3c — asking a person to approve ONE call, and remembering the yes.

THE DEADLOCK THIS ENDS. Chain stage 3 refuses an `irreversible` call with
"use ask_human with what you intend to do". It reads the approval from
`InterceptorContext.approved_effects` — a field that NOTHING HAS EVER
WRITTEN. So a person could be asked, could say yes, and the next leg built
the same context with the same empty tuple and refused the same call
identically. Twelve legs, then FAILED. An approval that can be demanded and
never granted is not a gate, it is a wall.

Ec3a made that wall much taller: `terraform apply`, `aws s3 rm` and
`gcloud … delete` are now irreversible, and without this they would simply
be impossible rather than approvable. The two belong in one change.

APPROVAL IS PER CALL, NOT PER CLASS, and that is a deliberate narrowing of
what the plan proposed. `approved_effects` would have meant a person
approving one `terraform apply` also approves every `aws s3 rm` for the
rest of the run — the card says one thing and grants another. The card
shows a command; the approval covers that command. Standing grants ("allow
this whole class, always") are a separate decision with a separate design,
and the ceiling for them is already right: `requires_approval` refuses to
loosen `IRREVERSIBLE` as an early return.

THE SIGNATURE IS LR5'S. `stall.signature` already normalises a call to
`tool:operand` — the command for `bash`, the path for `write` — and two
adapters phrasing one call differently produce the same hash. Defining a
second notion of "the same call" here would be a second thing to keep in
step, and the first divergence would be an approval that silently does not
apply to the call it was granted for.

NO ATTEMPTS FLOOR. `ask_human` demands three distinct prior attempts,
because a model asking on its first difficulty is the failure that costs a
person their attention. This is the opposite case: the executor is not
stuck, it is holding a loaded gun and asking whether to fire. There is
nothing to have "tried", and demanding evidence would teach it to do
something destructive three times first.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

KIND = "permission"

# Every question kind maps onto the designed vocabulary in `park._KIND`.
# A permission ask is an authorization: it is somebody granting leave to
# act, exactly like a plan approval, and differs only in scope.
VOCABULARY = "authorization"


def signature(tool: str, args: dict | None = None) -> str:
    """Which call this approval is for. LR5's normalisation, reused."""
    from kernel.harness.stall import signature as _sig

    return _sig(tool, args or {})


async def approved_calls(conn, execution_id: int) -> frozenset:
    """Signatures a person has approved for this run.

    Read fresh per leg, the same way `plan_is_approved` is, and for the
    same reason: the approval arrives BETWEEN legs, which is the entire
    point of parking.

    Only an ANSWERED question with an approving verdict counts. A declined
    one is a decision too, and it is not permission.
    """
    rows = await conn.fetch(
        """
        SELECT payload::text AS payload
          FROM questions
         WHERE execution_id = $1 AND kind = $2
           AND status = 'ANSWERED'
           AND lower(coalesce(answer_text, '')) NOT LIKE 'decline%'
        """, execution_id, KIND)
    out = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        got = payload.get("signature")
        if got:
            out.add(str(got))
    return frozenset(out)


def describe(tool: str, args: dict, effect: str, target: str,
             why: str = "") -> str:
    """What the card says. The CONCRETE CALL, not the class.

    "an irreversible tool call needs approval" is unanswerable — a person
    cannot tell a `terraform plan` typo from a production deletion. The
    command goes in, and so does what makes it irreversible.
    """
    lines = [f"Approve this {effect} call?", ""]
    command = str(args.get("command") or "").strip()
    if command:
        lines.append(f"    {command[:600]}")
    else:
        shown = {k: v for k, v in (args or {}).items() if k != "content"}
        lines.append(f"    {tool}({json.dumps(shown, default=str)[:600]})")
    lines.append("")
    if why:
        lines.append(f"It {why}.")
    if target:
        lines.append(f"Target: {target}")
    lines.append("")
    lines.append("Approving permits THIS call, once. Declining refuses it "
                 "and the run continues with the refusal.")
    return "\n".join(lines)


async def ask(conn, execution_id: int, generation: int, tool: str,
              args: dict, effect: str, target: str = "", why: str = ""):
    """Open a `permission` question and park. Returns the request id.

    Returns an `Invalid` instead when the ask is not well-formed, which
    the caller hands back to the model as an ordinary refusal — a
    malformed ask must not park the run, because parking is what costs a
    person their attention.
    """
    from kernel.harness.park import open_request

    sig = signature(tool, args)
    return await open_request(
        conn, execution_id, generation, KIND,
        {"question": describe(tool, args, effect, target, why),
         # WHAT THE VERDICT ATTACHES TO. Read back by `approved_calls`,
         # so the shape of this payload is load-bearing rather than
         # decorative.
         "signature": sig,
         "tool": tool,
         "args": {k: v for k, v in (args or {}).items() if k != "content"},
         "effect_class": effect,
         "target": target,
         "why": why},
        # ONE CARD PER DISTINCT CALL. `open_request` dedupes on the hash,
        # and without one it dedupes on the text — so a second, different
        # destructive call whose card happened to read similarly would be
        # refused as a repeat and the run would park on nothing.
        content_hash=f"permission-{execution_id}-{sig}")
