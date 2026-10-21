"""Trunk moved. The plan for catching up is always the same plan.

THE PROBLEM. A story's branch is cut from a trunk that then moves, and
it has to take those changes before it can merge. The plan gate is once
per cycle and the approved plan covered the FEATURE work, so the first
`sync_trunk` is refused -- and the executor would have to write, from
scratch, a plan that is word for word the same every time.

SO THE PLATFORM WRITES IT AND A PERSON APPROVES IT, ONCE. Nobody should
author these steps again, and nobody should have their branch
integrated without being asked.

ONE APPROVAL COVERS THE WHOLE CATCH-UP, CONFLICTS INCLUDED. The first
version approved only sync / check / emit and refused every write, so a
conflict parked the run for a second plan. On BMIF1 run 2 one moved
trunk cost FOUR approvals: the canned plan (which carried no checks, so
the executor wrote its own), a revision dropping a report step nobody
needed, and a conflict plan. A README conflict is not a decision worth
a person's interrupt; the person approving already said "bring this up
to date". So the scope now includes what resolving a conflict takes --
reading, editing, committing the merge -- and still nothing else: no
`bash`, no environment changes, no report. A check that FAILS after the
merge is different, it means the integration broke behaviour, and that
still stops for a person.

IT RUNS THE FULL SUITE, NOT ONLY THE STORY'S CHECKS. Integration is
exactly when the checks written for the feature can be too few: a
contract changed, a function renamed. The plan carries the story's own
command checks forward (approving a plan with checks REPLACES the
contract, so leaving them out would drop them) plus each merged
repository's whole suite (`suite_check`), which passes when the repo
has no tests.

THE SPLIT IS STILL MECHANICAL. `sync_trunk` reports conflicts per
mount, `run_check` reports pass or fail; nothing asks the executor to
judge whether to interrupt somebody.
"""

from __future__ import annotations

# What one approval of the canned plan authorises, and nothing else.
# Every other gated tool answers E_PLAN_NOT_APPROVED while it is live.
#
# `read`/`grep`/`glob`/`list_dir` are open before any plan and need no
# listing. `publish` is worker-side at the end of the leg, not a tool.
TOOL_SCOPE = frozenset({
    "sync_trunk",    # merge each lane in, worker-side, where the credential is
    "run_check",     # the carried story checks and each repo's suite
    "write",         # resolve a conflicted file
    "edit",          # resolve a conflicted file
    "story_commit",  # conclude the merge once markers are gone
})

STEPS = [
    "Call `sync_trunk` once. It merges every trunk lane this plan "
    "names into its own mount. Do not merge by hand with `bash`: the "
    "credential lives worker-side and a hand-rolled fetch "
    "authenticates as nobody.",
    "If it reports conflicts, resolve them in place, mount by mount: "
    "read each conflicted file, keep the intent of BOTH sides, remove "
    "every conflict marker, then `story_commit` to conclude the merge. "
    "Resolve every listed mount before running any check.",
    "Run every check with `run_check`: the story's own checks and the "
    "full test suite of each merged repository. A repository with no "
    "tests passes its suite check -- that is not a failure.",
    "If a check fails AFTER the merge, STOP and register a plan saying "
    "what broke. A check that passed before the merge and fails after "
    "it means the integration changed behaviour, and somebody has to "
    "decide what to do.",
    "When every check passes, `story_commit` anything still "
    "uncommitted -- this cycle's plan file is written into the tree for "
    "you -- and you are done; do not write a report. The branch is "
    "pushed and offered for merge again on its own at the end of the "
    "leg.",
]

RATIONALE = (
    "Trunk moved under this story's branch, so it has to take those "
    "changes before it can land. The platform writes these steps for "
    "you, but YOU approve them -- one approval for the whole catch-up, "
    "including resolving any merge conflict. It runs this story's own "
    "checks and every merged repository's full test suite; if you know "
    "something else should be run too, revise this plan and add it.")


def _targets_block(targets) -> str:
    lines = ["Lanes to take in:"]
    for t in targets:
        mount = t.get("mount") or "(no mount found -- not synced)"
        lines.append(f"- `{mount}` <- `{t['repo']}` `{t['lane']}` "
                     f"at {str(t['sha'])[:12]}")
    return "\n".join(lines)


def canned_plan(shas, lanes=(), *, targets=None, checks=None) -> dict:
    """The plan payload, naming every lane it is catching up to.

    EVERY LANE, NOT ONE, AND WHICH REPO EACH BELONGS TO. A story writing
    two repos -- or two lanes of one -- can be behind on all of them and
    they land as a group. `targets` pairs each lane with its repository,
    the sha it moved to and the mount it merges into, so the person
    approving sees exactly what will be taken in where; the bare
    `lanes`/`shas` lists could not say which sha belonged to which repo.
    """
    shas = list(shas or [])
    lanes = list(lanes or [])
    targets = list(targets or [])
    if targets:
        where = " on " + ", ".join(
            f"`{t['repo']}` `{t['lane']}` ({str(t['sha'])[:12]})"
            for t in targets)
        question = (f"Trunk moved{where}. Bringing this story's branch "
                    f"up to date before it lands.")
    else:
        lane_text = (" on " + ", ".join("`" + x + "`" for x in lanes)
                     if lanes else "")
        at = ", ".join(s[:12] for s in shas)
        question = (f"Trunk moved{lane_text} ({at}). Bringing this "
                    f"story's branch up to date before it lands.")
    steps = list(STEPS)
    if targets:
        steps.insert(0, _targets_block(targets))
    return {
        "question": question,
        "steps": steps,
        "rationale": RATIONALE,
        # The story's carried checks plus each merged repo's suite. Never
        # empty when the sweep built it: an empty list here would leave
        # the old contract in place and `run_check` measuring only the
        # feature's own checks, which is the gap this closes.
        "checks": list(checks or []),
        "tool_scope": sorted(TOOL_SCOPE),
        "trunk_shas": shas,
        "lanes": lanes,
        "targets": targets,
        # The flag says "the platform composed this", which is what lets
        # a reader tell a plan nobody wrote from one an executor did.
        "platform_canned": True,
    }


def scope_of(payload) -> frozenset | None:
    """The tool scope a plan payload declares, or None for an ordinary one.

    None and an empty set are DIFFERENT and the caller must keep them
    apart: None is "this plan scopes nothing", which is every plan a
    person wrote and means full authority once approved. An empty set
    would mean "approved for no tools at all", which no caller should
    ever produce and which would silently brick a run.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("tool_scope")
    if not raw:
        return None
    return frozenset(str(t) for t in raw)
