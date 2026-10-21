"""F2 — the committed tree, as a tar, and nothing else.

The Verifier checks what was COMMITTED, not what is lying in the
executor's workspace. That distinction is the difference between
verifying work and verifying an intention: uncommitted edits are not
work anybody can review, merge or roll back.

WHY A TAR PUSHED INTO THE CONTAINER, AND NOT A BIND MOUNT. The first
version mounted the exported tree, which needed a host path — and
`/workspace` is a named volume, so there is none to hand a sibling
container. The obvious repair (mount the whole volume and `cd` into a
subdirectory) would have given every criterion read access to the
executor's live working copy, which is precisely what the committed-tree
rule exists to deny.

Pushing the archive through `PUT /containers/{id}/archive` needs no host
path, no shared volume and no translation, and the Verifier's container
can reach nothing but what it was given. The awkward constraint turned
out to be pointing at a better design.

WHY `git archive` AND NOT A WORKTREE. An archive is a pure snapshot of
one commit — no `.git`, no index, no stash, no untracked files, no
hooks. A worktree shares the object store with the repository the
executor was writing to, so a `.git/hooks/pre-commit` or a
`.git/info/exclude` planted during the work travels with it.

The cost is that `.git` is absent, so a criterion needing history — a
`differential` kind comparing against a base — cannot use this export.
That kind does not exist yet; when it does it gets its own export
carrying exactly the history it needs, rather than this one loosening.
"""

from __future__ import annotations

import io
import logging
import subprocess
import tarfile

logger = logging.getLogger(__name__)


class TreeExportError(Exception):
    """The committed tree could not be produced."""


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TreeExportError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def head_commit(repo: str) -> str:
    """The commit the Verifier will check."""
    return _git(repo, "rev-parse", "HEAD")


def archive_is_remote(driver) -> bool:
    """Is this substrate's `git archive` somewhere this process is not?

    Absent means "local", which is what every driver but Kubernetes
    answers and what E0 answers with no driver attribute at all.
    """
    return getattr(driver, "ARCHIVE_MODEL", "local") != "local"


async def pod_address(conn, execution_id: int, driver=None) -> str:
    """Where this execution's tree is, or "" when it is on this disk.

    THE PAIR `verify_claim` NEEDS, resolved in one place because three
    callers need it and all three had simply not passed it -- so on
    Kubernetes `archive_in_pod("")` refused, `TreeExportError` followed,
    and every criterion of every story came back `error`. Not a wrong
    verdict: no verdict, from the only three callers that produce one.

    Empty for a local substrate, which is the convention
    `ExecutionEnvelope.workspace_host` already uses and which every
    caller already handles. `WorkspaceUnreachable` PROPAGATES for a pod
    substrate that cannot be resolved -- returning "" there would
    reproduce exactly the bug above, and `adjudicate_pass` isolates and
    logs one execution's failure already.
    """
    from kernel.workspace.browse import workspace_host      # noqa: PLC0415

    if not archive_is_remote(driver):
        return ""
    return await workspace_host(conn, execution_id, driver)


