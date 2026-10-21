"""Who owns a pod, a Job or a claim -- and therefore who may destroy it.

THE INVARIANT, IN ONE SENTENCE. A pod with its OWN CONTROLLER has its
own lifecycle and is never a reaper's business. The Job decides when its
pod dies, `run_job` deletes it in a `finally`, and
`ttlSecondsAfterFinished` is the backstop. A second reaper racing that
controller can only ever be wrong.

WHY THIS IS A MODULE AND NOT FIVE COPIES OF A CONDITION. Measured on
execution 208: a capture Job was seven seconds into a `pip install` when
reconcile ran, and the conductor destroyed its own verifier --

    reconcile: 1 goal(s), 1 reap, 1 release
    Killing  Stopping container work
    Killing  Stopping container capture

-- after which the verifier reported "the Job failed and its pod is
gone, so there is no record of what the criterion did". That was true,
and the conductor had done it. The chain was entirely label arithmetic:
`capture_manifest` stamps `managed-by: kernel`, correctly -- the pod IS
ours -- so `list_containers()` returns it; it stamps `execution-id` only
when the spec carries one, and neither capture call site does; so
`orphan_sweep` saw a kernel-owned pod with no execution, concluded "no
matching execution", and called `destroy(stop_timeout=0)`.

Each reaper had inferred ownership from whichever labels it happened to
read. Five inferences are five chances to be wrong, and the fifth was
going to be written by copying the fourth: Phases D and E each add a pod
kind. Stated once, a new kind is wrong in one place or right in all of
them. Every reaper in `kernel/conductor/` now opens with the same three
sentences -- what it owns, what it must never touch, what it does with
something it cannot identify -- and defers to the predicates here.

AND PHASE E's TWO OBJECTS, OWNED DELIBERATELY RATHER THAN BY LUCK.
`kernel/verifier/pod.py` builds a BARE POD -- a Job runs to completion
and a warm pod by definition does not -- so it classifies `KERNEL`
here, which means reapable, and `orphan_sweep` was already collecting
it on terminal before anybody decided that it should. Its OWNER is
`reclaim.py`, whose authority is the execution ROW; the sweep stays on
as the backstop for a tick where the claim listing fails, and now says
which KIND of pod it took rather than reporting a verification pod as
"container ... destroyed".

Its disk is `verify-ws-<id>` and NOT `ws-exec-<id>`, deliberately, so
that `workspace_execution` below does not parse it and the reclaim pass
cannot delete an environment out from under a running verification.
The cost of that protection was that NOTHING collected it: not
`list_workspaces`, which only enumerates names it can parse, and not
`ephemeral.py`, which selects on a label the claim does not carry. An
uncollected claim is a quota leak against a ceiling of 120, and every
execution now holds two. `execution_in_name` is the parsing rule made
reusable so the second prefix inherits the digits-or-nothing protection
instead of re-implementing it -- the prefixes themselves stay spelled
once each, `CLAIM_PREFIX` here and the verification pair in
`kernel/verifier/pod.py`, because that file cannot import this one back.

AND THE DEFAULT IS TO KEEP. `jobs_sweep._wanted` and `list_workspaces`
already had that rule: an owner that does not parse, a claim whose name
is not `ws-exec-<digits>`, is left alone, because a sweep that destroys
what it cannot identify is a sweep that destroys somebody's hand-made
debugging Job. Nothing here may relax it -- but nor may it be read as
"reap nothing unfamiliar": an unlabelled bare pod claimed by no
execution IS identified, as an orphan, and twelve of those once held
bridge ports until an unrelated gate failed with "no free port in
9200-9399".
"""

from __future__ import annotations

from enum import Enum

# ── the labels, spelled once ───────────────────────────────────────────

MANAGED_BY_LABEL = "managed-by"
KERNEL = "kernel"

# What every lister selects on, and therefore the outer bound on what
# any reaper can reach. `kubernetes.list_containers`,
# `kubernetes.list_workspaces`, `jobs_sweep` and the docker driver's
# container filter each carried their own copy of this string -- four,
# and a fifth would have arrived with the next lister. A Docker label
# filter and a Kubernetes label selector spell `key=value` the same way,
# so one constant serves both.
KERNEL_SELECTOR = f"{MANAGED_BY_LABEL}={KERNEL}"

# The execution a thing was made for, when its maker knew one.
OWNER_LABEL = "execution-id"

# TWO SPELLINGS OF "EPHEMERAL", AND THEY ARE NOT THE SAME STATEMENT.
#
# `agentos.ephemeral` on a POD is its maker saying "a controller owns my
# lifecycle" -- a fact about who deletes it, which is what a pod reaper
# must respect. `agentos-ephemeral` on a CLAIM is `podcheck` saying
# "nobody will miss this after a while" -- a fact about who wants it,
# which is what an age-based reaper needs. One label cannot answer both
# questions, and unifying the spelling would in any case strand every
# object already running under the old one: a selector that matches
# nothing is a reaper that silently collects nothing, which is the
# failure ephemeral.py exists to fix.
EPHEMERAL_POD_LABEL = "agentos.ephemeral"
EPHEMERAL_CLAIM_SELECTOR = "agentos-ephemeral=true"

