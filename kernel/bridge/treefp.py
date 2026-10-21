"""What the tree actually contained when a command ran.

WHY A FINGERPRINT AND NOT A COMMIT. A recorded check run is only worth
anything if "the tree has not changed since" can be answered honestly,
and HEAD cannot answer it. An executor runs its suite, edits three files
and does not commit -- HEAD is identical, the tree is not, and a check
keyed on HEAD would report a green tick over code that has never been
run. `mount_digest` in the Verifier is HEAD-only on purpose (a verdict
is keyed to a COMMIT), and that is the right key there and the wrong one
here.

SO: THE WORKING TREE, INCLUDING WHAT IS NOT COMMITTED. Git already has
the exact object for this -- the tree a commit WOULD have -- and the way
to get it without touching anything is a scratch index:

    cp .git/index $tmp          # keeps git's stat cache, so unchanged
    GIT_INDEX_FILE=$tmp git add -A       # files are not re-hashed
    GIT_INDEX_FILE=$tmp git write-tree

The real index, the working tree and HEAD are all untouched; the only
residue is loose objects for content git had not hashed yet, which are
unreachable and get collected. Copying the index rather than
`read-tree`ing a fresh one is the difference between re-hashing every
file in the repository and re-hashing the handful that changed.

THE SPILL DIRECTORY IS EXCLUDED BY NAME. A job's log grows inside the
workspace while the job runs, so a fingerprint that saw it would move on
every write and no run would ever still be current by the time it
finished. `jobspill.SPILL_DIR` is one constant for exactly this.

SANDBOX-SIDE, because this is the only side the tree is on. The worker
asks for it over the bridge like any other tool; nothing about it is
offered to the model, which has no use for a digest and every reason to
be unable to forge one.
"""

import errors
import jobspill
import os
import subprocess
import tempfile
import time

# ONE TOTAL BUDGET, NOT A PER-COMMAND ONE, and the difference is a real
# collision rather than tidiness.
#
# This used to allow 120 s PER GIT COMMAND. `tree_sha` runs three of
# them per repository and `fingerprint` walks every repository, so a
# two-repo workspace could spend twelve minutes here -- while the worker
# gives the bridge 180 s (`claim.take`) and, worse, while this call
# holds the shim's `_TOOL_LOCK`. The client would give up, the server
# thread would keep going, and the sandbox would answer 409 BUSY to
# everything for minutes with nobody waiting on the result.
#
# So the whole fingerprint is bounded once, comfortably under the
# caller's HTTP timeout, and the budget is spent DOWN as it goes. A
# fingerprint that cannot finish in time returns "" -- which every
# caller already reads as "no evidence is usable", the safe direction.
#
# 45 s is generous for what this does: `git add -A` against a copied
# index re-hashes only what changed, which is milliseconds on a warm
# tree. The budget is here to bound a pathological case, not a normal
# one.
TOTAL_BUDGET_S = 45.0

# The floor under any single command, so the last repository in a nearly
# exhausted budget still gets a real attempt rather than a 0-second one
# that is certain to fail.
MIN_COMMAND_S = 5.0

# How deep to look for repositories under the workspace root. The layout
# is `repos/<owner>__<name>`, so one level of nesting is the whole of
# it; the root itself is a repository in the single-repo case.
SCAN_DEPTH = 2


class _Budget:
    """What is left of the fingerprint's total wall clock."""

    __slots__ = ("deadline",)

    def __init__(self, total: float = TOTAL_BUDGET_S):
        self.deadline = time.monotonic() + total

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def spent(self) -> bool:
        return self.left() <= 0

    def slice_s(self) -> float:
        """What one command may have: the remainder, never below the
        floor -- a 0-second timeout is a guaranteed failure dressed as a
        measurement."""
        return max(MIN_COMMAND_S, self.left())


def _run(args, cwd, budget=None):
    """One git invocation. Returns stdout, or None if it failed at all."""
    budget = budget or _Budget()
    if budget.spent():
        return None
    try:
        done = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            timeout=budget.slice_s(), check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def git_roots(workspace: str) -> list:
    """Every repository in the workspace, as paths relative to it.

    The root is named by the empty string, matching the Verifier's
    `mount.at` convention so the two digests can be compared by eye.
    """
    found = []
    if os.path.isdir(os.path.join(workspace, ".git")):
        found.append("")
    stack = [(workspace, "", 0)]
    while stack:
        base, rel, depth = stack.pop()
        if depth >= SCAN_DEPTH:
            continue
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in (".git", jobspill.SPILL_DIR, "node_modules",
                              ".venv", "__pycache__"):
                continue
            here = f"{rel}/{entry.name}" if rel else entry.name
            if os.path.exists(os.path.join(entry.path, ".git")):
                found.append(here)
                continue        # a repo's submodules are its own business
            stack.append((entry.path, here, depth + 1))
    return sorted(set(found))


_EXCLUDES_PATH = ""

