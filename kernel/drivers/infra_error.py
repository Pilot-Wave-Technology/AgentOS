"""What KIND of not-a-verdict this is, beside the sentence that says it.

FAULT 10 IS WHY THIS FILE EXISTS. A return channel shaped for a person
and consumed by a parser breaks silently and expensively: the capture
sidecar printed its id correctly at every step, `parts[0] == MARKER`
compared `bytes` to `str`, and a 45,864,960-byte capture that had WORKED
was reported as "stored nothing" -- two deploy cycles to find, with the
right answer printed in the logs the whole time.

`infra_error` had exactly that shape. Plan 5.8 distinguishes FIVE ways
verification fails, and rows 2 and 3 -- "the checks could not run" and
"the cluster ended this run" -- both arrive as `error` with `infra=True`.
The ONLY thing separating them was the sentence `k8s_outcome` happened to
write, so `kernel/verifier/preempted.py` matched its prefix and an
anti-drift test was needed to hold two spellings of one fact together. A
carefully worded sentence makes a fine explanation and a terrible
protocol; it should only have to be the first.

SO THE SENTENCE STAYS, AND A KIND TRAVELS BESIDE IT. `InfraError` IS the
sentence -- a real `str`, so every reader that logs it, formats it,
stores it or puts it in a criterion's detail is unchanged -- and it
carries the fact the classifier already knew at the moment it chose the
words. Nothing has to be re-derived from prose.

WHICH ROW EACH KIND IS, because that is the only distinction with teeth:
`DISRUPTED` is row 3 (no verdict, re-run on the surviving claim) and
every other kind is row 2 (`error`, three strikes, back to the executor).
"""

from __future__ import annotations

# ROW 3. Something OUTSIDE the pod ended the run -- a Spot reclaim, the
# eviction API, the taint manager, scheduler preemption. It says nothing
# about the work, so it must cost a re-run and nothing else.
DISRUPTED = "disrupted"

# ROW 2. The container never reached the starting line: the image would
# not pull, the config was rejected, the pod was still Pending when the
# Job's deadline expired. Nothing was checked, and the reason is about
# this run's declared environment rather than about the cluster's day.
NEVER_STARTED = "never_started"

# ROW 2. Killed for exceeding a memory ceiling nobody declared. Kept
# apart from NEVER_STARTED because it is the one kind 9.6's sizing can
# act on, and apart from DISRUPTED because re-running it unchanged buys
# the same death again.
OOM = "oom"

# ROW 2. The ending happened and nobody recorded it -- the node stopped
# reporting on the container, or the pod was collected before it could
# be read. NOT row 3: "we did not see it" is not evidence that the
# cluster ended it, and guessing in that direction buys free re-runs for
# a fault that would recur.
UNOBSERVED = "unobserved"

# ROW 2, AND THE DEFAULT. There will be Kubernetes failure modes nobody
# here anticipated; they cost a re-run and three strikes, not a refusal.
UNKNOWN = "unknown"


class InfraError(str):
    """The sentence a person reads, carrying the fact a caller branches on.

    A `str` SUBCLASS AND NOT A DATACLASS, deliberately. Every existing
    reader -- `outcome.unrunnable`, the prepare Job's log line, the
    environment report's `[...]` prefix, the investigate summary -- takes
    this value as text today, and a wrapper object would have made all of
    them the caller's problem for one new field.
    """

    def __new__(cls, sentence: str, kind: str = UNKNOWN):
        self = super().__new__(cls, sentence)
        self.kind = kind
        return self

    def __str__(self) -> str:
        # RETURNING `self` IS THE LOAD-BEARING LINE, not a tautology.
        # `str(x)` on a str SUBCLASS ordinarily returns a plain copy, and
        # a plain copy has no `kind` -- so one caller re-wrapping the
        # value (`kernel/verifier/batch.py:_missing_reason` does exactly
        # that when a batched Job's node is reclaimed) would silently
        # drop row 3 back into row 2. That is fault 10 again: the
        # information is present, printed, and lost at a type boundary.
        # `PyObject_Str` accepts any str instance back from `__str__`,
        # so this keeps the kind through `str()`, `%s` and `f"{}"`.
        return self


def kind_of(value) -> str:
    """The kind of an infra error, or `UNKNOWN` for anything untagged.

    UNTAGGED MUST MEAN ROW 2, and the asymmetry is the same one
    `k8s_outcome` argues: a lost kind then costs an `error` -- three
    strikes and a person -- while defaulting the other way would hand a
    free re-run loop to every unrecognised ending. Wrongly not re-running
    is recoverable; a loop nobody sees is not.
    """
    return getattr(value, "kind", "") or UNKNOWN
