"""Ec2d — the `artifact` criterion: done means the deliverable exists.

WHY A DIFFERENT KIND AT ALL. Every criterion so far is a `command`: a shell
line run in a fresh container against the committed tree, judged by its exit
code. That is the right shape when the deliverable IS the tree — a test
passes or it does not. It is the wrong shape for an errand, whose tree is
discarded and whose deliverable is a report. There is no command that proves
a report was written, only commands that inspect a workspace nobody keeps.

So the check for an errand is the one the user named: **the artifacts
produced**. `artifact[role=report]` says this run must end holding a report,
and that is a question about `kernel.artifact_manifest`, not about a tree.

NO CONTAINER, NO TREE, NO DRIVER — and that is the point, not a shortcut. It
needs the database, which the Verifier loop does not have and should not
grow: `verify.py` builds a container at the contract's digest and hands it a
tarball, deliberately isolated from everything stateful. Threading a
connection through it for one kind would put the Verifier's clean seam in
the way of an errand that has no Verifier at all.

It is therefore evaluated on the WORKER, at `propose_completion`, where the
connection already is. The registry entry in `kinds.py` stays, saying so, in
case a story contract ever names one — an `error` that explains where it is
evaluated beats a silent pass.

SCOPED TO THIS EXECUTION. A criterion asks whether THIS run produced the
thing. Widening it to the goal would let a run pass on last week's report,
which is exactly the "already done" false green the corpus work spent four
attempts on.
"""

from __future__ import annotations

import logging

from kernel.verifier.outcome import FAIL, PASS, Outcome, unrunnable

logger = logging.getLogger(__name__)

# What an artifact criterion may constrain. Deliberately three: the point
# is "the deliverable exists", not a query language. A criterion nobody can
# read is a criterion nobody checks.
SPEC_KEYS = ("role", "name", "min_bytes")

ROLES = ("report", "dataset", "method", "log", "figure")


def describe(spec: dict) -> str:
    """`artifact[role=report]` — the plan's own notation, back out again."""
    inner = ",".join(f"{k}={spec[k]}" for k in SPEC_KEYS if spec.get(k))
    return f"artifact[{inner}]" if inner else "artifact[any]"


def parse(text: str):
    """`artifact[role=report,name=march.md]` -> a spec dict.

    Returns None when the text is not an artifact check at all, and a dict
    — possibly empty — when it is. THE DISTINCTION MATTERS: a bare
    `artifact` with no constraints is a badly written artifact check, and
    an empty dict conflated with "not one" would send it to a container to
    be shelled out, where it comes back `command not found`. An answer
    about the image, dressed as an answer about the check.

    Accepted as a string because a person amending a story types into a
    textarea and a model writing a plan copies the notation it was shown.
    Refusing it would mean the notation in the prompt is not the notation
    that works.
    """
    body = str(text or "").strip()
    if not body.lower().startswith("artifact"):
        return None
    body = body[len("artifact"):].strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    elif body:
        # `artifactory --version` starts with the word and is a command.
        return None
    spec: dict = {}
    for part in body.split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if sep and key:
            spec[key] = value
    return spec


def problems(spec: dict) -> list[str]:
    """What is wrong with this spec, so it is refused at REGISTRATION.

    A criterion that can never match is worse than none: the run does the
    work, proposes completion, and is refused by a check that was broken
    when it was written. The plan is where somebody can still fix it.
    """
    out = []
    if not any(spec.get(k) for k in SPEC_KEYS):
        # Satisfied by any artifact at all, including a stray log. A check
        # that cannot distinguish done from not-done is not a check.
        out.append(f"an artifact check must constrain at least one of "
                   f"{', '.join(SPEC_KEYS)} — as written it passes on any "
                   f"artifact, including one you did not mean")
    unknown = sorted(set(spec) - set(SPEC_KEYS))
    if unknown:
        out.append(f"unknown key(s) {', '.join(unknown)}; "
                   f"an artifact check constrains {', '.join(SPEC_KEYS)}")
    role = str(spec.get("role") or "").lower()
    if role and role not in ROLES:
        out.append(f"role {role!r} is not one of {', '.join(ROLES)}")
    if spec.get("min_bytes"):
        try:
            int(spec["min_bytes"])
        except (TypeError, ValueError):
            out.append(f"min_bytes {spec['min_bytes']!r} is not a number")
    return out


