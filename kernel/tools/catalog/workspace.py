"""What survives a leg, and how a leg ends.

Worker-side, all of them, and for one reason: they need git,
the real workspace, the profile or the database, and the
sandbox holds none of those and must not.

`register_plan` and `propose_completion` are the two halves of
the plan-based shape — one asks a human to approve an approach
before anything changes, the other says the work looks done and
lets somebody else decide. Both are `contained`: asking is not
an effect, and classifying them otherwise deadlocked the run.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _I, _S, _schema

TOOLS: dict[str, dict] = {
    # ── Workspace ────────────────────────────────────────────────────────
    "story_commit": _schema(
        "story_commit",
        "Commit the current changes. Only committed work survives the end "
        "of a leg. Refuses an empty diff with exit code 1.",
        {"message": {"type": _S, "description": "What changed and why."}},
        ["message"],
        side="worker", effect="reversible",
    ),

    "submit_story_review": _schema(
        "submit_story_review",
        "Record your review and finish. Call this ONCE, as the last "
        "thing you do -- it ends the run. `approve` means everything "
        "with material churn was read: if `not_examined` is non-empty "
        "the verdict is recorded as `partial` instead, and the "
        "correction is written into the review. If you could not read "
        "the diff at all, that is `code_not_reviewed` and it is a "
        "legitimate answer -- an honest account of what you could not "
        "see is worth more than a guess.",
        {"verdict": {"type": _S,
                     "description": "approve | partial | revise | "
                                    "code_not_reviewed | incomplete"},
         "summary": {"type": _S,
                     "description": "A paragraph a person will read: "
                                    "what you read, what you concluded, "
                                    "why."},
         "coverage": {"type": _S,
                      "description": "Files read of files changed."},
         "not_examined": {"type": "array", "items": {"type": _S},
                          "description": "Churn you did not read."},
         "findings": {
             "type": "array",
             "items": {"type": "object", "properties": {
                 "repo": {"type": _S}, "file": {"type": _S},
                 "line": {"type": _I}, "severity": {"type": _S},
                 "claim": {"type": _S}, "why_it_matters": {"type": _S}},
                 "required": ["claim"]},
             "description": "What is wrong, each anchored where it is."},
         "plan_drift": {"type": _S,
                        "description": "Promises in an earlier cycle "
                                       "neither delivered nor withdrawn."},
         "cross_branch": {"type": _S,
                          "description": "A change in one repo needing a "
                                         "matching change in another."},
         "claim_check": {"type": _S,
                         "description": "Whether the completion claim's "
                                        "counts and baseline square with "
                                        "the diff."}},
        ["verdict", "summary"],
        side="worker", effect="contained",
    ),

    "sync_trunk": _schema(
        "sync_trunk",
        "Bring the trunk into your branch. Fetches the trunk lane and "
        "MERGES it into every writable repository you hold, then tells "
        "you what happened per repo: `already` (nothing to take), "
        "`merged` (done, commit it), or `conflict` with the paths. "
        "YOU CANNOT DO THIS WITH `bash`: fetching needs a git "
        "credential and your sandbox holds none -- that is deliberate, "
        "not a gap -- which is why this is a tool and not a command. "
        "Use it when your story is to take trunk's changes, and when a "
        "push is refused because the branch is behind. On a conflict "
        "the markers are left in the files for you to resolve the "
        "ordinary way, then `story_commit`. It MERGES and never "
        "rebases: your branch is already pushed and other things name "
        "its commits.",
        {},
        [],
        side="worker", effect="reversible",
    ),

    # ── Planning and completion ──────────────────────────────────────────
    "register_plan": _schema(
        "register_plan",
        "Submit a plan for approval. Work cannot start until a human "
        "approves it. Registering a plan parks the run.",
        {"steps": {"type": "array", "items": {"type": _S},
                   "description": "Ordered steps, each independently doable."},
         "rationale": {"type": _S, "description": "Why this shape."},
         "checks": {
             "type": "array",
             "items": {"type": "object", "properties": {
                 "run": {"type": _S,
                         "description":
                             "A shell command that decides whether this "
                             "is done. YOU run it, as part of the work, "
                             "and report what it said when you propose "
                             "completion — nothing re-runs it before a "
                             "person sees your claim. If the platform "
                             "has verification switched on it is ALSO "
                             "re-run in a clean container built from the "
                             "committed manifests, and you cannot tell "
                             "from in here whether that is on: write a "
                             "check that would pass in both, because "
                             "anything you installed by hand exists here "
                             "and not there. "
                             "START IT WITH `cd <mount> && ` whenever it "
                             "touches a repository — e.g. `cd "
                             "repos/owner__name && .venv/bin/pytest "
                             "tests`. That prefix is the directory the "
                             "check runs FROM, and a clean container also "
                             "reads it to decide which repo's interpreter "
                             "and tools go first on PATH; without it the "
                             "first mount alphabetically wins, which is "
                             "the wrong repository as soon as a story "
                             "touches two. Do not mix frames: "
                             "`.venv/bin/pytest repos/owner__name/tests` "
                             "is wrong because `.venv` is relative to the "
                             "mount and `repos/...` to the workspace root, "
                             "and no cwd makes both true. "
                             "A full test suite is a perfectly good "
                             "check; never trim one to be quick. Say "
                             "`timeout_s` if it needs more than twenty "
                             "minutes in a clean container."},
                 "timeout_s": {"type": _I,
                               "description":
                                   "Seconds this check gets when a "
                                   "Verifier runs it, if the 1200s default "
                                   "is not enough — up to two hours. "
                                   "Declare it rather than splitting a "
                                   "suite to fit. Note which way it cuts: "
                                   "a check that exhausts a budget YOU "
                                   "declared counts as a FAILURE, because "
                                   "you wrote the number down, while one "
                                   "that exhausts the default is only "
                                   "undecided."},
                 "label": {"type": _S, "enum": ["goal", "guard"]},
                 # Ec2d — the other shape a check can take. A `command`
                 # check is a shell line judged by its exit code, which is
                 # right when the deliverable IS the tree. When the
                 # deliverable is a report, no command proves it was
                 # written: the tree is discarded and the report lives in
                 # the manifest. So `expect` asks the manifest instead.
                 "expect": {"type": _S, "enum": ["artifact"],
                            "description": "Set to `artifact` instead of "
                                           "`run` when the check is that "
                                           "you produced something."},
                 "role": {"type": _S,
                          "enum": ["report", "dataset", "method", "log",
                                   "figure"],
                          "description": "With expect=artifact: which "
                                         "role must exist."},
                 "name": {"type": _S,
                          "description": "With expect=artifact: the exact "
                                         "artifact name, if it matters."},
                 "min_bytes": {"type": _I,
                               "description": "With expect=artifact: a "
                                              "floor, so an empty file "
                                              "does not pass."}},
                 "required": ["label"]},
             "description":
                 "How this work will be PROVEN done. You write these "
                 "because you are the only one who can see the tree — the "
                 "person asking knows what they want, not what command "
                 "runs it. Label each one: `goal` must FAIL against the "
                 "code as it is now and pass when you are finished, which "
                 "is what makes it evidence; `guard` passes now and must "
                 "keep passing. Command checks are run against the current "
                 "tree before anyone approves them, and a goal that "
                 "already passes is refused — it could never show the work "
                 "happened. If your deliverable is an artifact rather than "
                 "a change to the tree, use "
                 "{expect: \"artifact\", role: \"report\", label: \"goal\"} "
                 "— it is satisfied when you emit_artifact a matching one, "
                 "and by nothing else."},
         "decisions": {
             "type": "array",
             "items": {"type": "object", "properties": {
                 "id": {"type": _S,
                        "description": "Short, unique within this plan: d1, d2."},
                 "what": {"type": _S,
                          "description": "The choice, in one line — "
                                         "'FastAPI for the HTTP layer'."},
                 "why": {"type": _S, "description": "What made it the choice."},
                 "supersedes": {
                     "type": _S,
                     "description": "Address of an EARLIER decision this "
                                    "replaces, e.g. `cycle1:d3`. Only the "
                                    "ones you are actually changing — an "
                                    "earlier decision you leave alone stays "
                                    "live, and claiming otherwise makes the "
                                    "planner work around something that "
                                    "still stands. `goal_knowledge` lists "
                                    "the addresses."}},
                 "required": ["what"]},
             "description":
                 "The decisions this plan MAKES, named one by one — "
                 "frameworks, patterns, storage shape, anything a later "
                 "story would otherwise re-argue from scratch. `rationale` "
                 "is one paragraph about the whole plan; these are "
                 "addressable, so a person can cite one and a later plan "
                 "can supersede exactly the one it changes. They are "
                 "approved along with the plan, which is what makes them "
                 "evidence rather than opinion. If your plan chooses a "
                 "library, a pattern or a shape, say so here."}},
        ["steps", "checks"],
        side="worker", effect="contained",
    ),
    "propose_completion": _schema(
        "propose_completion",
        "Claim the work is done, with evidence. This does NOT end the "
        "run. RUN YOUR PLAN'S CHECKS YOURSELF FIRST and put what they "
        "said in `reason_set` -- nothing re-runs a command check before "
        "a person sees it, so your numbers are the evidence. Any "
        "ARTIFACT check is measured here from the harness's own record "
        "of what you emitted: if one is unmet the claim is not recorded "
        "at all, and you get the reason back as this tool's result. "
        "Then a PERSON decides whether the story is done or has more "
        "work. If more work arrives, it comes as instructions at your "
        "next step -- same story, same workspace, same branch: continue "
        "there, then propose completion again.",
        {"reason_set": {
            "type": "array", "items": {"type": _S},
            "description":
                "Evidence, one item per criterion. NUMBERS, NOT PROSE, "
                "wherever the criterion ran anything: give passed, "
                "failed, skipped and errors. 'the suite is green' and "
                "'41 passed, 0 failed, 29 skipped, 0 errors' are the "
                "same claim, and only one of them lets a person see "
                "that twenty-nine tests did not run. The skip count is "
                "the cheapest false-green detector there is. "
                "AND COMPARE IT TO THE BASELINE you took before you "
                "changed anything -- 'was 38/0/2/0, now 41/0/2/0'. "
                "'No regressions' needs a before, or it is an "
                "assertion rather than a measurement; and the baseline "
                "is also how you tell a test you broke from one that "
                "was already red when you arrived."},
         # What a PLAN cannot say, because it is only knowable afterwards.
         # You are the only party who knows any of it first-hand: the
         # assistant would be inferring from a diff and would sound just as
         # certain. All optional — a claim is still valid without them.
         "techniques": {
             "type": "array", "items": {"type": _S},
             "description": "Frameworks, libraries and patterns you actually "
                            "used. Names, not prose: 'fastapi', 'pytest'. "
                            "This is what stops the next story re-arguing a "
                            "settled choice."},
         "learned": {
             "type": "array",
             "items": {"type": "object", "properties": {
                 "fact": {"type": _S, "description": "What is true, in one line."},
                 "why_it_matters": {"type": _S,
                                    "description": "What it changes for "
                                                   "whoever comes next."}},
                 "required": ["fact"]},
             "description":
                 "Constraints and surprises the WORK revealed — 'the vendor "
                 "rate-limits at 5/s', 'the fixture loader ignores a trailing "
                 "newline'. Not what you did (the diff says that) and not "
                 "what you planned (the plan says that): what you now know "
                 "that nobody knew when the plan was written."},
         "abandoned": {
             "type": "array",
             "items": {"type": "object", "properties": {
                 "tried": {"type": _S, "description": "The approach."},
                 "why_not": {"type": _S, "description": "What stopped it."}},
                 "required": ["tried"]},
             "description":
                 "Approaches you tried and dropped, with the reason. The "
                 "most valuable and least recorded thing here — without it "
                 "the next story tries the same dead end and pays the same "
                 "price to find out."}},
        ["reason_set"],
        side="loop", effect="contained",
    ),

    "goal_knowledge": _schema(
        "goal_knowledge",
        "What this goal has already DECIDED and LEARNED. Read it before "
        "you choose a framework, a pattern or a shape — the repository "
        "may have settled the question two stories ago, and a plan that "
        "re-opens a settled choice costs a review cycle to find out. "
        "Decisions here come from APPROVED PLANS: a person signed them, "
        "so they are binding in a way your own preference is not. A "
        "decision no later plan superseded is still live. "
        "`about` takes a repo name, a resource id or a path.",
        {"about": {"type": _S,
                   "description": "A repo, resource id, path or phrase — "
                                  "'what do we know about this'."},
         "story": {"type": _S, "description": "One story's record."},
         "cycle": {"type": _I, "description": "Only this plan cycle."},
         "limit": {"type": _I, "description": "Default 25."}},
        [],
        side="worker", effect="contained",
    ),
}
