"""Where the git broker's bearer lives, and why it is not in the workspace.

THE DEFECT THIS CLOSES. `_credential_helper` interpolated
OPENCLAW_AUTH_TOKEN straight into the helper command, and the caller
persisted that command with `git config credential.helper` -- so the
shared gateway bearer was written verbatim into
`<workspace>/<mount>/.git/config`, mode 0644. The sandbox mounts that
tree and runs as root. One `cat` and model-authored code holds a token
that opens `/internal/git-credential` for ANY story, which answers with
an installation token carrying `contents:write` and `workflows:write`.

The intent was right and is preserved: nothing durable lands on disk
except the helper, and each git operation mints a fresh scoped token.
What went wrong is that the thing minting them was itself durable, and
in the one directory the sandbox can read.

WHY A PATH FIXES IT. The sandbox is a separate container that shares the
worker's NETWORK namespace, not its mount namespace: it sees its own
workspace subpath and the read-only bridge, and nothing else. A file
outside /workspace is therefore invisible to it, while remaining
ordinary to the worker. `.git/config` ends up holding a path, which is
the same pointer-not-value rule `kernel/credentials/issue.py` already
enforces for cloud credentials.

ONE FILE, NOT ONE PER STORY. The bearer is a single process-wide value.
Writing a copy per story would imply a per-story scope that does not
exist, and would leave N files for a sweep that does not know about
them. The scoping is real but it lives at the other end -- the broker
scopes what it mints by (story, repo).
"""

from __future__ import annotations

import logging
import os

from kernel.workspace.driver import WorkspaceError

logger = logging.getLogger(__name__)

# B1: no default inside environ.get -- the fallback is spelled out here
# so it is visible in the code rather than hidden in a call.
_DIR_ENV = os.environ.get("AGENTOS_RUNTIME_DIR")
RUNTIME_DIR = _DIR_ENV or "/run/agentos"

BEARER_NAME = "git-broker-bearer"

_written: str | None = None


def bearer_path() -> str:
    """The path the helper reads. Never inside a workspace."""
    return os.path.join(RUNTIME_DIR, BEARER_NAME)


class BearerUnavailable(WorkspaceError):
    """The pod needs a broker bearer to work on this repo and has none.

    Raised at COMPOSITION, deliberately, so the pod refuses the
    workspace instead of accepting it and failing at publish. See the
    `required` argument below for why late is so much worse than early.

    A `WorkspaceError` so `CompositeDriver.acquire` names the mount it
    happened on -- which matters most in the multi-repo case, where
    "no credential" otherwise says nothing about WHICH repo.
    """


def write_bearer(required: bool = False) -> str | None:
    """Put the broker bearer on disk, worker-side. Returns its path.

    None when there is no token to write, which is the ordinary case for
    a local corpus origin and must stay quiet -- `_credential_helper`
    already returns "" for those and the clone proceeds bare.

    `required=True` says the CALLER knows this workspace cannot work
    without the bearer (a github https source), and turns that quiet
    None into a raise. Only the caller can know: this function sees an
    environment variable, not a remote.

    WHY IT RAISES RATHER THAN RETURNING None LOUDLY. Absent the token,
    a private clone fails immediately with GitHub's `Repository not
    found` -- an auth failure wearing a missing-repo message -- and a
    PUBLIC clone succeeds, reads fine, and fails only at `git push`,
    which is the last thing a story does. Execution 407 (next,
    2026-10-06) took that second path: every check passed, the work was
    committed, and the pod then discovered across eleven park/resume
    generations that it could never deliver it, while the model went
    looking for credentials of its own. The cost of refusing a
    workspace is one failed admission; the cost of accepting one that
    cannot publish is the whole story.

    Idempotent by a module global rather than by statting the file: this
    is called once per mount per story and the token does not change
    within a process.
    """
    global _written
    if _written is not None:
        return _written

    token = (os.environ["OPENCLAW_AUTH_TOKEN"]
             if "OPENCLAW_AUTH_TOKEN" in os.environ else "")
    if not token:
        if required:
            raise BearerUnavailable(
                "OPENCLAW_AUTH_TOKEN is not set in this pod, so no git "
                "credential can be minted and nothing can be pushed. "
                "It reaches the workspace-compose container from the "
                "`conductor-secrets` secret (see "
                "kernel/drivers/k8s_workspace.py CREDENTIAL_KEYS), "
                "where the key is marked optional -- check that the "
                "secret exists in this namespace and carries that key.")
        logger.debug("no OPENCLAW_AUTH_TOKEN; no bearer written "
                     "(expected for a local corpus origin)")
        return None

    path = bearer_path()
    # 0700 on the directory and 0600 on the file, matching
    # kernel/workspace/credentials.py. Worth little against a root
    # sandbox -- which is exactly why the real control is that this
    # path is not mounted there at all -- but it is free, and it keeps
    # the file out of reach of anything else sharing the worker.
    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    # THE FILE, ALWAYS. We created it, so we own it, so this cannot
    # fail -- and it is the protection that actually matters: the token
    # is in the file, not in the directory.
    os.chmod(path, 0o600)

    # THE DIRECTORY, ONLY IF WE OWN IT.
    #
    # Re-asserting 0700 is right where this process made the directory:
    # makedirs skips the mode when it already exists, and an earlier
    # process may have created it wider.
    #
    # It is impossible where the directory is a MOUNT POINT. In an
    # execution pod /run/agentos is an emptyDir, created root-owned by
    # the kubelet, and the container is deliberately not root -- so the
    # chmod returned EPERM and took the whole workspace with it:
    #
    #   workspace not composed: [Errno 1] Operation not permitted:
    #   '/run/agentos'
    #
    # Skipping it there loses nothing, because the isolation is not the
    # mode. It is that the volume is mounted in the composing container
    # and the worker and NOT in the sandbox -- a boundary the sandbox
    # cannot cross whatever the directory permits, which is strictly
    # stronger than 0700 on a shared filesystem. Where this process DOES
    # own the directory the chmod still runs and still must succeed.
    # `os.geteuid` is POSIX-only and the test suite runs on Windows,
    # which has no comparable notion here -- there "we own it" is the
    # right answer, because the mount-point case this guards is a Linux
    # container one and the chmod is harmless.
    geteuid = getattr(os, "geteuid", None)
    try:
        owned = (geteuid is None
                 or os.stat(RUNTIME_DIR).st_uid == geteuid())
    except OSError:                                          # noqa: BLE001
        owned = False
    if owned:
        os.chmod(RUNTIME_DIR, 0o700)
    else:
        logger.info("%s is not owned by this process (a mount point); "
                    "its mode is left alone and the bearer is protected "
                    "by the file mode and by the volume not being "
                    "mounted in the sandbox", RUNTIME_DIR)

    _written = path
    logger.info("git broker bearer written to %s (worker-private)", path)
    return path


def is_outside_workspace(path: str, workspace_root: str) -> bool:
    """The property this module exists to hold. Asserted in tests.

    Kept here rather than in the test so the rule is stated next to the
    thing it constrains -- a future change that relocates RUNTIME_DIR
    into the workspace tree fails a check that lives beside it.
    """
    root = os.path.abspath(workspace_root).rstrip(os.sep) + os.sep
    return not os.path.abspath(path).startswith(root)
