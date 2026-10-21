"""Why a pod has not started YET — Kubernetes' own sentence, not the clock.

WHAT WAS TRUE BEFORE. `promote.settle` had exactly one question for a
sandbox that is not answering: how long has it been. Under
`STARTUP_BUDGET_S` it said "waiting"; over it, "no answer in 300s
(budget 300s)". Both sentences are equally true of a pod pulling a cold
image and of a pod the namespace has REFUSED, and those need opposite
responses from whoever is reading — the first resolves itself, the
second never will and no amount of waiting helps.

THE MEASUREMENT (D5, plan §11.5i). `admit.SYSTEM_CEILING = 16` counts
EXECUTIONS and is blind to tier. Sixteen `small` pods are 28 vCPU / 104
GiB against the namespace quota in `scripts/infra/gke_cluster.sh` (128
CPU, 512Gi) — comfortable. Sixteen `xlarge` are 132 vCPU / 520 GiB, over
on BOTH axes. Nothing admits at xlarge today, so this has never been
seen; `test_the_ceiling_is_priced_against_the_real_quota` recomputes it
from both sources on every run, and is what will say when it changes.

TWO REFUSALS WEAR THE SAME `Pending`, AND THEY ARE NOT THE SAME NEWS.

  THE NAMESPACE REFUSED IT. A ResourceQuota is an operator-set ceiling.
  Waiting cannot change it, and — the part that makes patience actively
  harmful — the Pending pod is itself spending the quota it is queued
  for. `ephemeral.py` already reasons this way about CLAIMS ("in
  Kubernetes a Pending claim counts against the quota like any other");
  the insight was never applied to pods.

  NO NODE HAS ROOM YET. `agentos` is a GKE AUTOPILOT cluster: there are
  no node pools to resize, and Google adds a node to fit a pod that has
  been admitted. That takes a minute or two, during which
  `PodScheduled` is False for a pod that is perfectly healthy. Treating
  this as a refusal would destroy and rebuild pods that were about to
  start — the same expensive mistake `readiness.py` refuses to make
  when a probe hiccups.

Collapsing those two is how the first arrives disguised as the second's
symptom. So this reads the condition and says WHICH.

A THIRD REFUSAL DOES NOT COME THROUGH HERE AT ALL, and it is worth
writing down so nobody looks for it here. ResourceQuota is an ADMISSION
plugin, not the scheduler: a pod whose requests do not fit is refused by
the API server at `create`, with 403 and a body naming the resource, the
amount used and the limit. That raises out of `driver.create`, never
becomes a pod object, and so is invisible to everything in this file.
Plan §11.5i describes the D5 trap as the quota "refusing to schedule";
on Autopilot with a namespace ResourceQuota the sixteenth `xlarge` pod
is far more likely to be refused at CREATE than left Pending, so the
create path is the one the trap will probably take.

THAT PATH USED TO BE ILLEGIBLE, AND IS NOT ANY MORE — fixed, and
recorded here because this is where the next person looking at a quota
refusal will be reading. `runner.dispatch_pass` wrote the exception as
`f"dispatch failed: {type(e).__name__}: {e}"[:500]`, and
`ApiException.__str__` puts the HTTP RESPONSE HEADERS in front of the
body, so the 500 characters kept ended inside
`"kind":"Status","apiVersion"...` and not one of `exceeded quota`,
`requested:`, `used:` or `limited:` survived. Both halves of the D5 trap
misled, for two unrelated reasons.

`kernel/drivers/k8s_error.py` now supplies the other half:
`sentence(exc)` reads `Status.message` off `exc.body` — the only place
the numbers live — and `dispatch_pass` stores that instead of `str(e)`.
This file still owns the Pending half and nothing about it changed.

TWO THINGS MUST NOT BE "SIMPLIFIED", and both have the same shape — a
change that turns this module into a no-op no test can tell from a quiet
cluster.

  THE `is_pod` GATE. Three other sweeps in this package spell the same
  gate `getattr(driver, "name", "") != "kubernetes"`, and that idiom is
  BROKEN — no driver in this tree defines `.name` (neither
  `KubernetesDriver`, nor `DockerDriver`, nor the `Driver` base), so the
  guard is always true and the sweep behind it returns `[]`
  unconditionally. `WORKSPACE_MODEL`, which `podfs_client.is_pod` reads,
  is the attribute that actually exists.

  THE CLIENT COMES FROM THE DRIVER. `_core_api_of` explains this at
  length: a freshly constructed `CoreV1Api()` inherits whatever
  `Configuration` is the process default, and the only `load_*_config()`
  call in this tree lives on the driver. The draft that built its own
  client would have failed every call and been swallowed at WARNING.
"""

from __future__ import annotations

import logging

from kernel.workspace.podfs_client import is_pod

logger = logging.getLogger(__name__)

