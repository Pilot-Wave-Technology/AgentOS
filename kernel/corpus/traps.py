"""G2 — tasks designed to go wrong in one specific way.

A graded task asks *did the executor do the work*. A trap asks something
else entirely: *when this goes wrong, does the mechanism built for it
actually fire?* G1's corpus cannot answer that, because every task in it
is satisfiable and the reference solution proves so.

EACH TRAP NAMES ITS MECHANISM. Not "this is hard" — a specific piece of
the harness, built in a specific increment, that this exists to
exercise. A trap with no named mechanism is a task nobody can grade,
because there is no statement to be false.

    missing-file          the ask path (E6h) — three attempts, then park,
                          rather than inventing a plausible file
    ambiguous-rounding    the ask path again, for a different reason:
                          nothing is missing, the requirement has two
                          defensible readings
    flaky-test            F3's quorum — `flake`, never `fail`, so a coin
                          flip cannot refuse a finished story
    two-leg               the leg boundary and the brief (E5c) — a
                          read-set that does not fit one leg
    fix-breaks-another    the second failure is REPORTED, never silently
                          traded for the first (Fb2)

`observable` IS PART OF THE SPEC AND IS NOT AN EXCUSE. Two of the five
are decidable from the database and a container; three need a real model
in front of them, because what is being tested is a CHOICE — whether the
executor asks instead of guessing — and a harness cannot make that
choice on its behalf. Those are exercised at Fb.

The suite says out loud which ones it did not run. A trap suite that
reports five greens when it exercised two is worse than no trap suite:
it is the false-green shape F4's first pass produced, and the reason
every gate in this project now carries a red-check.

EVERY TRAP IS ARM-CHECKED, including the ones needing a model. A trap
that is not armed — the missing file quietly added, the ambiguous prompt
clarified by a later edit — is a trap that fires on nothing, and it
would sit in the suite green for ever. The arm check is deterministic
even when the trigger is not, and it is the half that rots.
"""

from __future__ import annotations

import io
import json
import logging
import os

from kernel.corpus.load import CORPUS_ROOT, CorpusError

logger = logging.getLogger(__name__)

OBSERVABLE = ("deterministic", "needs_model")
REQUIRED = ("id", "title", "mechanism", "observable", "why")


def traps_root(root: str | None = None) -> str:
    return os.path.join(root or CORPUS_ROOT, "traps")


def trap_ids(root: str | None = None) -> list[str]:
    where = traps_root(root)
    if not os.path.isdir(where):
        return []
    return sorted(
        d for d in os.listdir(where)
        if os.path.isdir(os.path.join(where, d)) and not d.startswith("."))


def load_trap(trap_id: str, root: str | None = None) -> dict:
    where = os.path.join(traps_root(root), trap_id)
    spec_path = os.path.join(where, "trap.json")
    if not os.path.exists(spec_path):
        raise CorpusError(f"{trap_id}: no trap.json")
    try:
        spec = json.loads(io.open(spec_path, encoding="utf-8").read())
    except json.JSONDecodeError as e:
        raise CorpusError(f"{trap_id}: trap.json is not JSON — {e}") from None

    for key in REQUIRED:
        if not str(spec.get(key, "")).strip():
            raise CorpusError(f"{trap_id}: trap.json has no {key!r}")
    if spec["id"] != trap_id:
        raise CorpusError(f"{trap_id}: trap.json says id={spec['id']!r}")
    if spec["observable"] not in OBSERVABLE:
        raise CorpusError(
            f"{trap_id}: observable is {spec['observable']!r}; expected one "
            f"of {OBSERVABLE}. A trap whose trigger nobody can observe is a "
            f"trap that reports green having fired on nothing.")

    prompt_path = os.path.join(where, "PROMPT.md")
    if not os.path.exists(prompt_path):
        raise CorpusError(f"{trap_id}: no PROMPT.md")

    spec["prompt"] = io.open(prompt_path, encoding="utf-8").read()
    spec["dir"] = where
    for name, key in (("break.patch", "break_patch"),
                      ("setup.patch", "setup_patch")):
        path = os.path.join(where, name)
        spec[key] = (io.open(path, encoding="utf-8").read()
                     if os.path.exists(path) else None)
    return spec


def load_all(root: str | None = None) -> list[dict]:
    return [load_trap(t, root) for t in trap_ids(root)]


def by_observability(traps) -> dict:
    out = {k: [] for k in OBSERVABLE}
    for t in traps:
        out[t["observable"]].append(t)
    return out
