"""E15 — where the prepare Job goes to get the tree, as an env contract.

THE PREPARE JOB IS NETWORKED, AND ALWAYS WAS (plan 5.15). Zero egress is
a property of the CRITERIA pod -- F2d is about what a CHECK can reach --
and prepare runs no check. So the tree does not have to travel inside
the Job's own command: the Job can fetch it.

WHAT THAT REPLACES. `env_payload.payload` carried `.agentos/setup.sh`,
`.agentos/env` and every recognised manifest as a base64 tar.gz in the
Job's command, capped at 256KB because a Kubernetes object is capped
near 1MiB. It was a REGRESSION and E8 said so: a `setup.sh` that reads a
sibling, or a `requirements.txt` carrying `-e .` or `-r base.txt`,
received only itself and failed. Execution 207 wrote a `setup.sh`.

TWO VARIABLES AND NOT ONE, and the split is the secret.
`podfs_auth.derive_read_only` is what the Job presents -- it can pull an
`archive` and is refused `publish`, `commit` and `credentials` (5.15a)
-- and a token belongs in its own variable so that logging, diffing or
echoing the DESCRIPTION cannot leak it. The description is inert and
worth printing; the token never is.

THE SECRET REACHES ONLY THE INIT CONTAINER. It is set on the fetch
container's `env` in `prepare_job`, never on the `ExecutionSpec` the
CONTRACT's image runs -- so repo-authored `setup.sh`, which is the one
piece of model-adjacent code in this Job, cannot read it out of its own
environment. `kernel/spec.py`'s `assert_no_credentials` refuses a spec
env key ending in `_TOKEN` for the same reason, and this stays on the
right side of that guard rather than defeating it.
"""

from __future__ import annotations

import json
import os

from kernel.verifier import volume

# The non-secret half: host, mounts, prefix, uid. JSON because it is a
# LIST of mounts -- a multi-repo workspace is the ordinary shape on this
# substrate ("repos/<owner>__<name>" for every story), and four
# positional variables that have to stay the same length is the kind of
# contract that breaks on the first two-repo story.
TREE_VAR = "AGENTOS_PREPARE_TREE"

# The read-only workspace token, alone, so nothing that prints the
# description prints this.
TOKEN_VAR = "AGENTOS_PREPARE_TOKEN"

# `podfs_stream.respond` falls back to "verify/" for an empty prefix, so
# an empty one cannot be asked for. Spelled here once and stripped again
# on extraction; `setups.from_tar` and `env_payload.collect` use the
# same word for the same reason.
PREFIX = "verify/"

# WHERE THE STREAMED TAR LANDS, and it is on the VOLUME rather than in
# the container's filesystem. A `large` tier's tree is bounded by the
# disk it was built on, and the disk is right here; ephemeral storage on
# Autopilot is not, and a pod evicted for local storage reports nothing
# a person can act on. Deleted as soon as it is unpacked.
STAGE = f"{volume.MOUNT}/.agentos-tree.tar"


class SourceMissing(RuntimeError):
    """The Job was started without being told where its tree is.

    LOUD AND AT THE START. Without this the init container would fetch
    nothing, the prepare container would find an empty `TREE`, and every
    install would fail on a missing manifest -- which reads exactly like
    a repository that committed none.
    """


def describe(host: str, mounts, uid: int) -> str:
    """The inert half of the contract, as the Job's env value.

    `mounts` is [(at, repo, commit)]. `repo` is the path INSIDE the
    execution pod -- the conductor cannot open it and never tries; it is
    handed to `podfs`, which is running on the filesystem that has it.
    """
    rows = [{"at": at or "", "repo": repo, "commit": commit}
            for at, repo, commit in mounts]
    return json.dumps({"host": host, "uid": int(uid), "mounts": rows},
                      sort_keys=True)


def read() -> dict:
    """{"host", "uid", "mounts", "token"} as the init container sees it.

    `os.environ[...]` and never `.get(x, default)`: the house rule in
    `kernel/` bans a default nobody can see, and here the policy is that
    there is no default -- a prepare Job with no source is a Job that
    would silently build an environment for an empty tree.
    """
    for name in (TREE_VAR, TOKEN_VAR):
        if name not in os.environ or not os.environ[name].strip():
            raise SourceMissing(
                f"{name} is unset or empty, so this prepare Job cannot "
                f"pull the committed tree it is meant to install "
                f"against. Both variables are set by "
                f"kernel/verifier/prepare_job.py:tree_container.")
    body = json.loads(os.environ[TREE_VAR])
    if not body.get("host") or not body.get("mounts"):
        raise SourceMissing(
            f"{TREE_VAR} names no workspace host or no mounts: "
            f"{os.environ[TREE_VAR][:200]}")
    return {**body, "token": os.environ[TOKEN_VAR].strip()}


def request(mount: dict, uid: int) -> dict:
    """The `/archive` payload for one mount.

    THE PREFIX CARRIES THE MOUNT, so a two-repo workspace reassembles
    itself on extraction: members arrive as `verify/<at>/...` and land at
    `TREE/<at>/...`, which is the same layout the criteria will see and
    the same one `setups` discovered the manifests under.
    """
    at = mount.get("at") or ""
    return {"op": "archive", "repo": mount["repo"],
            "commit": mount.get("commit") or "HEAD",
            "prefix": PREFIX + (f"{at}/" if at else ""),
            "uid": int(uid), "exclude": []}


def from_mounts(host: str, root: str, mounts, commit: str) -> dict:
    """The `source` a caller already holds, as `build_on_volume` takes it.

    ONE COMMIT SEMANTICS, MATCHING THE ARCHIVE PATH EXACTLY. `stage._archive`
    pins the caller's `commit` only in the FLAT case and otherwise lets
    `tree.archive_mounts` take each mount's own `head_commit` -- so a
    multi-repo workspace is several heads and never one. Spelling a
    different rule here would prepare an environment from one tree and
    check the work against another, which is the disagreement class 5.10
    is about.

    GIT MOUNTS ONLY. A `copyin` document has no commit to archive and no
    install manifest in it; `archive_mounts` walks them for the CRITERIA,
    which read them, and an install does not. Stated rather than silently
    filtered: a `setup.sh` that reads an uploaded document is a gap, and
    it is this line.
    """
    git = [m for m in (mounts or ()) if getattr(m, "binding", "") == "git"]
    nested = [m for m in git if getattr(m, "at", "")]
    if not nested:
        rows = [("", root, commit)]
    else:
        rows = [(m.at, f"{root}/{m.at}", "HEAD") for m in nested]
    return {"host": host, "mounts": rows}