# The two verdicts. Deliberately not an enum: these travel into a
# `stall_reason` string and out to test assertions, and a bare str is
# what both of those want.
REFUSED = "refused"
UNSCHEDULED = "unscheduled"

# PLACED AND WORKING, not placed and wedged. P3 put `env_setup` in an
# init container (`workspace-setup`) so a thirty-minute install stops
# eating the leg's 45-minute wall clock -- and that immediately made
# STARTUP_BUDGET_S wrong, because 300s was measured against the TWO
# init containers that existed then: an image pull and a clone.
#
# Without this verdict a monorepo install crosses 300s, promotion
# declares the pod lost, destroys it, dispatch rebuilds it, and the
# install starts again -- A REBUILD LOOP, and the bigger the
# repository the more certain it is. Q1 predicted the shape and could
# not close it: `PodScheduled=True` is all a CONDITION says, and
# telling "cold image" from "wedged" needs the container statuses.
INSTALLING = "installing"

# HOW A QUOTA REFUSAL IS RECOGNISED. Matched on the MESSAGE and not the
# `reason`, because `reason` is `Unschedulable` for a namespace ceiling
# and for a shortage of nodes alike — the distinction only exists in the
# sentence the scheduler wrote. The API server's own 403 body says
# "... is forbidden: exceeded quota: <name>, requested: ..., used: ...,
# limited: ...", and a `FailedScheduling` that names the quota carries
# the same words, so one list covers both spellings.
_QUOTA_WORDS = ("exceeded quota", "forbidden", "resourcequota")

# What a person reads first. The scheduler's own message follows it, and
# it is the message that names the resource and the limit — which is the
# only part an operator can act on.
_HEADLINE = {
    REFUSED: "the namespace refused this pod",
    UNSCHEDULED: "no node has room for this pod yet",
}

# The SQL `LIKE` patterns that match a sentence THIS module wrote,
# derived from the headlines above so the two can never drift apart.
# `promote_row.unnote` takes back a reason that stopped being true, and
# it must take back only ours: a DISPATCHING row can equally carry a
# dispatch failure or a contract refusal, and erasing one of those would
# be the same silence this whole item is about, caused by the fix for it.
# No headline contains `%` or `_`, so none of them is accidentally a
# wildcard — a headline that gains one must be escaped here.
NOTE_PATTERNS = [f"{head}:%" for head in _HEADLINE.values()]

# ONE BUDGET IS SHORTENED, AND ONLY ONE.
#
# `promote.STARTUP_BUDGET_S` is 300s and that is right for a pod that
# has been PLACED: two init containers run before either of ours, one of
# them a git clone, and a slow clone should cost a slow start rather
# than a rebuild loop. It is pure delay for a pod the namespace has
# refused — five minutes of waiting to report something decided at once,
# by a ceiling no amount of waiting moves.
#
# NOT ZERO for the refused case, and the reason is measured elsewhere in
# this package: `reclaim` leaves the row DISPATCHING for reconcile,
# which advances the generation and hands it back to ADMITTED, and
# dispatch then builds the pod again. Acting on the first tick would
# make that a tight rebuild loop against a quota that may be seconds
# from freeing as another run finishes. Thirty seconds is about seven
# ticks — long enough that a refusal already clearing clears, short
# enough that the pod stops squatting on the quota it is queued for.
#
# And the rebuild it leads to is not silent: the second `create` meets
# the SAME full quota at the API server, which refuses it with 403 and a
# body naming the resource, and `runner.dispatch_pass` writes that to
# `stall_reason`. So the loop describes itself on the row every time
# round, which is the property that makes a loop tolerable.
REFUSED_BUDGET_S = 30.0

# What a pod running `workspace-setup` may take. `limits.JOB_DEFAULT_S`
# is 1800 -- the budget the harness gives an install when its caller
# declares none, chosen because "a cold `pnpm install` on a monorepo is
# minutes rather than seconds". The init container runs the same
# commands, so it gets the same number plus a minute for the pull and
# the clone ahead of it. One number, two places, and they are the same
# fact about how long an install takes.
#
# READ, NOT RESTATED. `setups.BUDGET_S` is that number now -- the same
# `JOB_DEFAULT_S` plus the same minute -- and the Verifier's prepare Job
# reads it too, so the two rooms cannot be given different amounts of
# time for the one committed command they both run. They were: 1860
# here against 900 there, which made a slow-installing repository work
# for the executor and time out in verification.
#
# THE VERIFIER SCALES THIS PER MOUNT AND THIS DOES NOT, deliberately. A
# setup that overruns here is not fatal -- `ensure_setup` reports and
# the leg still runs, so the model can call `env_setup` per mount under
# its own budget. An overrun THERE decides a verdict, and there is no
# second attempt to fall back on.
from kernel.verifier.setups import BUDGET_S as SETUP_BUDGET_S

