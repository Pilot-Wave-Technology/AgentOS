"""Model loop — E5 increment of Stage E.

The first model-driven execution: a loop that calls the model seam,
evaluates predicates after every step, and ends legs on the harness's terms.

1. Calls snapshot_preflight at startup — refuses to start if it fails.
2. After each step: evaluates all three predicates (token budget, step budget,
   lease clock).  If any fires, the leg ends.
3. propose_completion tool call: idempotent, the run does not end.

ES2 built kernel/harness/ as pure functions with no loop.  This is the loop.

──────────────────────────────────────────────────────────────────────

THIS FILE IS NOW A FACADE, and that is deliberate. It measured 833 code
lines against the 200 ceiling (plan 11.5c/11.5f D2a), so the loop moved
into fourteen `loop_*` modules along the seams a leg already has:

    loop_leg          `run_leg` itself: the phases, the step loop, and
                      every way out of it
    loop_terms        what the caller, the profile and the model each
                      decide about the surface and the size
    loop_rehydrate    the prior leg, from the checkpoint or the journal
    loop_preamble     what the model is shown before its first request,
                      in the order it is shown
    loop_progress     the only filesystem a leg touches
    loop_intake       the assistant's mailbox at the step boundary
    loop_turn         the one place that talks to a provider
    loop_step         the durable record of one step, and its scrubbing
    loop_claim        propose_completion, gated by the run's own checks
    loop_tool         an ordinary tool call and the evidence it leaves
    loop_endings      everything that ends a leg, in the order that
                      decides WHY
    loop_boundary     what this leg leaves for the next one
    loop_result       the two shapes `run_leg` returns, which D2
                      turns into a wire format
    loop_accounting   spend and peak context, read back from the journal

WHY A FACADE RATHER THAN THIRTY EDITED IMPORT LINES. `kernel.harness.loop`
is imported by the worker pass, the console, `leg_runner`, `subagent_run`
and some twenty CI gates, every one of them for `run_leg`. Re-exporting
keeps all of those true, keeps `inspect.getsource` pointing at the real
definitions, and means this split can be judged on the seams rather than
on a diff spread across thirty unrelated files. D2 moves the leg runner
into the worker container next, and it should be reviewing the leg, not
this.

THE PRIVATE NAMES ARE RE-EXPORTED TOO. `_execution_spend`,
`_sum_step_spend`, `_scrub_json` and `_scrub_text` are imported by
`infra/e0/ci/test_e9b_guard_regressions.py`,
`infra/e0/ci/test_unit_usage.py` and
`app/services/tests/test_credential_leak_guards.py`; private to callers
is not the same as absent. `_as_args` and `_default_tool_executor` go
with them for the same reason a reader would expect them here.

NEW CODE SHOULD IMPORT THE MODULE IT MEANS. The facade exists to keep the
existing world working, not to be the front door for ever; a new caller
that only wants the spend should say `loop_accounting`.

THE SOURCE-READING GUARDS MOVED WITH THE CODE, not with the name. A test
or a mutation that greps THIS file now greps an import list and passes on
no evidence at all — which is the failure mode plan 11.5f records from
the last wave, where a SECURITY mutation pinned to a path its subject had
left matched nothing and proved nothing. The three that pointed here were
repointed in the same commit:
`app/services/tests/test_credential_leak_guards.py` at `loop_step.py`,
and `infra/e0/ci/redbuild_mutations.py`'s G9 recovery mutation at
`loop_step.py` too — its `find` pattern was the step-journal `seq` query,
which is now the only one in that file.
"""

from __future__ import annotations

from kernel.harness.loop_accounting import (
    _execution_peak_context,
    _execution_spend,
    _sum_step_spend,
)
from kernel.harness.loop_boundary import (
    BOUNDARY_MAX_ATTEMPTS,
    BOUNDARY_RETRY_DELAY_S,
    BOUNDARY_TIMEOUT_S,
)
from kernel.harness.loop_leg import run_leg
from kernel.harness.loop_step import _as_args, _scrub_json, _scrub_text
from kernel.harness.loop_tool import _default_tool_executor
from kernel.harness.loop_turn import EMPTY_RESPONSE_RETRIES

__all__ = [
    "BOUNDARY_MAX_ATTEMPTS", "BOUNDARY_RETRY_DELAY_S", "BOUNDARY_TIMEOUT_S",
    "EMPTY_RESPONSE_RETRIES", "run_leg",
]