def archive_in_pod(host: str, repo: str, commit: str, mounts=(),
                   prefix: str = "verify/", uid: int = 0,
                   inject: dict | None = None,
                   execution_id: int | None = None) -> bytes:
    """The committed tree of a repository this process cannot open.

    The pod's worker runs `archive_committed_tree` — the same function,
    on the filesystem that has the repository — and returns the tar.

    THE HELD-OUT SET IS ADDED HERE AND NEVER CROSSES. `podfs` has no
    `inject` argument, and giving it one would be the wrong repair:
    F4's rule is that the held-out checks are "absent from the executor
    entirely", and the execution's pod is the executor's pod. They live
    in the criteria, in the conductor's database, and are written into
    the copy the Verifier receives — which is exactly what the local
    path does, one process later.

    MULTI-MOUNT IS REFUSED, not half-served. `archive_mounts`
    reassembles a workspace by running git per repository AND walking
    the copied-in documents off the local filesystem; `podfs` exposes
    only the git half. A git-only archive would hand the criteria a
    tree with every document missing — the failure `archive_mounts`
    records in its own comment, where a suite failed on a rule nobody
    had got wrong and the executor was sent to fix correct code twice.
    """
    from kernel.workspace.podfs_client import (WorkspaceUnreachable, archive,
                                               archive_mounts as remote_mounts)

    nested = [m for m in (mounts or ()) if getattr(m, "at", "")]
    try:
        if nested:
            # THE WHOLE WORKSPACE, git and copied-in documents together.
            #
            # This used to REFUSE, because `podfs` exposed only the git
            # half and a tree with every document missing is worse than
            # no tree. The refusal was right and it was total: on
            # Kubernetes every workspace is `repos/<owner>__<name>`, so
            # `any(m.at)` is always true and no story could be verified
            # at all -- the wall the first story to reach the Verifier
            # would have hit. `archive_mounts` now runs in the pod,
            # where both halves are.
            tar = remote_mounts(host, repo, mounts, prefix=prefix, uid=uid,
                                execution_id=execution_id)
        else:
            tar = archive(host, repo, commit, prefix=prefix, uid=uid,
                          execution_id=execution_id)
    except WorkspaceUnreachable as exc:
        # The verifier's one refusal path. `error` for every criterion
        # and never `fail`: the tree could not be produced, so nothing
        # was checked, so nothing was found wrong.
        raise TreeExportError(str(exc)) from exc
    if not inject:
        return tar
    from kernel.verifier.treestream import write_injected  # noqa: PLC0415

    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r") as src, \
            tarfile.open(fileobj=out, mode="w") as dst:
        for member in src:
            dst.addfile(member, src.extractfile(member)
                        if member.isfile() else None)
        write_injected(dst, inject, prefix, uid)
    return out.getvalue()


def archive_committed_tree(repo: str, commit: str, prefix: str = "verify/",
                           uid: int = 0, exclude: list[str] | None = None,
                           inject: dict | None = None) -> bytes:
    """`commit` as a tar, ready to push into a container.

    `uid` rewrites ownership on every entry. The Verifier's container
    runs as a different uid from the executor's on purpose, and a tree it
    cannot write is a tree most check runners fail on for a reason that
    has nothing to do with the work — pytest alone wants `__pycache__`
    and `.pytest_cache`. The point of the different uid is that the
    EXECUTOR cannot have pre-placed something executable, not that the
    checks run read-only.

    `exclude` drops path prefixes, relative to the repository root.

    `inject` adds files that are NOT in the repository — F4's held-out
    set. They live in the criterion that owns them and are written
    into the Verifier's copy here, which is what makes 'absent from
    the executor entirely' literally true. Deleting them from the
    executor's workspace instead would not: the executor commits, the
    deletion is IN the commit, and the Verifier's archive of that
    commit has no held-out set either.
    """
    from kernel.verifier.treestream import export           # noqa: PLC0415

    payload = export(repo, commit, None, prefix=prefix, uid=uid,
                     exclude=exclude, inject=inject)
    logger.info("archived %s of %s: %d bytes", commit[:12], repo,
                len(payload))
    return payload


def stream_committed_tree(repo: str, commit: str, dst: str,
                          prefix: str = "verify/", uid: int = 0,
                          exclude: list[str] | None = None,
                          inject: dict | None = None) -> str:
    """The same export, landing at `dst` instead of in this process.

    E5. The bytes form above is correct AND it is a ceiling: a `large`
    tier's volume is 50GiB, and `bytes` of that is not a slow
    verification but a killed process -- classified `error`, retried
    three times, then handed back to the executor at gen+1 to repair
    code that was never broken. Plan 9.6. Any caller with somewhere to
    put a file should use this one.
    """
    from kernel.verifier.treestream import export           # noqa: PLC0415

    export(repo, commit, dst, prefix=prefix, uid=uid, exclude=exclude,
           inject=inject)
    logger.info("archived %s of %s to %s", commit[:12], repo, dst)
    return dst