def matches(spec: dict, row) -> bool:
    """Does one manifest row satisfy this spec?

    Name is compared case-insensitively and exactly. Not a glob: a check
    that matches `report*` passes on `report-draft-DO-NOT-USE.md`, and an
    executor picking its own filenames should not be able to widen its own
    check by naming things loosely.
    """
    role = str(spec.get("role") or "").lower()
    if role and str(row["role"]).lower() != role:
        return False
    name = str(spec.get("name") or "").strip().lower()
    if name and str(row["name"]).strip().lower() != name:
        return False
    # NON-EMPTY BY DEFAULT, not by remembering. `min_bytes` was opt-in,
    # so a check declared as `{expect: artifact, role: report}` passed on
    # a ZERO-BYTE report -- the deliverable exists, the criterion is
    # green, and nothing was delivered. An author should not have to
    # think of `min_bytes: 1` to get that; a file with no bytes in it
    # satisfies nothing.
    #
    # STILL OVERRIDABLE, downwards included: a spec that genuinely wants
    # to accept an empty file says `min_bytes: 0` and means it.
    floor = spec.get("min_bytes")
    floor = 1 if floor is None else int(floor)
    if floor and int(row["bytes"] or 0) < floor:
        return False
    return True


async def evaluate(conn, execution_id: int, criterion: dict) -> Outcome:
    """PASS if this execution emitted a matching artifact, else FAIL.

    FAIL rather than ERROR when nothing matches: the check RAN and said no.
    R1 reserves `error` for a check that could not say anything, and "the
    deliverable is not there" is very much something.
    """
    spec = dict((criterion or {}).get("spec") or {})
    spec.pop("run", None)               # a command's field, harmless here
    wrong = problems(spec)
    if wrong:
        return unrunnable("; ".join(wrong), kind="artifact",
                          criterion_id=(criterion or {}).get("id"))

    rows = await conn.fetch(
        "SELECT role, name, bytes, artifact_id, summary "
        "FROM kernel.artifact_manifest WHERE execution_id = $1", execution_id)
    hits = [r for r in rows if matches(spec, r)]
    wanted = describe(spec)

    if hits:
        # A ROW IS A CLAIM, NOT A DELIVERABLE. This used to pass here.
        # Between 2026-09-18 and 2026-09-20 every artifact on the
        # cluster was registered and unreadable -- the leg put its bytes
        # on a PVC that died with the pod -- and three stories reported
        # "satisfied by 1 artifact(s)" for objects nobody could open.
        # Asking the store is the difference between a catalogue and a
        # promise.
        from kernel.verifier import artifact_reachable

        try:
            gone = await artifact_reachable.missing(
                [r["artifact_id"] for r in hits], execution_id)
        except artifact_reachable.Unanswerable as exc:
            # ERROR, NOT FAIL. A store that cannot be reached says
            # nothing about the work, and refusing a delivered story
            # over an outage is a worse bug than the one above.
            return unrunnable(
                f"{wanted} matched {len(hits)} manifest row(s), but "
                f"whether the bytes are retrievable could not be "
                f"established: {exc}",
                kind="artifact", criterion_id=(criterion or {}).get("id"))
        readable = [r for r in hits if r["artifact_id"] not in set(gone)]
        if readable:
            return Outcome(PASS, {
                "reason": f"{wanted} satisfied by {len(readable)} "
                          f"artifact(s)",
                "check": wanted,
                "artifacts": [{"id": r["artifact_id"], "role": r["role"],
                               "name": r["name"], "bytes": r["bytes"]}
                              for r in readable]})
        # MATCHED AND UNREADABLE. Not "nothing matched": the executor
        # needs to know its emit did not survive, which is a different
        # thing to do next than emitting a differently named file.
        lost = ", ".join(f"{r['role']}:{r['name']}" for r in hits)
        return Outcome(FAIL, {
            "reason": f"{wanted} matched {len(hits)} artifact(s) that "
                      f"the store cannot produce ({lost}); the bytes "
                      f"were not durably stored, so nothing was "
                      f"delivered",
            "check": wanted, "unreadable": len(hits)})

    # NAMES WHAT IS THERE. "no matching artifact" leaves the executor
    # guessing whether it emitted the wrong role, the wrong name, or
    # nothing at all — and it has one leg left to work it out.
    held = ", ".join(f"{r['role']}:{r['name']}" for r in rows) or "nothing"
    return Outcome(FAIL, {
        "reason": f"{wanted} is not satisfied; this run emitted {held}",
        "check": wanted, "emitted": len(rows)})
