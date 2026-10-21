"""F6 — how well the verification environment matched the working one.

The Verifier is only worth having while people believe it, and the way
it stops being worth having is not a dramatic failure. It is drift:
refusals arrive that are about the environment rather than the work,
people learn to override them, and it ends up catching nothing while
still reporting green.

That is erosion, and the only defence against erosion is a number
somebody looks at. So every verdict carries one.

THE HEADLINE SIGNAL IS `unrunnable`, not the pass rate. A criterion that
came back `error` did not judge the work — it failed to run — and R1
makes that non-refusing precisely so an environment problem cannot kill
a finished story. Which is correct, and which also means environment
problems are SILENT unless something counts them. A Verifier whose
criteria increasingly cannot run is one whose remaining refusals are
about to start being ignored.

WHY NOT MEASURE AGREEMENT WITH THE EXECUTOR. The obvious parity metric —
"the executor thought it passed and the Verifier disagreed" — is not a
parity signal at all. The executor claiming completion IS the model's
opinion, and the whole architecture exists because that opinion is not
evidence. A high disagreement rate might mean the environments diverged,
or it might mean the model is optimistic; the number cannot tell you
which, and a metric you cannot act on is a metric people stop reading.
`unrunnable` can only mean one thing.
"""

from __future__ import annotations

import logging

from kernel.image_digest import UNPINNED_KEY
from kernel.verifier.outcome import ERROR, FLAKE

logger = logging.getLogger(__name__)


def measure(contract: dict, deltas: list | None, env_report: dict | None,
            results: list) -> dict:
    """Parity for one verification. Goes on the verdict.

    Deliberately flat and small: this is read by a view, a console panel
    and a person, and a nested shape would need each of them to agree
    about how to walk it.
    """
    deltas = deltas or []
    skipped = len((env_report or {}).get("skipped") or [])
    values = [outcome.passes for _, outcome in results]

    parity = {
        # A tag means the Verifier may have run a different image than
        # the one approved. Rule 6 calls that permanent parity noise, so
        # it is counted rather than assumed away.
        "image_pinned": bool(contract.get("base_digest")),
        "image": contract.get("base_digest") or contract.get("base_image"),
        # E6 — WHY it is not pinned, when the contract knows. Writing
        # the digest was the fix; this is the half of it that survives
        # the fix failing. Executions 205-210 all carried the warning
        # and none of them could say what to do about it, so it read as
        # weather rather than as a repair somebody owns.
        "image_unpinned_because": ((contract.get("workspace_spec") or {})
                                   .get(UNPINNED_KEY)),
        "env_total": len(deltas),
        "env_reproduced": max(0, len(deltas) - skipped),
        "env_skipped": skipped,
        "criteria": len(results),
        "unrunnable": sum(1 for v in values if v == ERROR),
        "flaky": sum(1 for v in values if v == FLAKE),
        "services": len(((contract.get("workspace_spec") or {})
                         .get("services") or [])),
    }
    if parity["unrunnable"] or parity["env_skipped"] or not parity["image_pinned"]:
        logger.info("parity: %s", parity)
    return parity


def concerns(parity: dict) -> list[str]:
    """What a person should be told about this verification, in words.

    A number in a jsonb column is not a warning. These are the sentences
    the console shows next to a verdict, and they exist because the
    failure mode is somebody scrolling past a figure they did not
    interpret.
    """
    notes = []
    if not parity.get("image_pinned"):
        # THE WORDING IS LOAD-BEARING and stays. E6 made contracts
        # resolve their tag at approval, so this now fires only when
        # that genuinely could not be done — and in that case the
        # contract says why, which is appended rather than substituted:
        # a person who learned this sentence must still recognise it.
        note = ("the contract names an image by TAG, not a digest — the "
                "Verifier may not have run the image that was approved")
        why = parity.get("image_unpinned_because")
        notes.append(f"{note} ({why})" if why else note)
    if parity.get("env_skipped"):
        notes.append(
            f"{parity['env_skipped']} recorded install(s) could not be "
            f"reproduced from a clean base, so the checks ran without them")
    if parity.get("unrunnable"):
        notes.append(
            f"{parity['unrunnable']} of {parity.get('criteria', 0)} "
            f"criteria could not RUN. They did not judge the work, and a "
            f"verdict resting on the rest is a partial one")
    if parity.get("flaky"):
        notes.append(
            f"{parity['flaky']} criterion/criteria disagreed with "
            f"themselves across runs and were not counted as refusals")
    return notes


async def recent(conn) -> dict:
    """The aggregate a person watches for drift."""
    row = await conn.fetchrow("SELECT * FROM verdict_parity")
    return dict(row) if row else {}
