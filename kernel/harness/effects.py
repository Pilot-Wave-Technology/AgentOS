"""H2 — what a tool call can DO to the world, and who may allow it.

THREE CLASSES, and the boundary that matters is the third one.

  contained     nothing outside the workspace changes. Reads, searches,
                edits under the writable scope. The overwhelming majority
                of calls, and the reason a policy engine must be cheap.
  reversible    something outside the workspace changes and can be put
                back — a branch pushed, a file written to a shared path.
  irreversible  something outside the workspace changes and CANNOT be
                put back. A production write, a delete, a message sent, a
                payment. Approval is a person, every time.

`irreversible` CANNOT BE LOOSENED IN ANY CONFIGURATION. Not by a profile,
not by an environment variable, not by a contract. That is the whole
point of the class existing: a setting that can downgrade it is a setting
somebody sets during an incident and never sets back, and the failure is
unrecoverable by construction.

CLASSIFICATION IS BY TOOL AND ARGUMENTS, not by the model's say-so. A
model that could declare its own call `contained` would be the entire
attack surface. It never sees this vocabulary.

WHAT `unknown` MEANS. A tool nobody has classified is treated as
`irreversible` — the conservative direction, and the one that makes
adding a tool without thinking about its effects fail loudly rather than
quietly widen what a run can do. G4's error taxonomy learned the same
lesson: an undefined code must not silently become one.
"""

from __future__ import annotations

from kernel.tools.catalog import tools_by_effect

CONTAINED = "contained"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"

# ── THE THREE SETS ARE READ OFF THE CATALOG, and this is the fix for the
# bug that produced this file's history.
#
# They were three literals here, keyed by tool name, kept level with a
# catalog in another file by hand. FIVE tools shipped without a class.
# Each omission classified the tool `unknown`, therefore irreversible,
# therefore denied at chain stage 3 on every profile offering it — a
# failure that surfaces a long way from the mistake and reads as policy:
#
#   ask_human, register_plan  ASKING IS NOT AN EFFECT. They record a
#       question or a plan and change nothing outside the workspace. The
#       tool whose entire purpose is to REACH a person needed a person's
#       approval to run, which is the deadlock this class system exists
#       to avoid rather than create.
#   propose_completion  Missed when those two were fixed. It writes a
#       proposal saying the work looks done; the Conductor decides. So an
#       executor could do the work and never be allowed to say so.
#   env_setup  Applies the manifest the repo already commits, in the same
#       disposable container as env_install — so the tool an executor must
#       call before it can run a single test was denied as irreversible.
#   goal_knowledge  READS approved decisions for the goal, strictly less
#       reaching than `read`. It shipped on ALL THREE profile surfaces
#       unclassified.
#
# The guard written after the third omission did not catch the fifth. Its
# logic was right; it could not RUN — `test_effects_cover_profiles.py`
# hardcoded `/app/profiles`, so it only executed inside one container
# target. A check that cannot run did not prevent the thing it was written
# to prevent, which is a sharper lesson than the omission itself.
#
# So the sets are no longer a second place to remember. `effect=` is
# declared beside the schema, `_schema` rejects an unknown value at import,
# and a tool cannot be half-written: there is no state in which the model
# is shown a tool that has no class.
#
# The MEANINGS still live here, because they are this module's argument:
#   contained    read, list_dir, glob, grep, artifact_read, skill, write,
#                edit, bash_output, bash_kill and the four above
#   reversible   story_commit (local until something pushes it),
#                env_install (recorded, disposable container), env_setup,
#                web_fetch (reads the world, changes nothing in it),
#                publish_artifact (a NEW version with the old retained, so
#                undoable by republishing — not contained, it durably
#                reaches outside; not irreversible, nothing is lost)
#   by_argument  bash, bash_background, subagent. Deliberately NOT
#                contained: `bash` can do anything, and classifying it by
#                name would classify `rm -rf /` as contained.
_CONTAINED = tools_by_effect(CONTAINED)
_REVERSIBLE = tools_by_effect(REVERSIBLE)
_BY_ARGUMENT = tools_by_effect("by_argument")

# Substrings in a shell command that make it irreversible. Narrow on
# purpose: an over-eager list turns every run into an approval queue and
# gets switched off, which is worse than not having one.
# Ec3a — MOVED, AND WIDENED. This was sixteen substrings matched against a
# lowercased command. They caught the classic ways to destroy a MACHINE and
# missed every way to destroy an ACCOUNT: `terraform apply`, `aws s3 rm`,
# `gcloud … delete`, `helm uninstall`, `gh pr merge` all classified
# `reversible` and ran silently — and those are exactly what an errand with
# a cloud credential reaches for.
#
# A longer substring list was the wrong answer. A substring cannot tell
# `terraform apply` from `echo "terraform apply"`, or `truncate table t`
# from `truncate --help`, and every false positive spends a person's
# attention on nothing until they approve without reading. `destructive.py`
# matches TOKENS per simple command, after wrappers and flags are stripped,
# and returns the REASON so the card says what the call does.
from kernel.harness.destructive import reason as _destructive_reason


def classify(tool: str, args: dict | None = None) -> str:
    """The effect class of one call. Never asks the model."""
    args = args or {}
    if tool in _CONTAINED:
        return CONTAINED
    if tool in _REVERSIBLE:
        return REVERSIBLE
    if tool in _BY_ARGUMENT:
        return _classify_by_argument(tool, args)
    # UNKNOWN IS IRREVERSIBLE. A tool added without a class must fail
    # loudly rather than quietly widen what a run may do.
    return IRREVERSIBLE


def _classify_by_argument(tool: str, args: dict) -> str:
    if tool == "subagent":
        # A child's calls are classified when the CHILD makes them, at
        # the child's own chain. What matters at the SPAWN is how much
        # the child could do at all, which the flavour fixes.
        #
        # THE PREVIOUS VERSION TESTED `flavour == "effect"`, WHICH NO
        # VALID CALL CAN PRODUCE. The enum is `{investigate, build}` in
        # both the catalog and `attenuate.FLAVOURS`, so the branch was
        # unreachable and every spawn — including a `build` child that
        # keeps `bash` — classified `contained`. A test asserted the dead
        # branch worked, which is why it survived.
        #
        # Classified by what attenuation actually leaves the child:
        #   investigate — no shell, no write, no commit. Contained, and
        #                 `attenuate` enforces it by SURFACE, not scope.
        #   build       — keeps `bash`, so it reaches as far as `bash`
        #                 does. Reversible, matching how a non-destructive
        #                 shell command is classified two lines below.
        #
        # Neither is irreversible, so this changes no approval outcome
        # today; it stops the classifier lying about what it inspected.
        return CONTAINED if args.get("flavour") == "investigate" else REVERSIBLE
    if _destructive_reason(args.get("command", "")):
        return IRREVERSIBLE
    # A shell command that is not obviously destructive is still not
    # contained: it can write anywhere the process can reach. Reversible
    # is the honest middle, and it is what makes the policy configurable
    # without making `irreversible` configurable.
    return REVERSIBLE


def requires_approval(effect: str, policy: dict | None = None) -> bool:
    """Does this class need a person, under this policy?

    `irreversible` ALWAYS returns True and the policy cannot say
    otherwise. The check is written as an early return rather than as a
    lookup with a default, because a lookup is one typo away from being
    overridable.
    """
    if effect == IRREVERSIBLE:
        return True
    policy = policy or {}
    if effect == REVERSIBLE:
        return bool(policy.get("approve_reversible", False))
    return bool(policy.get("approve_contained", False))