# The init container P3 added. Named here rather than imported from
# `k8s_workspace` because this module is read by the CONDUCTOR against
# a pod it did not build, and the manifest builder is a different
# concern -- but they must agree, and a test asserts they do.
SETUP_CONTAINER = "workspace-setup"

# THERE IS DELIBERATELY NO `UNSCHEDULED_BUDGET_S`, and the first draft of
# this module had one at 120s. Three findings killed it:
#
#   IT BUYS THE OPERATOR NOTHING. The sentence reaches `stall_reason`
#   at `SCHEDULING_GRACE_S` — fifteen seconds — whatever the budget is.
#   The budget decides only when the pod is DESTROYED, and destroying it
#   sooner tells nobody anything they were not already told at 15s.
#
#   IT CAN STOP THE VERY THING IT IS WAITING FOR. On GKE AUTOPILOT the
#   Pending pod IS the scale-up request. Destroying it cancels the node
#   and recreating it starts the clock over, so a tier whose node takes
#   longer than the budget never converges — and the slowest nodes are
#   the large ones this whole item is about.
#
#   THE CASE IT WAS AIMED AT IS NOT FIXED BY ANY NUMBER.
#   `p3-cloud-workspace.md` records a live pod that "stays Pending for
#   ever" (`nodeSelector: {agentos.io/node-class: ...}`, a label no
#   Autopilot node carries). That loops at 120s and loops at 300s; what
#   ends it is a person reading the scheduler's sentence.
#
# So UNSCHEDULED changes the WORDS and not the clock.

# Below this age, do not ask. EVERY pod is briefly unscheduled — the
# scheduler has not looked at a pod one second old — so asking about a
# fresh one buys an API call per row per tick and a false alarm on the
# board. It is half of `REFUSED_BUDGET_S`, so nothing that matters is
# hidden by it.
SCHEDULING_GRACE_S = 15.0


def classify(message: str) -> str:
    """REFUSED if the sentence names a quota, UNSCHEDULED otherwise.

    Defaults to UNSCHEDULED — the patient answer — because a message
    this does not recognise is evidence of nothing, and calling an
    unrecognised sentence a refusal would tear down healthy pods on a
    Kubernetes release that reworded something.
    """
    text = (message or "").lower()
    return REFUSED if any(word in text for word in _QUOTA_WORDS) \
        else UNSCHEDULED


def sentence(verdict: str, said: str) -> str:
    """One line an operator can act on: what happened, then who said it.

    The scheduler's own message is kept VERBATIM and second. It is the
    half that names the resource, the amount used and the limit, and
    paraphrasing it would drop exactly the numbers somebody needs to
    decide whether to raise the quota or cancel a run.
    """
    return f"{_HEADLINE.get(verdict, verdict)}: {said}"


def scheduling_verdict(pod) -> tuple[str, str] | None:
    """(REFUSED | UNSCHEDULED, what Kubernetes said), or None if placed.

    None is the ordinary case and means the caller's normal patience
    applies: a placed pod is pulling an image or cloning repositories,
    and both genuinely take minutes.

    A pod with NO `PodScheduled` condition at all is also None. The
    object exists and the scheduler has not reached it — too new to
    judge, and `SCHEDULING_GRACE_S` already covers that window.

    Written against attributes rather than a client type so it can be
    exercised on a plain object. The refusal it recognises cannot be
    reproduced on a laptop, so the only way it is ever tested is by
    handing it the shape the API returns.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return None
    for cond in (getattr(status, "conditions", None) or []):
        if getattr(cond, "type", "") != "PodScheduled":
            continue
        if str(getattr(cond, "status", "")) == "True":
            return None
        said = (getattr(cond, "message", None)
                or getattr(cond, "reason", None)
                or "the scheduler has not placed it and did not say why")
        return classify(said), said
    return None


def installing(pod) -> bool:
    """Is the pod running P3's setup init container right now?

    Read from `initContainerStatuses`, which is the only place that says
    WHICH init container is running -- `PodScheduled` says the pod was
    placed and nothing more, which is exactly the gap Q1 recorded and
    could not close from conditions alone.

    Attributes rather than a client type, the same as
    `scheduling_verdict`: a running install cannot be reproduced on a
    laptop, so the only way this is ever tested is by handing it the
    shape the API returns.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return False
    for each in (getattr(status, "init_container_statuses", None) or []):
        if getattr(each, "name", "") != SETUP_CONTAINER:
            continue
        state = getattr(each, "state", None)
        return getattr(state, "running", None) is not None
    return False


