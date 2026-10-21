"""Claims nobody owns and nobody will miss. Reaped by age.

THE ONE THAT BIT US. `list_workspaces` reaps only claims named
`ws-exec-<digits>` -- everything else is skipped, deliberately, so that
an operator's hand-made claim is never deleted by a sweep. That rule is
right and this is the hole in it: `podcheck` writes `ws-podcheck-<ts>`
through the same manifest builder, so it is KERNEL-made but unreapable,
and its only collector is a best-effort `kubectl delete` that is skipped
under `--keep` and never runs at all if the script is killed.

Measured 2026-09-17: one such claim Pending for nineteen hours. The
namespace quota is 120 claims; a claim survives its pod on purpose, so
this is not "120 at once", it is 120 ever unless something collects
them. The symptom arrives much later, on an unrelated goal, as "nothing
runs" -- which is the worst kind of resource leak to diagnose.

WHY A LABEL AND NOT A NAME. A name can say what made a claim; it cannot
say whether anybody still wants it. `agentos-ephemeral` is the maker
saying "nobody will miss this after a while", which is exactly what an
age-based reaper needs and the only thing that distinguishes a
throwaway claim from an operator's. Unlabelled claims stay protected,
so the rule `list_workspaces` exists to enforce is not weakened.

AGE AND NOT STATE. A claim stuck Pending -- no node, no capacity, a
storage class that cannot bind -- is the exact case that leaks, and it
never reaches a state a state-based sweep could recognise. In
Kubernetes a Pending claim counts against the quota like any other.

WHAT THIS REAPER OWNS: claims whose maker LABELLED them throwaway, and
only those. Nothing else collects them -- they are not `ws-exec-<id>`,
so the reclaim pass cannot see them, and podcheck's own `kubectl delete`
is skipped under `--keep` and absent if the script is killed.

WHAT IT MUST NEVER TOUCH: an unlabelled claim, which is one of three
things and none of them is this reaper's -- an execution's workspace
(reclaim.py's, released only when the story is finished with it), its
VERIFICATION claim (`verify-ws-<id>`, also reclaim.py's since plan 5.7),
or an operator's, made by hand and worth keeping.

AND THE VERIFICATION CLAIM IS NOT A CANDIDATE FOR THIS SWEEP, however
tempting it looks. It leaked for exactly the reason podcheck's did --
no collector -- so the obvious repair is to label it throwaway and let
age take it. Age is the wrong question about it: it is warm ON PURPOSE
across every generation whose manifest did not change, so a run being
refused five times in six hours is a healthy claim this would delete
mid-verification. `reclaim.py` asks the right one, which is whether the
EXECUTION is finished with it.

WHAT IT DOES WITH SOMETHING IT CANNOT IDENTIFY: keeps it. A claim with
no creation timestamp has no age to judge, so it falls outside the one
question this reaper is allowed to ask.
"""

from __future__ import annotations

import datetime
import logging

from kernel.ownership import EPHEMERAL_CLAIM_SELECTOR

logger = logging.getLogger(__name__)

# The claim spelling, from `kernel.ownership`, which also explains why
# it is NOT the pod spelling: on a pod `agentos.ephemeral` means "a
# controller owns my lifecycle" and forbids reaping, while here the
# label is the maker saying nobody will miss this -- the one thing that
# permits it. Two rules that read alike and mean opposite things are
# worth keeping in one file where the difference is written down.
EPHEMERAL_LABEL = EPHEMERAL_CLAIM_SELECTOR

# Long enough that a slow `podcheck` against a cold node pool is never
# reaped underneath itself, short enough that a day's worth cannot fill
# the quota. Nothing legitimate holds one of these for hours.
MAX_AGE_S = 4 * 3600.0


async def sweep_ephemeral(conn, driver, *, dry_run: bool = False) -> list:
    """Delete ephemeral claims older than MAX_AGE_S. Returns actions."""
    from kernel.drivers.k8s_call import quiet                # noqa: PLC0415
    from kernel.drivers.kubernetes import _api               # noqa: PLC0415

    # `claim_capable`, NOT `driver.name`. `unscheduled.py` wrote this
    # down and nobody acted on it: "that idiom is BROKEN -- no driver in
    # this tree defines `.name` ... so the guard is always true and the
    # sweep behind it returns `[]` unconditionally". This sweep has
    # therefore never run.
    from kernel.verifier.handover import claim_capable      # noqa: PLC0415

    if not claim_capable(driver):
        return []

    client, _ = _api()
    api = client.CoreV1Api()
    listing = await quiet(api.list_namespaced_persistent_volume_claim,
                          what="listing ephemeral claims",
                          namespace=driver.namespace,
                          label_selector=EPHEMERAL_LABEL)
    if listing is None:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    actions = []
    for claim in listing.items:
        name = claim.metadata.name
        created = claim.metadata.creation_timestamp
        if created is None:
            # No age to judge it by. Left alone, for the same reason an
            # unparseable name is: a sweep that cannot identify
            # something must not destroy it.
            continue
        age = (now - created).total_seconds()
        if age < MAX_AGE_S:
            continue
        if dry_run:
            actions.append({"execution_id": None, "action": "claim_orphan",
                            "detail": f"{name} is {age / 3600:.1f}h old"})
            continue
        await quiet(api.delete_namespaced_persistent_volume_claim,
                    what=f"deleting ephemeral claim {name}",
                    name=name, namespace=driver.namespace)
        logger.info("claim %s reclaimed: ephemeral and %.1fh old",
                    name, age / 3600, extra={"event": "reconcile"})
        actions.append({"execution_id": None, "action": "claim_reaped",
                        "detail": f"{name} ({age / 3600:.1f}h)"})
    return actions