def fetch_tree(host: str, repo: str, commit: str, dst: str,
               prefix: str = "verify/", uid: int = 0,
               exclude: list[str] | None = None,
               inject: dict | None = None, *,
               execution_id: int) -> str:
    """The POD's committed tree, streamed onto this process's disk.

    `archive_in_pod`'s streaming twin, and the shape section 5.5 asks
    for: the archive crosses the wire into a file and is read lazily
    from there, so neither end ever holds a tree.

    THE HELD-OUT SET IS APPENDED AFTER THE FETCH and never requested
    from the pod -- F4's rule is that those checks are "absent from the
    executor entirely", and the execution's pod is the executor's.
    `append_injected` seeks past the end-of-archive blocks rather than
    rewriting, so the injection costs its own size and not the tree's.
    """
    from kernel.verifier.treestream import append_injected  # noqa: PLC0415
    from kernel.workspace.podfs_client import (             # noqa: PLC0415
        WorkspaceUnreachable, archive_to_file)

    try:
        archive_to_file(host, repo, commit, dst, prefix=prefix, uid=uid,
                        exclude=exclude, execution_id=execution_id)
    except WorkspaceUnreachable as exc:
        # The verifier's one refusal path, the same as `archive_in_pod`'s:
        # `error` for every criterion and never `fail`. The tree could
        # not be produced, so nothing was checked, so nothing was found
        # wrong.
        raise TreeExportError(str(exc)) from exc
    append_injected(dst, inject, prefix, uid)
    return dst


def held_out_files(criteria: list[dict]) -> dict:
    """The held-out checks, as path -> content, from the criteria owning them.

    F4's fourth defence, and the reason it is an INJECTION rather than a
    deletion. A held-out check that lived in the repository and was
    removed from the executor's workspace would be removed from the
    commit too, and the Verifier's archive of that commit would not have
    it either — the defence would delete itself.

    Kept in the criterion instead. It is never in the repository, so
    there is nothing for the executor to read, tune against, or notice
    the absence of.
    """
    files: dict = {}
    for criterion in criteria:
        if not criterion.get("held_out"):
            continue
        for path, body in ((criterion.get("spec") or {}).get("files")
                           or {}).items():
            files[path] = body
    return files


def mount_digest(root: str, mounts) -> tuple:
    """(at, sha) for every git mount. E7f — the digest is a SET.

    A verdict is keyed to a claim and a claim was judged against a tree.
    With several repositories that is several trees, so one sha names a
    fraction of what was checked -- and re-running the Verifier "at that
    digest" would reconstruct a different world.
    """
    import os

    out = []
    for m in mounts or ():
        if m.binding != "git":
            continue
        at = os.path.join(root, m.at) if m.at else root
        try:
            out.append((m.at, head_commit(at)))
        except TreeExportError:
            out.append((m.at, ""))          # present, but nothing committed
    return tuple(out)