def ceiling_for(verdict: str | None, default: float) -> float:
    """The longer patience an installing pod needs. SEPARATE FROM
    `budget_for` ON PURPOSE.

    `budget_for` may only ever SHORTEN, and its docstring says why:
    `promote_one` is called from `start` with `budget=0` behind a person
    pressing a button -- "this was your one chance" -- and a table
    lookup that silently lengthened that would be the opposite of the
    point. That rule is right and this does not bend it; it is a second
    function, applied FIRST, and it lengthens only in the one case where
    the default is wrong rather than merely short.

    GUARDED ON `default >= STARTUP`. A caller that deliberately chose
    less than the standard patience has said something, and an install
    is not a reason to overrule them. Only a caller taking the ordinary
    budget gets the ordinary budget corrected.

    The pairing is `budget_for(v, ceiling_for(v, default))`: lengthen
    for the install, then let a refusal shorten it again -- a pod that
    is BOTH refused and somehow installing is refused, because it has no
    node and nothing is running on it.
    """
    from kernel.conductor.promote import STARTUP_BUDGET_S  # noqa: PLC0415

    if verdict == INSTALLING and default >= STARTUP_BUDGET_S:
        return SETUP_BUDGET_S
    return default


def budget_for(verdict: str | None, default: float) -> float:
    """How long to keep waiting, given why the pod has not started.

    ONLY REFUSED MOVES IT. UNSCHEDULED returns the caller's own budget
    untouched, on purpose — see the constants above: shortening it tells
    nobody anything the 15-second note did not, and on Autopilot the
    Pending pod IS the scale-up request, so destroying it early cancels
    the node it is waiting for.

    `min` AND NOT A PLAIN LOOKUP. `promote_one` is called from `start`
    behind a person pressing a button with `budget=0` — "this was your
    one chance" — and `promote_ready` may be called with a shorter
    budget by a test or a soak. A table lookup would silently LENGTHEN
    those, which is the opposite of the point: this may make the wait
    shorter and must never make it longer.
    """
    if verdict == REFUSED:
        return min(default, REFUSED_BUDGET_S)
    return default


def _core_api_of(driver):
    """The DRIVER'S OWN client, or None when there is nothing to ask.

    NOT a fresh `CoreV1Api()` off `kernel.drivers.kubernetes._api()`,
    which is what the first draft did and which is wrong in a way that
    hides itself: that constructor inherits whatever `Configuration` is
    the process default, and `KubernetesDriver._core_api` is the ONLY
    thing in this tree that calls `load_incluster_config()`. A bare
    client works only once the driver happens to have built one, and
    otherwise points at `http://localhost`, where `quiet` swallows the
    failure at WARNING and returns None — a no-op indistinguishable from
    a quiet cluster, the same trap as the `.name` gate above.

    A REACH-THROUGH, ACKNOWLEDGED. `handover.py` records
    `driver._core_api()` being reached through "until the seam existed
    to take it"; the seam here would be `Driver.pod_conditions()`, and
    `kernel/drivers/` is not this item's to change. Guarded twice over —
    `is_pod` first, because Docker has no scheduler and no quota to ask
    about; then `getattr` and `except`, so neither a double that only
    does create/destroy nor a driver whose config load fails can raise
    inside a `gather` and be charged to the pod.
    """
    if not is_pod(driver):
        return None
    try:
        getter = getattr(driver, "_core_api", None)
        return getter() if callable(getter) else None
    except Exception:                                       # noqa: BLE001
        logger.warning("no Kubernetes client to ask with", exc_info=True)
        return None


async def why_pending(driver, container_id: str) -> tuple[str, str] | None:
    """Ask the substrate why this pod has not started. None if it cannot say.

    NEVER RAISES and never blocks the pass: `quiet` bounds the call and
    swallows its failure at WARNING. A conductor that cannot reach the
    control plane must still promote the sandboxes that ARE answering —
    degrading to the old clock-only behaviour is the correct failure
    here, and it is what `None` means.
    """
    api = _core_api_of(driver)
    if api is None:
        return None
    from kernel.drivers.k8s_call import quiet                # noqa: PLC0415

    pod = await quiet(api.read_namespaced_pod,
                      what=f"asking why pod {container_id} has not started",
                      name=container_id,
                      namespace=getattr(driver, "namespace", None))
    if pod is None:
        return None
    verdict = scheduling_verdict(pod)
    if verdict is not None and verdict[0] == REFUSED:
        logger.warning("pod %s: %s", container_id, sentence(*verdict),
                       extra={"event": "tick"})
        return verdict
    # PLACED, AND POSSIBLY WORKING. `scheduling_verdict` returns None
    # for a placed pod -- correct, and not enough since P3: an install
    # in `workspace-setup` is placed AND unable to answer for up to
    # half an hour, and the caller's 300s would destroy it and start
    # the install again. Asked only when the pod is not refused,
    # because a pod with no node is running nothing.
    if verdict is None and installing(pod):
        return (INSTALLING,
                "its environment is still installing "
                f"({SETUP_CONTAINER})")
    return verdict
