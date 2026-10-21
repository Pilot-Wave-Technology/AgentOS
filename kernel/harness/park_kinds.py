"""The kinds of waiting, and the two signals that carry them.

SPLIT OUT OF `park.py` (plan 11.5c: 519 code lines against a 200
ceiling). This is the alphabet the other five `park_*` modules share, and
it is here ALONE so that none of them has to import another to name a
kind. `park_open` writes `_KIND`, `park_read` reads it back through
`_OUT`, `park_resolve` raises `ApprovalRefused`, and the executor loop
catches `ParkRequested` — four consumers, one definition, no cycle.

WHY TWO DICTS AND NOT ONE WITH AN INVERSE. `_KIND` is many-to-one
(`question` -> `clarification`) and `_OUT` maps two stored kinds back to
the same caller word (`clarification` and `decision` -> `question`).
Neither is the other's inverse and neither can be derived from the other;
a future reader must not "simplify" this into a single mapping.
"""

from __future__ import annotations

# The two shapes of waiting, mapped onto the designed vocabulary:
# a question is a clarification, a plan approval is an authorization.
_KIND = {"question": "clarification",
         "plan_approval": "authorization",
         # verification passed; a person decides done-or-more-work
         "acceptance": "acceptance",
         # a person's question was answered and the answer needs a
         # decision before more work happens — see `apply_hold`
         "direction": "direction",
         # Ec3b — one destructive CALL. Kept as its own stored kind
         # rather than folded into `authorization`, because
         # `permission.approved_calls` has to find exactly these and a
         # plan approval must never read as permission for a call.
         "permission": "permission",
         # The one kind NO PERSON answers: the run is waiting for
         # its work to reach trunk, and the merge is the answer.
         # Absent from HUMAN_ANSWERED_KINDS for that reason -- a
         # card asking somebody to confirm what the engine is
         # already watching for would be a second authority over
         # one fact, free to disagree with the first.
         "landing": "landing",
         # The work is ON TRUNK and a person decides whether the
         # STORY is finished. Distinct from `acceptance`, which asks
         # the same words about work that is only pushed -- and from
         # `landing`, which nobody answers. Named `closeout` rather
         # than anything with "land" in it, because a kind one
         # letter from `landing` is a trap in every log line.
         "closeout": "closeout",
         # A person CLOSED the PR without merging it. Deliberate --
         # GitHub distinguishes closed from merged -- so the question
         # is what they meant by it, not whether something broke.
         "pr_closed": "pr_closed"}

# Back to the caller's vocabulary, so the console and the tests keep
# speaking about questions and plan approvals rather than kinds.
_OUT = {"clarification": "question", "decision": "question",
        "authorization": "plan_approval", "direction": "direction",
        "landing": "landing", "closeout": "closeout",
        "pr_closed": "pr_closed"}


class ApprovalRefused(Exception):
    """A plan may not be approved yet, and why.

    Raised rather than returned so no caller can approve by ignoring a
    return value — the failure this would be guarding against is exactly
    somebody not checking.
    """


class ParkRequested(Exception):
    """Raised through the tool path to end the leg. Carries the request id."""

    def __init__(self, request_id: int, kind: str, summary: str):
        super().__init__(summary)
        self.request_id = request_id
        self.kind = kind
        self.summary = summary
