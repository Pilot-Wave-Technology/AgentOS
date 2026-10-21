"""G1 — a clean checkout of the fixture repo, per task run.

Every run starts from the same seed commit. Not a nicety: the corpus
exists to compare runs, and two runs that started from different trees
are not comparable — which is the same argument the Verifier makes for
archiving a commit rather than reading a working directory.

The seed is built here rather than checked in as a `.git` directory,
because a committed `.git` is a submodule as far as the outer repository
is concerned, and the one thing worse than no fixture is a fixture git
refuses to clone.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Fixed, so the seed commit is the same on every machine and in every
# run. A timestamp here would make the commit hash vary and every
# "unchanged" comparison meaningless.
SEED_ENV = {
    "GIT_AUTHOR_NAME": "corpus",
    "GIT_AUTHOR_EMAIL": "corpus@agentos.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "corpus",
    "GIT_COMMITTER_EMAIL": "corpus@agentos.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


class WorkspaceError(Exception):
    """The checkout could not be made, or the patch would not apply."""


def _git(*args, cwd: str, check: bool = True):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={**os.environ, **SEED_ENV})
    if check and result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{(result.stderr or result.stdout).strip()[:400]}")
    return result


def checkout(source_repo: str, into: str) -> str:
    """Copy the fixture repo to `into` and commit it. Returns the commit.

    A fresh directory every time. Reusing one would let a previous run's
    leftovers into the next, and the failure that produces looks like a
    model problem for a long time before anyone suspects the harness.
    """
    if os.path.exists(into):
        shutil.rmtree(into)
    shutil.copytree(source_repo, into,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".pytest_cache", ".git"))
    _git("init", "-q", cwd=into)
    _git("add", "-A", cwd=into)
    _git("commit", "-q", "-m", "seed", cwd=into)
    return _git("rev-parse", "HEAD", cwd=into).stdout.strip()


def apply_solution(workspace: str, patch_text: str,
                   message: str = "reference solution") -> str:
    """Apply a task's known-correct diff and commit it. Returns the commit.

    `git apply` and not `patch`: it fails loudly on a diff that does not
    match, and a solution that has silently rotted against the fixture is
    exactly what the validator exists to catch.
    """
    patch_file = os.path.join(workspace, ".solution.patch")
    with open(patch_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(patch_text)
    try:
        _git("apply", "--whitespace=nowarn", ".solution.patch", cwd=workspace)
    finally:
        os.remove(patch_file)
    _git("add", "-A", cwd=workspace)
    _git("commit", "-q", "-m", message, cwd=workspace)
    return _git("rev-parse", "HEAD", cwd=workspace).stdout.strip()


def head(workspace: str) -> str:
    return _git("rev-parse", "HEAD", cwd=workspace).stdout.strip()


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
