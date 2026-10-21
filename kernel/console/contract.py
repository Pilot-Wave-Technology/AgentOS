"""The contract a console run is dispatched under.

Without one the adjudicator reports `no_contract` every tick and the
story sits in VERIFYING for ever — which is precisely the state F2b
exists to end, arriving by a different road. `create_execution` never
set `contract_id`, so every console story to date has been unjudgeable.

WHAT "DONE" MEANS IS FIXED BEFORE THE WORK STARTS. That is the whole
point of a contract and it is why this runs at start and not at claim
time: criteria written after the work has been seen are criteria written
to fit it.

The check surface is hashed HERE, over the workspace as it is at
dispatch — which is the tree a person approving this would be looking
at. F4's third defence has no meaning otherwise: a surface hashed after
the executor has run records whatever the executor left.

H1b replaces this with configuration derived from a real contract row
supplied by the Conductor. Until then the console composes one, because
a console that cannot produce a judgeable story cannot be used to test
the Verifier at all.
"""

from __future__ import annotations

import json
import logging

from kernel.verifier.surface import hash_tar
from kernel.verifier.tree import archive_committed_tree, head_commit

logger = logging.getLogger(__name__)

# The Verifier runs criteria in THIS image, and it must be able to run
# them. python:3.12-slim has no pytest, which is how F4's first run
# produced six false greens. The sandbox is built from the same image so
# exec/verify parity starts at 1 rather than being repaired later.
# ONE DECLARATION, shared with `kernel/worker/contracts.py`. These were
# two copies of one literal and the cloud could pull neither.
from kernel.base_image import default_base_image

# E6 -- and a NAME is not yet a pin. `base_digest` has existed since F1
# and nothing ever wrote it, so every run on 2026-09-17 logged "the
# contract names an image by TAG, not a digest". Resolved HERE because
# here is where the approval happens: the digest recorded must be the
# one that was approved, not whatever the tag means at verification.
from kernel.image_digest import pin

DEFAULT_IMAGE = default_base_image()


def parse_checks(text: str) -> list[str]:
    """One command per line. Blank lines and `#` comments ignored."""
    return [line.strip() for line in (text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def parse_globs(text: str) -> list[str]:
    """Comma or newline separated."""
    raw = (text or "").replace(",", "\n")
    return [g.strip() for g in raw.splitlines() if g.strip()]


async def create(conn, goal_id: str, kind: str, domain: str,
                 workspace: str, checks: list[str], surface: list[str],
                 image: str = DEFAULT_IMAGE, approved_by: str = "console"):
    """Write the contract and its criteria. Returns (id, note).

    Returns a NOTE rather than raising when there is nothing to check: a
    run with no criteria is legitimate — an errand, or somebody
    exploring — and refusing to start it would make the console less
    useful than it was. But it will never reach DONE, and saying so at
    the moment of starting is worth more than discovering it later.
    """
    if not checks:
        return None, ("no checks given, so this run has no definition of "
                      "done — it can work and commit, and the adjudicator "
                      "will report it as unjudgeable rather than passing it")

    # Hashed over the tree AS IT IS NOW. A surface hashed after the
    # executor runs records whatever the executor left, which is the
    # defence agreeing with the attack.
    surface_hash = None
    if surface:
        try:
            tar = archive_committed_tree(workspace, head_commit(workspace),
                                         prefix="verify/")
            surface_hash = hash_tar(tar, surface, prefix="verify/")
        except Exception as e:                              # noqa: BLE001
            logger.warning("could not hash the check surface: %s", e)
            return None, (f"the check surface could not be hashed ({e}); "
                          f"starting WITHOUT surface protection")

    # When it cannot be pinned the digest stays NULL and the reason goes
    # onto the contract, so the parity warning keeps firing AND can say
    # what would fix it. A silent fallback would be worse than today.
    base_digest, spec = pin(image, {"check_surface": surface})
    contract_id = await conn.fetchval(
        """
        INSERT INTO contracts (goal_id, kind, domain, base_image,
                               base_digest, workspace_spec,
                               check_surface_hash, approved_at, approved_by)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, now(), $8)
        RETURNING id
        """,
        goal_id, kind, domain, image, base_digest,
        json.dumps(spec), surface_hash, approved_by)
    # ONE FUNCTION DECIDES WHAT A CHECK IS, and it is not this one.
    #
    # This loop used to read `run` and `label` off each check, hardcode
    # `kind='command'` and write `{"run": run}` as the whole spec. It is
    # the console twin of the defect `worker/adopt.py` carries a comment
    # block about: an `artifact[role=report]` check became a shell line
    # that can never pass, and a declared `timeout_s` was dropped on the
    # floor. Two writers, one shape between them, and only one of them
    # understood it.
    #
    # `normalise` is that one. It resolves the artifact notation in both
    # spellings, reads `timeout_s` from the top level or from `spec`, and
    # defaults the label to `unlabelled` -- everything this loop did, plus
    # everything it silently discarded.
    # `_insert_criteria` is called rather than reimplemented, because the
    # spec column is not the normalised `spec` -- it is `{"run": run,
    # **spec}`, and `run` is kept even on an artifact criterion so the
    # card and the adjudicator's log have something to print. Writing
    # that out a second time here is how the two writers drifted apart in
    # the first place.
    #
    # The label still goes in AT INSERT, which `_insert_criteria` also
    # does: F1 lets `agentos_worker` write a criterion and not edit one --
    # verdicts are keyed to criteria, so a label set afterwards would be
    # an edit under a verdict that may already exist. Setting it at
    # insert is the only legal moment, and the refusal that revealed that
    # is the grant doing its job.
    from kernel.worker.contracts import _insert_criteria, normalise

    # COUNTED AFTER NORMALISING, because `normalise` drops a check whose
    # `run` is empty. Reporting the count that was handed in would name a
    # criterion that does not exist.
    settled = normalise(checks)
    await _insert_criteria(conn, contract_id, settled)

    note = (f"{len(settled)} check(s)"
            + (f", {len(surface)} surface glob(s) hashed"
               if surface_hash else ", NO check surface — an agent that "
                                    "edits a test is not caught"))
    logger.info("contract %s for %s/%s: %s", contract_id, kind, domain, note)
    return contract_id, note
