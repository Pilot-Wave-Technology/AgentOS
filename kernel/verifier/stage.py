"""F2 — everything that has to be true BEFORE the first criterion runs.

    a commit  ->  the exact tree every criterion will be checked against

Lifted out of `verify.py` at E2, which had to split the file to add the
batched path and could not add a line to it first. NOTHING HERE
CHANGED: the ordering below is a sequence of measured incidents, each
recorded in the comment above the step that fixes it, and the split
moved the text rather than rewriting it.

THE ORDER IS THE CONTENT. Archive, then discover setups, then replay the
recorded installs, then hash the approved check surface, then prepare,
then hash it AGAIN. Each pair of neighbours is a decision:

    archive before anything      on a pod substrate `git` and `os.walk`
                                 SUCCEED against a claim this process
                                 cannot open, and produce an empty tar
    surface before prepare       the first hash must see the pristine
                                 tree
    surface again after prepare  a repo-authored setup that edits an
                                 approved test would pass the first
                                 hash and fail nothing else

`stage` returns `(Staged, None)` or `(None, results)`. The second is
already a complete answer for every criterion — one per criterion,
because "the contract names no base image" carries the criterion's own
id — and the caller returns it unchanged.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.verifier.environment import build_deps, rebase_deps_tar
from kernel.verifier.kinds import TREE_PREFIX, VERIFIER_UID
from kernel.verifier.outcome import FAIL, Outcome, unrunnable
from kernel.verifier.surface import CODE_SURFACE_CHANGED
from kernel.verifier.surface import check as surface_check
from kernel.verifier.tarslice import names
from kernel.verifier.tree import (
    TreeExportError, archive_committed_tree, archive_in_pod,
    archive_is_remote, held_out_files,
)

logger = logging.getLogger(__name__)


class Staged:
    """The tree the criteria will run against, and what it needs."""

    __slots__ = ("tar", "deps_tar", "mounts_env")

    def __init__(self, tar: bytes, deps_tar: bytes, mounts_env: dict):
        self.tar = tar
        self.deps_tar = deps_tar
        self.mounts_env = mounts_env


def _every(criteria: list[dict], outcome: Outcome):
    return [(c, outcome) for c in criteria]


async def _archive(driver, repo_path: str, commit: str, *, mounts, injected,
                   workspace_host: str, execution_id):
    # I4 — THE TREE IS TAKEN WHERE THE TREE IS. Checked before the
    # mount branch below, because both of those run `git` and
    # `os.walk` against paths that, on a pod substrate, name a claim
    # this process cannot open: they would not raise, they would
    # produce an empty tar and every criterion would fail for having
    # no code to check.
    # OFF THE EVENT LOOP, all three branches. Each shells out to
    # `git archive` once per mount and builds a tar of the result;
    # on a 1-CPU container that is tens of seconds of work, and run
    # inline it stops every other coroutine in the conductor -- the
    # admission sweep, the dispatch sweep, the heartbeats. Measured
    # on execution 203: two conductors frozen for 154s and 184s,
    # both killed by the liveness probe, while verification ran.
    # `deliver.py` already wraps its one `git rev-parse` for exactly
    # this reason; this module never did.
    if archive_is_remote(driver):
        return await asyncio.to_thread(
            archive_in_pod, workspace_host, repo_path, commit,
            mounts=mounts, prefix=TREE_PREFIX,
            uid=VERIFIER_UID, inject=injected,
            execution_id=execution_id)
    # E7f — several repositories are several trees, reassembled
    # here so a check means the same thing in the Verifier's
    # container as it did in the sandbox.
    if mounts and any(m.at for m in mounts):
        from kernel.verifier.tree import archive_mounts  # noqa: PLC0415
        return await asyncio.to_thread(
            archive_mounts, repo_path, mounts, prefix=TREE_PREFIX,
            uid=VERIFIER_UID, inject=injected)
    return await asyncio.to_thread(
        archive_committed_tree,
        repo_path, commit, prefix=TREE_PREFIX, uid=VERIFIER_UID,
        # F4's fourth defence. The held-out checks live in the
        # criteria that own them, never in the repository, and
        # are written into the Verifier's copy here — so there is
        # nothing for the executor to read, tune against, or
        # notice the absence of.
        inject=injected)


async def _replay(driver, contract, criteria, commit, env_deltas, timeout,
                  report, execution_id):
    """F5: the executor's recorded installs, replayed from the base image.

    ONCE for the whole claim, in a networked container that runs no
    criterion and is given no tree — so the criteria still have no
    network and still cannot fetch a version of themselves that passes.

    A replay that fails is `error` for every criterion, never `fail`: a
    check that could not be given its dependencies did not run, so it
    found nothing wrong.
    """
    raw, env_report = await build_deps(
        driver, contract.get("base_digest") or contract.get("base_image"),
        env_deltas, timeout=timeout,
        # AND THE ID TRAVELS WITH IT. This spawns a capture Job of
        # its own, and a capture Job that cannot name its execution
        # is one the conductor's orphan sweep reaps mid-install --
        # measured on execution 208, where it read as flake because
        # only an overlapping reconcile tick bites.
        execution_id=execution_id)
    if report is not None:
        report.update(env_report)
    if not env_report.get("ok"):
        return b"", _every(criteria, unrunnable(
            "the executor's recorded environment could not be reproduced",
            commit=commit, install=env_report.get("output", "")[-800:]))
    if env_report.get("skipped"):
        # F6 counts these. Said out loud so a divergence is never
        # silent — a delta that vanishes is one nobody can see.
        logger.warning("verify: %d env delta(s) could not be "
                       "reproduced: %s", len(env_report["skipped"]),
                       [d["manager"] for d in env_report["skipped"]])
    return rebase_deps_tar(raw), None


def _surface(contract, tar, criteria, commit, injected, ignore=frozenset(),
             again=False):
    """F4: has the thing being checked edited the check?

    Before any container. A surface that changed means the approval was
    given for a different set of checks, and running them would be
    asking the agent's own tests whether the agent's work is good.

    Refused as `fail`, not `error`: unlike a container that would not
    start, this DID find something wrong, and it is the one thing the
    agent can fix by putting the checks back.

    The injected held-out checks are NOT part of the approved surface —
    they did not exist when it was hashed, and they match `test_*.py`
    like any other test. Counting them made the hash differ on every
    verification, so the two defences cancelled each other out and a
    clean commit reported as tampered with.
    """
    found = surface_check(contract, tar, prefix=TREE_PREFIX,
                          ignore=set(injected) | set(ignore))
    if found and found.get("protected"):
        reason = ("a workspace setup modified the approved check surface "
                  "during prepare") if again else found["reason"]
        logger.warning("verify: %s — %s", commit[:12], reason)
        return _every(criteria, Outcome(FAIL, {
            "reason": reason, "code": CODE_SURFACE_CHANGED,
            "approved": found.get("approved"), "current": found.get("current"),
            "command": ("check surface hash (post-setup)" if again
                        else "check surface hash")}))
    if found and not again:
        # Said out loud every verification. A defence that quietly does
        # nothing is worse than one that is absent, because the absent
        # one is visible.
        logger.warning("verify: %s is UNPROTECTED — %s", commit[:12],
                       found["reason"])
    return None


async def _prepare(driver, contract, criteria, tar, setups, injected,
                   commit, timeout, report, execution_id):
    """Sources plus their own in-tree environment.

    AFTER the surface check, which must hash the pristine tree, and
    BEFORE any criterion, which must run offline against the result.
    """
    from kernel.verifier.prepare import build_prepared     # noqa: PLC0415

    image = contract.get("base_digest") or contract.get("base_image")
    if not image:
        return None, [(c, unrunnable("the contract names no base image",
                                     criterion_id=c.get("id")))
                      for c in criteria]
    # Parsing a whole tar is pure Python over every member; same
    # argument as the archive above.
    pristine = await asyncio.to_thread(names, tar)
    prepared, prep = await build_prepared(driver, image, tar, setups,
                                          inject=injected,
                                          timeout=max(timeout, 600.0),
                                          # Same reason as the replay
                                          # above: one capture Job per
                                          # mount, each needing to name
                                          # its execution to survive the
                                          # orphan sweep.
                                          execution_id=execution_id)
    if report is not None:
        report.update({f"setup_{k}": v for k, v in prep.items()
                       if k != "mounts_env"})
    if not prep["ok"]:
        # `error`, never `fail`: a check that could not be given its
        # environment did not run, so it found nothing wrong (R1).
        logger.warning("verify: setup prepare for %s failed: %s",
                       commit[:12], (prep.get("output") or "")[-300:])
        return None, _every(criteria, unrunnable(
            "the workspace setup could not be prepared", commit=commit,
            mount=prep.get("failed_mount", ""), output=prep.get("output", "")))

    # ---- F4, AGAIN, ON WHAT THE CHECKS WILL ACTUALLY SEE ------------
    # The first surface check hashed the pristine tree; the setups have
    # since run repo-authored shell over each mount. A setup that edits
    # an approved test file is the vendor trick's successor --
    # sanctioned shell rewriting the checks -- and it would pass the
    # first hash and fail nothing else. New files the builds created
    # (.venv ships its own test_*.py) are ignored by NAME; approved
    # files are not, so an edit or a deletion still changes the hash.
    grown = (await asyncio.to_thread(names, prepared)) - pristine
    refused = _surface(contract, prepared, criteria, commit, injected,
                       ignore=grown, again=True)
    if refused:
        return None, refused
    return (prepared, prep.get("mounts_env") or {}), None


async def stage(driver, contract: dict, criteria: list[dict],
                repo_path: str, commit: str, *, mounts=(),
                timeout: float = 300.0, env_deltas=None,
                report: dict | None = None, workspace_host: str = "",
                execution_id: int | None = None):
    """(Staged, None), or (None, one outcome per criterion).

    An archive failure is `error` for every criterion, never `fail`. The
    tree could not be produced, so nothing was checked, so nothing was
    found wrong.
    """
    injected = held_out_files(criteria)
    try:
        tar = await _archive(driver, repo_path, commit, mounts=mounts,
                             injected=injected,
                             workspace_host=workspace_host,
                             execution_id=execution_id)
    except TreeExportError as e:
        logger.warning("verify: could not archive %s: %s", commit[:12], e)
        return None, _every(criteria, unrunnable(
            "the committed tree could not be archived",
            commit=commit, error=str(e)))

    # ── The committed tree may DEFINE its environment (setups) ──────
    #
    # Discovery reads the archive, never the working copy: an
    # environment the executor did not commit is one the team does not
    # have. When a setup is found, F5's recorded-install replay is
    # skipped entirely — the tree's own definition supersedes whatever
    # the executor happened to type, which is the point.
    from kernel.verifier.setups import from_tar as discover_setups
    # GIT MOUNTS ONLY. A copied-in dataset can contain a stray
    # package.json (real datasets do), and discovery that walked every
    # mount would run `npm install` inside somebody's data in a
    # networked root container. A single-mount workspace archives with
    # no `at` prefix; root ("") is then the one mount there is.
    setups = discover_setups(
        tar, [m.at for m in (mounts or [])
              if m.at and getattr(m, "binding", "git") == "git"] or [""])

    # BOTH MODES COMPOSE. A story can hold repo A with a manifest and
    # repo B with none but with recorded env_install deltas -- and the
    # first version of this let A's setup silently drop B's replay,
    # which re-created the exact ModuleNotFoundError the whole
    # mechanism exists to end. Setups build IN-TREE, under /tmp/verify;
    # the replay lands beside it, at /tmp/deps on PYTHONPATH and
    # /tmp/node_modules on node's own walk up. They do not collide, and
    # where both exist the tree's own wins -- see kinds.check_path.
    deps_tar = b""
    if env_deltas:
        deps_tar, refused = await _replay(driver, contract, criteria, commit,
                                          env_deltas, timeout, report,
                                          execution_id)
        if refused:
            return None, refused

    refused = _surface(contract, tar, criteria, commit, injected)
    if refused:
        return None, refused

    mounts_env: dict = {}
    if setups:
        built, refused = await _prepare(driver, contract, criteria, tar,
                                        setups, injected, commit, timeout,
                                        report, execution_id)
        if refused:
            return None, refused
        tar, mounts_env = built

    if report is not None:
        # Evidence for the investigator, underscore-keyed: the EXACT
        # tree the criteria will run against and the environment they
        # get. In-memory hand-off only -- parity/measure builds its own
        # dict, so none of this reaches a verdict row.
        report["_tree_tar"] = tar
        report["_mounts_env"] = mounts_env

    return Staged(tar, deps_tar, mounts_env), None
