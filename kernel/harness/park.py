"""E6h / E6i — parking a run on a human, and resuming when they answer.

Two tools park and they share everything but the question they ask:
`register_plan` waits for approval, `ask_human` waits for an answer. One
mechanism, because two would mean two resume paths and two ways to leave a
run parked forever.

WHAT PARKING IS. Not an error and not a leg ending on budget: a run that
cannot proceed until a person acts. It costs nothing while it waits — the
leg ends, the journal holds what happened, and the next leg starts with the
answer in front of it.

WHY THE LEG MUST END. A tool that blocked waiting for a human would hold
the lease, burn the wall clock, and eventually die on the lease predicate
with the question unanswered. Parking is the leg ending on a predicate like
any other, with a row saying what it is waiting for.

THE ANSWER ARRIVES AS A MESSAGE, NOT AS STATE. The next leg reads it in its
prompt, ahead of the task, so the model sees the reply to its own question
rather than inferring it from a changed workspace.

──────────────────────────────────────────────────────────────────────

THIS FILE IS NOW A FACADE, and that is deliberate. It measured 519 code
lines against the 200 ceiling (plan 11.5c), so the implementation moved
into six `park_*` modules along the seams the mechanism already had:

    park_kinds      the kinds of waiting; `ApprovalRefused`, `ParkRequested`
    park_open       opening a park, and retiring one nobody will answer
    park_read       what this run is waiting on, and what it was last told
    park_messages   rendering a human's reply so the model reads it as one
    park_resolve    recording the reply, the H8a gate, and the un-park
    park_approval   what an approval binds to, and whether one still stands

WHY A FACADE RATHER THAN NINETEEN EDITED IMPORT LINES. `kernel.harness.park`
is imported by twenty-three call sites across the executor, the worker, the
console, the verifier and the CI suite — including `harness/executor.py`,
which another change owns and is splitting at the same time. Re-exporting
keeps every one of those imports true, keeps `inspect.getsource` (which two
tests use to read the SQL that RUNS) pointing at the real definitions, and
means the split can be judged on the seams instead of on a diff spread over
two dozen unrelated files.

The private names are re-exported too. `_KIND`, `_OUT` and `_notify_parked`
are read by tests that assert on the stored vocabulary and on the errand
wake; private to callers is not the same as absent, and dropping them here
would break those tests for a cosmetic reason.

NEW CODE SHOULD IMPORT THE MODULE IT MEANS. The facade exists to keep the
existing world working, not to be the front door forever; a new caller that
only wants `plan_is_approved` should say `park_approval`.
"""

from __future__ import annotations

from kernel.harness.park_approval import (
    pin_plan_to_grant,
    plan_hash,
    plan_is_approved,
    standing_approval,
)
from kernel.harness.park_kinds import _KIND, _OUT, ApprovalRefused, ParkRequested
from kernel.harness.park_messages import (
    _direction_message,
    format_answer_for_prompt,
    format_plan_feedback,
)
from kernel.harness.park_open import (
    _notify_parked,
    apply_hold,
    close_open_questions,
    open_request,
)
from kernel.harness.park_read import answered_since, pending_request
from kernel.harness.park_resolve import resolve_request

__all__ = [
    "ApprovalRefused", "ParkRequested",
    "answered_since", "apply_hold", "close_open_questions",
    "format_answer_for_prompt", "format_plan_feedback", "open_request",
    "pending_request", "pin_plan_to_grant", "plan_hash", "plan_is_approved",
    "resolve_request", "standing_approval",
]