# Ours is first; the rest are Kubernetes' own, stamped on every pod a
# Job creates whatever its author remembered. THAT REDUNDANCY IS THE
# POINT. Execution 208 happened because a manifest did not carry a label
# a sweep wanted, and the next pod kind -- Phase D's, Phase E's -- will
# be written by somebody who has not read this file. A Job-made pod is
# protected here even if it wears none of our labels at all.
CONTROLLER_LABELS = (
    EPHEMERAL_POD_LABEL,
    "job-name",
    "batch.kubernetes.io/job-name",
    "controller-uid",
    "batch.kubernetes.io/controller-uid",
)

# ONE SPELLING OF THE CLAIM NAME, and it is load-bearing rather than
# cosmetic: dispatch builds it, `release_workspace` deletes it and
# `list_workspaces` parses the execution id back out of it, so the name
# is how a disk is matched to the row that says whether the work is
# finished with.
CLAIM_PREFIX = "ws-exec-"


class Owner(str, Enum):
    """Who is responsible for ending a thing's life.

    CONTROLLER  something else deletes it; a reaper may only wait.
    KERNEL      we created it directly and nothing else will collect it.
    FOREIGN     it says it belongs to somebody else. Never ours to touch.
    """

    CONTROLLER = "controller"
    KERNEL = "kernel"
    FOREIGN = "foreign"


def is_foreign(labels) -> bool:
    """True only when a label NAMES a different owner.

    ABSENCE IS NOT FOREIGNNESS. Both listers already filter on
    `managed-by=kernel`, so an unlabelled thing in their output is one
    whose labels were lost, not somebody else's -- and the containers
    the orphan sweep exists to collect arrive exactly that way. Treating
    a missing label as "not ours" would have disabled the sweep while
    looking like caution.
    """
    owner = labels.get(MANAGED_BY_LABEL)
    return owner is not None and owner != KERNEL


def pod_has_own_controller(labels) -> bool:
    """True when a Job (or any controller) decides this pod's fate."""
    return any(labels.get(name) for name in CONTROLLER_LABELS)


def pod_owner(labels) -> Owner:
    """Classify a pod the way a pod reaper must read it."""
    if is_foreign(labels):
        return Owner.FOREIGN
    if pod_has_own_controller(labels):
        return Owner.CONTROLLER
    return Owner.KERNEL


def may_reap_pod(labels) -> bool:
    """The one question `orphan_sweep` and `reclaim` are allowed to ask
    of a pod before destroying it."""
    return pod_owner(labels) is Owner.KERNEL


def may_reap_job(labels) -> bool:
    """And the one a JOB reaper asks -- which is a different question.

    DO NOT COPY `may_reap_pod` HERE. A Job wears `agentos.ephemeral`
    too, and on the Job that label means "throwaway", not "somebody else
    owns me": nothing controls a Job we created. Reading the pod rule
    onto Jobs would skip every Job there is and turn `jobs_sweep` into
    an expensive no-op -- silently, since a sweep that collects nothing
    looks exactly like a clean cluster.
    """
    return not is_foreign(labels)


def owner_label(labels) -> str | None:
    """The raw `execution-id`, or None when the thing does not claim one.

    ABSENT AND UNREADABLE ARE DIFFERENT FACTS and the reapers answer
    them differently -- a bare pod naming no execution is an orphan, a
    pod naming one we cannot parse is a mystery and is kept -- so the
    two are asked for separately rather than collapsed into `None`.
    """
    raw = labels.get(OWNER_LABEL)
    return raw if raw else None


def parse_execution_id(raw) -> int | None:
    """An `execution-id` label value as an id, or None when it is not one.

    None means "no readable owner", which every caller treats as a
    reason to KEEP. It is a function rather than an `int()` at each call
    site because `int()` RAISES: in `orphan_sweep` that exception
    escaped the loop, so a single mislabelled pod would have stopped
    every later reap in the same tick.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def execution_of(labels) -> int | None:
    """The execution a thing claims, read off its labels."""
    return parse_execution_id(owner_label(labels))


def claim_name(execution_id) -> str:
    """The workspace claim for an execution."""
    return f"{CLAIM_PREFIX}{execution_id}"


def execution_in_name(name, prefix) -> int | None:
    """The execution a PREFIXED object name says it belongs to, or None.

    TWO PREFIXES NOW, AND ONE RULE. The workspace claim is
    `ws-exec-<id>` and the verification claim is `verify-ws-<id>`, and
    the protection both need is identical: the tail must be DIGITS or
    the object belongs to nobody this reaper can name. Copying four
    lines into the second reaper is how the two would come to disagree
    -- `int(tail)` in one of them raises, and in a sweep an exception
    escapes the loop and stops every later reap in the same tick.

    The prefixes are NOT enumerated here. A caller passes the one it
    means, so a name can never be parsed by the wrong reaper's rule:
    `verify-ws-42` read with `CLAIM_PREFIX` is None, which is exactly
    the invisibility that keeps `reclaim` off a live verification.
    """
    if not name or not name.startswith(prefix):
        return None
    tail = name[len(prefix):]
    return int(tail) if tail.isdigit() else None


def workspace_execution(name) -> int | None:
    """The execution a WORKSPACE claim's name says it belongs to, or None.

    None is the protection: `podcheck` writes `ws-podcheck-<ts>` through
    the same manifest builder and wears the same `managed-by` label, and
    an operator's hand-made claim wears whatever they typed. Reaping on
    the label alone would take both. `ephemeral.py` collects podcheck's
    by age and by its own label, which is the narrower statement.
    """
    return execution_in_name(name, CLAIM_PREFIX)