# WHAT A TEST RUN WRITES INTO THE TREE JUST BY RUNNING.
#
# THE DEFECT THIS CLOSES, measured on run 256: two `run_check` jobs, the
# same command, no source edit between them, and two different
# fingerprints --
#
#   job-a0dbe1b6  fc5aff69478f93
#   job-6e3361c7  a7ea566b33df69
#
# -- so A7 never found a prior run and the reuse path was unreachable
# for any Python repository. `jobspill` had anticipated exactly this for
# its own log directory ("a log file growing inside the tree while the
# job runs would move that fingerprint on every write ... A5 would
# defeat A7 entirely") and excluded one name; the caches every pytest
# invocation creates were not on the list.
#
# CACHES AND COVERAGE ONLY, DELIBERATELY NOT DEPENDENCY TREES. `.venv/`,
# `node_modules/` and `target/` are already outside the fingerprint on
# any repo whose setup ran, because the registry rows write them into
# `.git/info/exclude` before installing. Where a repo has NOT excluded
# them they are genuinely part of what a check would see, and a pip
# install between two runs is a real difference that should move the
# fingerprint. Excluding them here would tell A7 that two trees with
# different dependencies are the same tree, which is the one mistake
# this mechanism must not make.
_CACHE_PATTERNS = (
    "__pycache__/", "*.pyc", ".pytest_cache/", ".ruff_cache/",
    ".mypy_cache/", ".hypothesis/", ".tox/", ".nox/",
    ".coverage", ".coverage.*", "htmlcov/", ".nyc_output/",
)


def _excludes_file() -> str:
    """One throwaway ignore file: the spill directory and the caches.

    Made once per process and left in the temp directory: it is a few
    lines, it is read on every fingerprint, and a per-call file would be
    a create and a delete around every `git add`.
    """
    global _EXCLUDES_PATH
    if _EXCLUDES_PATH and os.path.exists(_EXCLUDES_PATH):
        return _EXCLUDES_PATH
    handle, path = tempfile.mkstemp(prefix="agentos-fpignore-")
    lines = (jobspill.SPILL_DIR + "/",) + _CACHE_PATTERNS
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(chr(10).join(lines) + chr(10))
    _EXCLUDES_PATH = path
    return path


def tree_sha(root: str, budget=None) -> str:
    """The tree this repository WOULD commit, working changes included.

    Empty string when the answer cannot be had -- not a repository, no
    git, a timeout. A missing fingerprint makes evidence unusable, which
    is the safe direction: the check simply runs again.
    """
    budget = budget or _Budget()
    git_dir = _run(["git", "rev-parse", "--absolute-git-dir"], root, budget)
    if not git_dir:
        return ""
    scratch = None
    try:
        handle, scratch = tempfile.mkstemp(prefix="agentos-fp-")
        os.close(handle)
        # AN EMPTY INDEX, NOT A COPY OF THE REAL ONE, and the difference
        # is correctness rather than speed.
        #
        # Copying `.git/index` carries git's STAT CACHE, so `git add -A`
        # re-hashes only what looks changed -- which is much faster and
        # WRONG HERE. The copy's own mtime is the moment of the copy, so
        # every working file appears older than the index and is trusted
        # clean on stat alone; git's racy-timestamp protection, which
        # exists for exactly this, never engages. An edit that keeps the
        # file's SIZE then never gets re-hashed and the fingerprint does
        # not move.
        #
        # `test_an_uncommitted_edit_moves_the_fingerprint` caught it in
        # CI: `x = 1` to `x = 2`, six bytes either way, same second --
        # and the fingerprint was identical. That is the green tick over
        # an unrun suite this whole mechanism exists to prevent, so the
        # stat cache goes.
        #
        # THE COST IS BOUNDED AND SMALL. `git add -A` now hashes the
        # working tree, but the tree here is SOURCE: `.venv`,
        # `node_modules`, `target` and the spill directory are all
        # excluded before this runs, and git hashes source at a rate
        # where a repository of this shape is well inside
        # `TOTAL_BUDGET_S`.
        os.unlink(scratch)
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = scratch
        # THE SPILL IS EXCLUDED AS AN IGNORE RULE, NOT AS A PATHSPEC.
        # `:(exclude).agentos-out` looks like the direct way to say this
        # and is not: git refuses to add a path it is also ignoring and
        # exits 1 with "the following paths are ignored", so the whole
        # fingerprint came back empty the moment `jobspill` had written
        # its `.git/info/exclude` line -- exactly the repositories where
        # it matters. A throwaway excludes file says the same thing
        # without naming the path to `add`, and leaves no trace in the
        # repository.
        excludes = _excludes_file()
        try:
            done = subprocess.run(
                ["git", "-c", f"core.excludesFile={excludes}",
                 "add", "-A", "--", "."],
                cwd=root, env=env, capture_output=True, text=True,
                timeout=budget.slice_s(), check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        if done.returncode != 0:
            return ""
        try:
            written = subprocess.run(
                ["git", "write-tree"], cwd=root, env=env,
                capture_output=True, text=True, timeout=budget.slice_s(),
                check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        if written.returncode != 0:
            return ""
        return written.stdout.strip()
    finally:
        if scratch and os.path.exists(scratch):
            try:
                os.unlink(scratch)
            except OSError:
                pass


def fingerprint(workspace: str) -> str:
    """The whole workspace as one comparable string.

    `at=sha;at=sha`, repositories in sorted order. A SET, for the reason
    `mount_digest` gives: with several repositories one sha names a
    fraction of what a check saw, and two workspaces agreeing on that
    fraction are not the same tree.

    Empty when no repository could be fingerprinted, which callers must
    read as "no evidence is usable here" rather than "nothing changed".
    """
    budget = _Budget()
    parts = []
    for at in git_roots(workspace):
        if budget.spent():
            return ""           # partial is worse than absent
        root = os.path.join(workspace, at) if at else workspace
        sha = tree_sha(root, budget)
        if not sha:
            return ""
        parts.append(f"{at}={sha}")
    return ";".join(parts)


def tool_tree_fingerprint(args: dict, workspace: str) -> dict:
    """The bridge surface. Not a model tool -- the worker asks.

    Answered in the ordinary envelope so it travels the one code path
    every other tool call uses, and carried in `fingerprint` rather than
    `output` so a caller reads a field instead of parsing prose.
    """
    return errors.ok(output="", fingerprint=fingerprint(workspace))