async def tree_state(conn, execution_id: int, root: str, mounts):
    """(digest, head) for a workspace, wherever it physically is.

    THE LEG RUNS IN THE CONDUCTOR AND THE TREE DOES NOT. On Docker the
    conductor shares the workspace volume, so reading `/workspace` here
    was reading the story's checkout. On Kubernetes the tree is in the
    execution's pod and the conductor has no such path -- so both the
    dry-run and the ADJUDICATOR read nothing and concluded, in the
    adjudicator's own words,

        no committed tree to verify; the Verifier checks commits,
        and there is none

    for work that had committed perfectly well. One function because
    two callers asking the same question in two ways is how they come
    to disagree about whether a story passed.
    """
    import asyncio                                          # noqa: PLC0415
    import functools                                        # noqa: PLC0415

    from kernel.drivers.registry import resolve             # noqa: PLC0415
    from kernel.workspace.browse import workspace_host      # noqa: PLC0415
    from kernel.workspace.podfs_client import (             # noqa: PLC0415
        WorkspaceUnreachable, digest as remote_digest, is_pod)

    if not root:
        return (), None
    if not is_pod(resolve()):
        # OFF THE EVENT LOOP: `head_commit` shells out to git and
        # `mount_digest` walks every mount on disk. The pod branch below
        # already went through `to_thread`; this one -- the Docker and
        # E0 path -- did not, and there is no reason for the two to
        # differ. See kernel/verifier/verify.py for what inline blocking
        # costs the conductor.
        def _local():
            try:
                head = head_commit(root)
            except TreeExportError:
                head = None
            return mount_digest(root, mounts), head

        return await asyncio.to_thread(_local)

    try:
        host = await workspace_host(conn, execution_id)
        out = await asyncio.to_thread(
            functools.partial(remote_digest, host, root, mounts,
                              execution_id=execution_id))
    except WorkspaceUnreachable as exc:
        logger.warning("tree_state: workspace for %s unreachable: %s",
                       execution_id, exc)
        return (), None
    return (tuple(tuple(p) for p in out.get("digest") or ()),
            out.get("head"))


def archive_mounts(root: str, mounts, prefix: str = "verify/", uid: int = 0,
                   inject: dict | None = None) -> bytes:
    """Every mount as ONE tar, each under its own path.

    The Verifier runs checks against the tree the story produced, and
    with several repositories that tree is the composition of them. This
    reassembles it inside the archive so a check written as
    `cd repos/acme__api && pytest` means the same thing in the Verifier's
    container as it did in the sandbox.

    Held-out files are injected into the FIRST writable mount, which is
    where a test belongs when there is a choice; with one mount that is
    the same behaviour as before.
    """
    import io as _io
    import os
    import tarfile

    writable_first = [m for m in mounts if m.writable] or list(mounts)
    target = writable_first[0].at if writable_first else ""

    buf = _io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as out:
        for m in mounts:
            at = os.path.join(root, m.at) if m.at else root
            sub = f"{prefix}{m.at}/" if m.at else prefix
            if m.binding != "git":
                # copyin and none have no commit; take it as it stands.
                # An input that is not under version control is still
                # part of the tree the checks see -- and for a document
                # it is the WHOLE contract: a story implementing
                # VALIDATION_RULES.md cannot be verified against a tree
                # that does not contain it.
                #
                # A MOUNT CAN BE ONE FILE. An uploaded document mounts
                # at `docs/NAME.md` -- the file itself, not a folder --
                # and `os.walk` on a file yields nothing at all. So
                # every document was silently dropped from the verified
                # tree: the executor's tests read `docs/PRICE_LIST.xlsx`
                # and passed, the verifier had no such file, and half a
                # suite failed on a rule nobody had got wrong. The
                # executor was then sent to fix validation logic that
                # was correct, twice, before escalating.
                if os.path.isfile(at):
                    info = out.gettarinfo(at, arcname=f"{prefix}{m.at}")
                    info.uid = info.gid = uid
                    with open(at, "rb") as fh:
                        out.addfile(info, fh)
                    continue
                for base, _dirs, files in os.walk(at):
                    for name in files:
                        full = os.path.join(base, name)
                        rel = os.path.relpath(full, at).replace(os.sep, "/")
                        info = out.gettarinfo(full, arcname=f"{sub}{rel}")
                        info.uid = info.gid = uid
                        with open(full, "rb") as fh:
                            out.addfile(info, fh)
                continue
            piece = archive_committed_tree(
                at, head_commit(at), prefix=sub, uid=uid,
                inject=inject if m.at == target else None)
            with tarfile.open(fileobj=_io.BytesIO(piece), mode="r") as src:
                for member in src.getmembers():
                    handle = src.extractfile(member) if member.isfile() else None
                    out.addfile(member, handle)
    return buf.getvalue()
