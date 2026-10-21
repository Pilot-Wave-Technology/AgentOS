"""F4 — the check surface, hashed at approval.

The third defence, and the one F2 shipped without. A criterion's command
comes from the contract, so a rewritten Make target is inert — but the
FILES the command reads are still in the tree the agent was working in.
Delete the assertions from a test and `pytest` exits 0 having checked
nothing, which F2's own suite records as an attack it cannot see.

    check_surface = ["test_*.py", "tests/**", "conftest.py",
                     "pyproject.toml", "Makefile"]

Hashed at APPROVAL over the tree as it was then. Recomputed at
verification over the committed tree. A difference means the thing being
checked has edited the check, and the approval no longer applies to it.

WHY GLOBS AND NOT A DERIVED SET. A surface derived from the test's
import graph would be tighter and is the shape the plan reserves the
right to move to (Still-open item 6). Declared globs are what ships
first because the failure modes are what need discovering, and they need
a real repository to discover:

  TOO NARROW — something outside the surface changes behaviour and a
  hack slips through.

  TOO BROAD — unrelated edits invalidate approvals until people widen
  the surface to make the noise stop. This is EROSION rather than
  failure: it looks like a working system right up until it catches
  nothing.

F7 points this at a real tree and inspects for both. Until then the
mechanism is honest and its calibration is unproven — which is a
different thing from a mechanism that does not exist.

A CONTRACT WITH NO SURFACE DECLARED IS NOT PROTECTED, and says so rather
than defaulting to something plausible. Guessing that `test_*.py` is the
surface would be this module deciding what the checks are, and a wrong
guess in the narrow direction is invisible.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging

from kernel.verifier.tarstream import digest_of, opened

logger = logging.getLogger(__name__)

CODE_SURFACE_CHANGED = "CHECK_SURFACE_CHANGED"


def declared(contract: dict) -> list[str]:
    """The globs this contract's check surface is made of."""
    spec = contract.get("workspace_spec") or {}
    return [g for g in (spec.get("check_surface") or []) if g]


def _matches(path: str, globs: list[str]) -> bool:
    # `**` in a declared glob means "and everything under it", which
    # fnmatch does not do on its own: `tests/**` would miss `tests/a.py`.
    for glob in globs:
        if fnmatch.fnmatch(path, glob):
            return True
        if glob.endswith("/**") and path.startswith(glob[:-2]):
            return True
        if glob.endswith("**") and path.startswith(glob[:-2]):
            return True
    return False


def hash_tar(source, globs: list[str], prefix: str = "",
             ignore: set | None = None) -> str:
    """The surface's hash over a tar of the tree. Empty globs -> "".

    `source` is a path or bytes. THE BODIES ARE NEVER HELD: each
    matched file collapses to a 32-byte digest as it streams past,
    and only those are sorted. The list used to carry every matched
    file's whole CONTENT so it could be sorted before hashing -- and
    a surface of `tests/**` over a monorepo IS the tree, so the
    defence against a rewritten test was itself a way to run the
    conductor out of memory. E5, plan 9.6.

    `ignore` drops paths that are in the tar but were NOT in the
    repository — F4's held-out checks, injected by the Verifier. They
    match `test_*.py` like any other test and did not exist when the
    surface was approved, so counting them makes the hash differ on
    every verification and the two defences cancel each other out.

    Found by the trap suite: the held-out attack reported the tree as
    tampered with, on a commit nobody had touched.

    Sorted by path before hashing, because a tar's member order is not
    stable across git versions and a hash that changes with the archiver
    would refuse every claim on the day somebody upgrades git.

    The path is hashed alongside the content: without it, RENAMING a
    test file to something the globs do not match — deleting it from the
    surface — leaves the hash unchanged.
    """
    if not globs:
        return ""
    digest = hashlib.sha256()
    entries = []
    with opened(source) as tf:
        for member in tf:
            if not member.isfile():
                continue
            path = member.name[len(prefix):] if prefix else member.name
            if ignore and path in ignore:
                continue
            if not _matches(path, globs):
                continue
            handle = tf.extractfile(member)
            entries.append((path, digest_of(handle, hashlib.sha256())
                            if handle else hashlib.sha256(b"").digest()))
    for path, body in sorted(entries):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
    logger.info("check surface: %d file(s) over %d glob(s)",
                len(entries), len(globs))
    return f"sha256:{digest.hexdigest()}"


def surface_files(source, globs: list[str],
                  prefix: str = "") -> list[str]:
    """Which files the surface actually covered. For F7's inspection.

    The calibration question — too narrow or too broad — is answered by
    looking at this list against a real repository, so it is a function
    rather than a log line.
    """
    if not globs:
        return []
    found = []
    with opened(source) as tf:
        for member in tf:
            if not member.isfile():
                continue
            path = member.name[len(prefix):] if prefix else member.name
            if _matches(path, globs):
                found.append(path)
    return sorted(found)


def check(contract: dict, source, prefix: str = "",
          ignore: set | None = None):
    """Has the check surface changed since approval? Returns None or a reason.

    Both halves must be present to protect anything. A contract that
    declares globs but was never hashed, or carries a hash but declares
    no globs, is reported as UNPROTECTED rather than silently passing —
    a defence that quietly does nothing is worse than one that is absent,
    because the absent one is visible.
    """
    globs = declared(contract)
    approved = contract.get("check_surface_hash")
    if not globs or not approved:
        return {"protected": False,
                "reason": ("no check surface is declared for this contract"
                           if not globs else
                           "a check surface is declared but was never "
                           "hashed at approval")}
    current = hash_tar(source, globs, prefix=prefix, ignore=ignore)
    if current == approved:
        return None
    return {"protected": True, "reason": "the check surface has changed "
                                         "since this contract was approved",
            "approved": approved, "current": current}
