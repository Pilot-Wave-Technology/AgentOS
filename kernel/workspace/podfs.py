"""Workspace operations, served from the container that has the workspace.

WHAT THIS IS NOT. It is not "the worker tools, moved". Several of them —
`emit_artifact`, `publish_artifact` — need the DATABASE as well as the
tree, and an execution pod must never hold a DSN: `assert_no_credentials`
refuses one in a spec and the whole sandbox design rests on the pod
holding as little as possible. Moving those wholesale would have carried
the database into the pod to reach a filesystem.

SO THE SPLIT IS BY RESOURCE, NOT BY TOOL. This serves the FILESYSTEM
half — read a file in scope, commit the writable mounts, archive a
commit, publish, walk the tree. The conductor keeps the DATABASE half
and keeps orchestrating: `emit_artifact` still decides what supersedes
what and still writes `artifact_manifest`; it just asks for the bytes
instead of opening the file.

Each primitive is the SAME function the conductor calls on Docker,
imported here and called with a local path. `read_in_scope`,
`story_commit`, `archive_committed_tree` and `explorer` are not
reimplemented — what moves is which process runs them.

WHY HTTP AND NOT THE BRIDGE. The bridge belongs to the SANDBOX, which
holds no credential by design, and these operations need the git broker
bearer. Putting them on the sandbox's listener would hand the container
the model drives the one credential the whole arrangement keeps away
from it. Two listeners, two trust levels, one protocol shape.

WHO MAY CALL IT. Two controls, and the first one alone was not enough.
The `execution-bridge-ingress` NetworkPolicy decides which POD may open
9101, and the namespace is open on ingress by default, so that policy is
a control and not a formality — see kernel/drivers/k8s_bridge.py.

BUT A NETWORK POLICY IS ABOUT PODS, AND THE SANDBOX IS NOT A POD. It is
a CONTAINER in this one, sharing this network namespace, so it reaches
this server on 127.0.0.1 without crossing any policy at all. Verified
live on exec-193: the model's own `bash` ran `curl 127.0.0.1:9101` with
`{"op":"explore"}` and got the whole workspace tree — and `commit`,
`credentials`, `revoke_credentials` and `plan` were reachable the same
way, from the one container the whole design assumes is hostile.

So every operation now carries a per-execution token that only the
conductor can derive and only the WORKER container is given.
kernel/workspace/podfs_auth.py is the contract and holds the reasoning;
this module is one of its two ends.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kernel.workspace import podfs_auth

logger = logging.getLogger(__name__)

# ONE SENTENCE FOR EVERY REFUSAL. "no token is configured" and "your
# token is wrong" are different facts and an unauthorised caller is
# entitled to neither: the first of them tells the sandbox that any
# header at all would have worked, which is the answer it is probing
# for. So the body is a constant and the log is where the difference
# lives.
UNAUTHORISED = ("E_UNAUTHORISED: this workspace answers only the "
                "conductor that owns the execution.")

# A RECOGNISED TOKEN THAT IS TOO NARROW IS A DIFFERENT FACT, and unlike
# the one above it is safe to state: the caller already holds a token
# for this execution, so nothing is disclosed by telling it which half
# of the surface that token is for. E5 gives the verification pod a
# read-only token so it can pull its own tree without being able to
# `publish` the branch it is checking.
NARROW = ("E_READ_ONLY: this token may read the workspace and not "
          "change it. See kernel/workspace/podfs_auth.py.")

# Beside the bridge's 9100, so one policy can name a contiguous pair and
# a reader can see at a glance that both are execution-pod surfaces.
WORKER_PORT = 9101

# B1: the fallback is spelled out rather than hidden in a get() default.
_ROOT_ENV = os.environ.get("AGENTOS_WORKSPACE_ROOT")
ROOT = _ROOT_ENV or "/workspace"

DEFAULT_BIND = "127.0.0.1"


def op_read(args: dict) -> dict:
    """Bytes of one file, scope-checked. For `emit_artifact`.

    The scope check is `read_in_scope`, unchanged: it normalises before
    comparing and keeps the separator in the prefix, because without it
    `/workspace/exec-4` accepts `/workspace/exec-42` — another
    execution's tree.
    """
    from kernel.harness.emit import read_in_scope

    data, problem = read_in_scope(args.get("root") or ROOT,
                                  args.get("path") or "")
    if problem:
        return {"error": problem}
    # base64 because this is JSON and an artifact is bytes: a report is
    # usually text and a figure never is, and guessing per payload is
    # how a PNG arrives corrupted and nobody knows which layer did it.
    return {"bytes": base64.b64encode(data).decode("ascii")}


def op_commit(args: dict) -> dict:
    """Commit every writable mount that changed. The same tool."""
    from kernel.envelope import ExecutionEnvelope
    from kernel.harness import tools_worker

    envelope = ExecutionEnvelope(
        workspace_path=args.get("root") or ROOT,
        mounts=_mounts(args.get("mounts") or []))
    result = tools_worker.story_commit(envelope, args.get("args") or {},
                                       args.get("id") or "commit")
    return {"output": result.output, "error": result.error,
            "exit_code": result.exit_code}


def op_heads(args: dict) -> dict:
    """Which branch each writable mount is checked out on.

    For D1's lane check, which must run WHERE THE CHECKOUT IS. The
    conductor asked `git rev-parse` against its own filesystem, found
    nothing, and concluded no mount was on a lane -- a check that cannot
    see the tree always passes, which is the worst way for a guard
    against pushing to a merge target to fail.
    """
    import subprocess                                       # noqa: PLC0415

    root = args.get("root") or ROOT
    out = {}
    for mount in _mounts(args.get("mounts") or []):
        if not mount.writable or mount.binding != "git":
            continue
        path = os.path.join(root, *mount.at.split("/")) if mount.at else root
        try:
            out[mount.at] = subprocess.run(
                ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
                timeout=30).stdout.strip()
        except Exception:                                   # noqa: BLE001
            out[mount.at] = ""
    return {"heads": out}


def op_changed(args: dict) -> dict:
    """What this story changed, per writable mount. `{at: "stat text"}`.

    FOR THE ASSESSOR, and it exists because the assessor was given a
    TREE and a task and left to find the work itself. On a 2,447-file
    repository that is a hunt through a listing capped at 400 -- so it
    spent its steps orienting and could still miss the four files the
    story actually touched.

    IT MUST RUN HERE. `git archive` is how the tree reaches the
    verification pod, and an archive carries no `.git`: the pod holds
    files at one commit and no history, so `git diff` is impossible
    there and no tool design fixes that. The clone is in the EXECUTION
    pod, which is where this runs.

    FROM THE MERGE-BASE, NOT THE LANE HEAD. A story has many rounds and
    the assessor judges the finished work, not the last delta -- round
    three touching one file does not make that file the thing to judge.
    And merge-base rather than the lane itself, so somebody else merging
    into the trunk mid-story does not arrive as this story's work.

    NEVER RAISES. A mount whose lane cannot be resolved returns "" and
    the caller falls back to the tree listing, which is what every
    assessment did before this existed.
    """
    import subprocess                                       # noqa: PLC0415

    root = args.get("root") or ROOT
    lanes = args.get("lanes") or {}
    out = {}
    for mount in _mounts(args.get("mounts") or []):
        if not mount.writable or mount.binding != "git":
            continue
        path = os.path.join(root, *mount.at.split("/")) if mount.at else root
        lane = (lanes.get(mount.at) or "").strip()
        if not lane:
            out[mount.at] = ""
            continue
        try:
            ref = _lane_ref(path, lane)
            if not ref:
                out[mount.at] = ""
                continue
            base = subprocess.run(
                ["git", "-C", path, "merge-base", ref, "HEAD"],
                capture_output=True, text=True, timeout=30).stdout.strip()
            if not base:
                out[mount.at] = ""
                continue
            out[mount.at] = subprocess.run(
                ["git", "-C", path, "diff", "--stat", f"{base}..HEAD"],
                capture_output=True, text=True,
                timeout=60).stdout.strip()[:8000]
        except Exception:                                   # noqa: BLE001
            out[mount.at] = ""
    return {"changed": out}


def op_publish(args: dict) -> dict:
    """Push every writable mount. THE SAME CODE, in the right container.

    `publish_execution` ran `CompositeDriver().publish` in the conductor,
    whose filesystem has no `/workspace` on this substrate -- so every
    mount came back MOUNT_MISSING, the push was refused, and the
    executor was told "your work PASSED verification but could not be
    pushed". Measured on execution 203, and it meant NO story on a pod
    substrate could ever reach DONE.

    `story_commit` had already been moved here for exactly this reason
    (see `op_commit`); publish was the half that never followed, which
    is why the commits existed and the branch sat [ahead 1].

    The git credentials are here too, which is where rule 9 always
    wanted this to run.
    """
    from kernel.workspace.composite import CompositeDriver  # noqa: PLC0415

    refs = CompositeDriver().publish(
        args.get("root") or ROOT,
        tuple(_mounts(args.get("mounts") or [])))
    return {"refs": [{"kind": r.kind, "ref": r.ref, "at": r.at,
                      "detail": r.detail} for r in refs]}


def op_archive(args: dict) -> dict:
    """A committed tree as a tar, for a criterion.

    THE COMMITTED TREE, never the working copy — that distinction is the
    difference between verifying work and verifying an intention, and it
    is why this takes a commit rather than a directory.
    """
    from kernel.verifier.tree import archive_committed_tree

    tar = archive_committed_tree(
        args.get("repo") or ROOT, args.get("commit") or "HEAD",
        prefix=args.get("prefix") or "verify/",
        uid=int(args.get("uid") or 0),
        exclude=args.get("exclude") or None)
    return {"bytes": base64.b64encode(tar).decode("ascii")}


def op_explore(args: dict) -> dict:
    """The working tree as the assistant panel shows it.

    THE WORKING TREE AND NOT THE LAST COMMIT, which is the explorer's
    whole point: "a leg can run twenty minutes between checkpoints, and
    the moment worth watching is the one where the edit exists and the
    commit does not".
    """
    from kernel.workspace import explorer

    what = args.get("what") or "tree"
    root = args.get("root") or ROOT
    if what == "tree":
        return {"result": explorer.tree(root, args.get("at") or "",
                                        bool(args.get("writable")))}
    if what == "read":
        return {"result": explorer.read(root, args.get("rel") or "")}
    if what == "diff":
        return {"result": explorer.diff(root, args.get("rel") or "")}
    return {"error": f"E_UNKNOWN_OP: no explorer operation {what!r}"}


def op_archive_mounts(args: dict) -> dict:
    """The whole workspace as one tar. The same function.

    `archive_in_pod` could only ask for the GIT half, so it refused any
    workspace with a nested mount rather than hand the criteria a tree
    with every document missing. That refusal was right and it was
    total: on Kubernetes EVERY workspace is `repos/<owner>__<name>`, so
    `any(m.at)` is always true and no story could be verified at all.

    `archive_mounts` walks git and copyin together, and it belongs in
    the pod because that is where both halves are.

    NO `inject` ACROSS THE WIRE, deliberately. The held-out checks are
    "absent from the executor entirely" and this pod IS the executor's;
    they are added to the tar one process later, conductor-side, which
    is what the local path already does.
    """
    import base64

    from kernel.verifier.tree import archive_mounts

    tar = archive_mounts(args.get("root") or ROOT,
                         _mounts(args.get("mounts") or []),
                         prefix=args.get("prefix") or "verify/",
                         uid=int(args.get("uid") or 0))
    return {"result": base64.b64encode(tar).decode()}


def op_credentials(args: dict) -> dict:
    """Put a cloud lease and its sidecars in the workspace. Same function.

    `credentials/issue.py` wrote these with plain filesystem calls from
    the conductor, which has no `/workspace` on a pod substrate -- so a
    goal with a cloud grant could not be honoured, and `issue` calls
    that fatal on purpose: "a run that should have had an identity and
    silently did not fails later, somewhere unrelated, with a permission
    error nobody traces back to this decision."

    THE TOKEN CROSSES ONCE, to the pod that is entitled to it, and is
    written under `.agentos/credentials` exactly as the local path
    writes it. Nothing is returned but the paths.
    """
    from kernel.workspace.credentials import place_lease

    written = place_lease(args.get("root") or ROOT,
                          args.get("lease_id") or "",
                          args.get("payload") or {},
                          args.get("sidecars") or {})
    return {"result": written}


def op_revoke_credentials(args: dict) -> dict:
    """Delete a lease. The part of revocation that actually works."""
    from kernel.workspace.credentials import revoke

    return {"result": bool(revoke(args.get("root") or ROOT,
                                  args.get("lease_id") or ""))}


def op_plan(args: dict) -> dict:
    """Mirror the plan into every writable mount. The same function.

    The leg runs in the conductor and the tree is here, so this crosses
    for the filesystem half only -- the rendering, the cycle number and
    the story id are all settled before the call.
    """
    from kernel.harness.planfiles import write_files

    written = write_files(args.get("root") or ROOT,
                          _mounts(args.get("mounts") or []),
                          args.get("story") or "",
                          int(args.get("cycle") or 1),
                          args.get("body") or "")
    return {"result": written}


def op_digest(args: dict) -> dict:
    """The committed sha per mount, and the root's HEAD.

    The dry-run asks "what tree would these checks run against", and it
    asked the CONDUCTOR's filesystem -- which has no /workspace, so the
    answer was always "no committed tree to check against yet" and the
    preview it promises never ran.
    """
    from kernel.verifier.tree import TreeExportError, head_commit, mount_digest

    root = args.get("root") or ROOT
    mounts = _mounts(args.get("mounts") or [])
    try:
        head = head_commit(root)
    except (TreeExportError, Exception):                    # noqa: BLE001
        head = None
    return {"result": {"head": head,
                       "digest": [list(pair) for pair in
                                  mount_digest(root, mounts)]}}



def _lane_ref(path: str, lane: str) -> str:
    """The trunk lane as THIS clone can name it, or "".

    A LANE IS A BARE BRANCH NAME -- `goal_resources.branch_name`, e.g.
    `calculator` -- and a clone does not have one. `git clone` makes a
    local branch for the default branch and `git checkout <ref>` makes
    one for the story's working branch; every other branch exists only
    as `refs/remotes/origin/<name>`.

    AND A BARE NAME DOES NOT FIND IT. Git resolves `<name>` against
    `refs/<name>`, `refs/tags/<name>`, `refs/heads/<name>`,
    `refs/remotes/<name>` and `refs/remotes/<name>/HEAD` -- never
    `refs/remotes/origin/<name>`. So `git merge-base calculator HEAD`
    printed nothing, `op_changed` returned "" exactly as it is written
    to, and the assessor fell back to a tree listing on every story.
    A dark feature: the failure and the feature having nothing to say
    look identical from outside.

    `origin/<lane>` FIRST, because that is what a clone has and it is
    also the FRESHER of the two after a fetch. The bare name second, for
    a workspace where the lane really is checked out locally -- the
    corpus origins do that -- and "" last, which the callers already
    treat as "cannot answer".
    """
    import subprocess                                       # noqa: PLC0415

    for candidate in (f"origin/{lane}", lane):
        got = subprocess.run(
            ["git", "-C", path, "rev-parse", "--verify", "--quiet",
             f"{candidate}^{{commit}}"],
            capture_output=True, text=True, timeout=30)
        if got.returncode == 0 and got.stdout.strip():
            return candidate
    return ""


def op_sync(args: dict) -> dict:
    """Bring the trunk into every writable mount. `{at: {...}}`.

    FOR THE DIFF STORY. A feature branch is cut from the trunk and the
    trunk moves under it -- somebody else landed work -- so the branch
    has to take those changes before it can merge. That is an ordinary
    coding story with a brief, but the git half of it cannot be done by
    the model.

    IT MUST RUN HERE, AND THAT IS THE WHOLE REASON THIS EXISTS. Fetching
    needs a credential. The credential helper is persisted into
    `.git/config`, which the sandbox reads, but it is a POINTER: the
    bearer lives in a worker-private directory the sandbox has no mount
    for (`workspace/git_bearer.py`, and `bindings._credential_helper`
    explains why interpolating the value there published the shared
    gateway bearer to model-authored code). So `git fetch` from the
    sandbox authenticates as nobody and fails on a private repository.
    Commit and publish are worker-side for exactly this reason; sync is
    the third of the same kind, and it was missing.

    FETCH FIRST, ALWAYS. The clone happened at dispatch and the trunk is
    what has moved since -- often after the clone, which is what created
    the story. Merging `origin/<lane>` without fetching merges the trunk
    as it was when the pod started, leaves the branch still behind, and
    looks exactly like success.

    MERGE, NEVER REBASE. The branch is already pushed and `git_nodes`
    and the PR name its commits; rebasing rewrites them and needs a
    force-push that invalidates everything pointing at the old shas.
    A merge also keeps the merge-base meaningful, which is what
    `op_changed` reads to show the assessor this story's own work.

    A CONFLICT IS AN ANSWER, NOT A FAILURE. It is left in the working
    tree with the paths named, because resolving it is the one part of
    this the model CAN do -- editing files needs no credential -- and it
    then commits through `story_commit` as usual.
    """
    import subprocess                                       # noqa: PLC0415

    root = args.get("root") or ROOT
    lanes = args.get("lanes") or {}
    out = {}
    for mount in _mounts(args.get("mounts") or []):
        if not mount.writable or mount.binding != "git":
            continue
        path = os.path.join(root, *mount.at.split("/")) if mount.at else root
        lane = (lanes.get(mount.at) or "").strip()
        if not lane:
            out[mount.at] = {"state": "error",
                             "detail": "no trunk lane for this mount"}
            continue
        out[mount.at] = _sync_one(subprocess, path, lane)
    return {"synced": out}


def _group_writable_umask() -> dict:
    """`subprocess.run` kwargs giving git a 0002 umask, where there is one.

    Per call, not process-wide: a process umask would widen every file
    the worker writes, including ones the sandbox must not touch.
    """
    if os.name != "posix":
        return {}
    return {"preexec_fn": lambda: os.umask(0o002)}


def _make_merged_files_group_writable(subprocess, path: str) -> None:
    """`chmod g+w` every file the merge touched. Never raises.

    Belt and braces with the umask: a file git rewrote IN PLACE keeps
    its old mode, and a tree from before this fix has 0644 files the
    umask never sees. ORIG_HEAD is the pre-merge commit for both a
    clean merge and a conflicted one.
    """
    import stat
    try:
        names = set(subprocess.run(
            ["git", "-C", path, "diff", "--name-only", "ORIG_HEAD"],
            capture_output=True, text=True, timeout=60).stdout.split())
        names |= set(subprocess.run(
            ["git", "-C", path, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, timeout=60).stdout.split())
    except Exception as exc:                                # noqa: BLE001
        logger.warning("sync %s: could not list merged files: %s", path, exc)
        return
    # AND THEIR DIRECTORIES, up to the repo root: a directory an earlier
    # merge created at 0755 lets the sandbox edit a file in place but not
    # create a new one beside it, or replace one by delete-and-recreate.
    root = os.path.abspath(path)
    targets = set()
    for name in names:
        target = os.path.abspath(os.path.join(path, name))
        targets.add(target)
        parent = os.path.dirname(target)
        while parent.startswith(root + os.sep):
            targets.add(parent)
            parent = os.path.dirname(parent)
    for target in sorted(targets):
        try:
            if os.path.isfile(target) or os.path.isdir(target):
                mode = stat.S_IMODE(os.stat(target).st_mode)
                os.chmod(target, mode | stat.S_IWGRP | stat.S_IRGRP
                         | (stat.S_IXGRP if os.path.isdir(target) else 0))
        except OSError as exc:
            logger.warning("sync %s: %s not made group-writable: %s",
                           path, target, exc)


def _sync_one(subprocess, path: str, lane: str) -> dict:
    """Fetch the lane and merge it. Never raises."""
    try:
        got = subprocess.run(
            ["git", "-C", path, "fetch", "origin",
             f"{lane}:refs/remotes/origin/{lane}"],
            capture_output=True, text=True, timeout=300)
        if got.returncode != 0:
            # A FETCH THAT DID NOT LAND SAYS NOTHING ABOUT THE MERGE, and
            # merging a stale ref here would report success over a branch
            # that is still behind.
            return {"state": "error",
                    "detail": f"could not fetch {lane}: "
                              f"{(got.stderr or '')[-400:]}"}
        ref = f"origin/{lane}"
        behind = subprocess.run(
            ["git", "-C", path, "rev-list", "--count", f"HEAD..{ref}"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        if behind == "0":
            return {"state": "already", "detail": f"up to date with {lane}",
                    "behind": 0}
        merged = subprocess.run(
            ["git", "-C", path, "merge", "--no-edit", ref],
            capture_output=True, text=True, timeout=300,
            **_group_writable_umask())
        # THE MODEL HAS TO BE ABLE TO EDIT WHAT THE MERGE WROTE. This runs
        # in the WORKER (uid 65531); `write`/`edit` run in the SANDBOX
        # (65532), sharing group 65532 (`k8s_spec.WORKSPACE_GID`). Under
        # the default 0022 umask every file git rewrote came out 0644 --
        # group-readable, not group-writable -- so a conflicted README
        # could be read and never resolved: E_PERMISSION on BMIF1 run 4,
        # and on run 3 a "resolution" that never landed, so the markers
        # were committed and merged to trunk. Same two-uid fact as
        # `loop_progress._make_it_the_models_to_write`.
        _make_merged_files_group_writable(subprocess, path)
        if merged.returncode == 0:
            return {"state": "merged", "behind": int(behind or 0),
                    "detail": (merged.stdout or "")[-2000:]}
        conflicts = subprocess.run(
            ["git", "-C", path, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, timeout=60).stdout.split()
        said = ((merged.stdout or "") + (merged.stderr or ""))[-2000:]
        return {"state": "conflict", "behind": int(behind or 0),
                "conflicts": conflicts, "detail": said}
    except Exception as exc:                                # noqa: BLE001
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}


OPS = {"read": op_read, "commit": op_commit, "archive": op_archive,
       "heads": op_heads, "publish": op_publish, "changed": op_changed,
       "sync": op_sync,
       "explore": op_explore, "plan": op_plan, "digest": op_digest,
       "archive_mounts": op_archive_mounts,
       "credentials": op_credentials,
       "revoke_credentials": op_revoke_credentials}


def _mounts(rows):
    from kernel.workspace.mounts import Mount

    return tuple(
        Mount(at=r.get("at", ""), binding=r.get("binding", "none"),
              source=r.get("source") or "", ref=r.get("ref") or "",
              writable=bool(r.get("writable")), kind=r.get("kind") or "")
        for r in rows)


def handle(payload: dict) -> dict:
    op = OPS.get(payload.get("op") or "")
    if op is None:
        return {"error": f"E_UNKNOWN_OP: {payload.get('op')!r}; "
                         f"expected one of {', '.join(sorted(OPS))}"}
    try:
        return op(payload)
    except Exception as exc:                                # noqa: BLE001
        # NEVER A TRACEBACK ACROSS THE WIRE. The caller turns this into a
        # ToolResult a model reads; a stack trace tells it nothing it can
        # act on and leaks paths from a container it cannot see.
        logger.exception("podfs: %s", payload.get("op"))
        return {"error": f"E_WORKSPACE_OP_FAILED: {type(exc).__name__}: {exc}"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                      # noqa: N802
        # EVERY OP, WITH NO EXEMPTION. The gate is here and not in
        # `handle`, above the dispatch table, so a tenth operation
        # cannot arrive unguarded: there is no per-op decision to
        # forget. That is the shape of the defect being closed --
        # `read` was the only op once, and six more were added one at a
        # time without anybody asking again who could call them.
        #
        # THE BODY IS DRAINED WHATEVER THE ANSWER. Closing an HTTP/1.0
        # connection with the request body still unread reaches urllib
        # as a reset, and a caller that cannot read the 401 cannot tell
        # a refusal from a dead pod. Nothing is PARSED before the check
        # and no operation runs.
        from kernel.workspace import podfs_stream        # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        scope = podfs_auth.scope(self.headers.get(podfs_auth.HEADER))
        if scope is None:
            logger.warning("podfs: refused an unauthorised %s",
                           self.command)
            self._send(401, {"error": UNAUTHORISED})
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            self._send(200, {"error": f"E_BAD_REQUEST: {exc}"})
            return
        # THE SCOPE GATE SITS ABOVE THE DISPATCH TABLE, for the reason
        # the token check does: there is no per-op decision to forget,
        # so a twelfth operation cannot arrive unguarded. Parsing is not
        # running -- no operation has happened yet.
        if not podfs_auth.permits(scope, payload.get("op") or ""):
            logger.warning("podfs: a %s caller asked for %r", scope,
                           payload.get("op"))
            self._send(403, {"error": NARROW})
            return
        # THE ONE ROUTE WHOSE BODY IS NOT JSON. A tar base64'd into a
        # reply is four copies of the tree between the two processes,
        # which a `large` tier makes fatal. podfs_stream states it.
        if self.path == podfs_stream.PATH:
            podfs_stream.respond(self, payload)
            return
        self._send(200, handle(payload))

    def do_GET(self):                                       # noqa: N802
        # `/health` IS THE ONE EXEMPT PATH, and the exemption is
        # deliberate rather than left over. Three things bound it:
        #
        #   * A LIVENESS ANSWER CANNOT HOLD A SECRET. If a kubelet probe
        #     is ever put on 9101 it is configured in a pod manifest and
        #     has no way to present a token derived per execution from
        #     `AGENT_TOKEN_SIGNING_KEY`. Nothing probes this port today,
        #     and the day something does the token would be the thing
        #     that broke it -- as a restart loop on a healthy pod.
        #   * IT DISCLOSES ONE BIT THE SANDBOX ALREADY HAS. "A server is
        #     listening" is visible to anything in this network
        #     namespace from `ss -ltn`; a token here would guard nothing.
        #   * IT CANNOT GROW AN OPERATION. Every op is a POST and this
        #     verb has no dispatch at all, so widening the exemption
        #     would take moving an op to GET, which is a visible change.
        #
        # The bridge keeps its own `/health` outside the tool lock for
        # the analogous reason -- see kernel/bridge/shim.py, where a
        # liveness probe queued behind an `env_setup` had pods destroyed
        # under healthy runs.
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *a):                         # noqa: A003
        logger.info("podfs %s", fmt % a)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # REFUSE TO START, BEFORE A SOCKET EXISTS.
    #
    # The tempting alternative -- check the header only when a token
    # happens to be configured -- reproduces this exact bug, silently,
    # in whichever environment forgot to set the variable: the surface
    # comes up wide open and nothing anywhere says so. A pod that will
    # not start is a CrashLoopBackOff somebody reads within the hour.
    #
    # It costs the local substrates nothing. `is_pod(driver)` is False
    # on Docker and E0, where this module is never a container command:
    # `k8s_workspace.py` is the only thing that runs it, and the same
    # manifest that runs it injects the variable.
    podfs_auth.configured_token()
    argv = list(sys.argv[1:] if argv is None else argv)
    bind = DEFAULT_BIND
    if "--bind" in argv:
        index = argv.index("--bind")
        if index + 1 < len(argv):
            bind = argv[index + 1]
    # THREADED, FOR THE SAME REASON THE BRIDGE IS. This was
    # `HTTPServer`: one request served to completion before the next is
    # read. `archive_mounts` tars an entire workspace and
    # `op_credentials` writes a lease -- and while either ran, every
    # other caller queued behind it: the file explorer showing a person
    # their tree, the adjudicator asking for a digest, the credential
    # sweep. `podfs_client.TIMEOUT` is 120s, so a slow archive would not
    # merely delay them, it could fail them.
    #
    # The bridge's lesson one port over, and it arrived here the moment
    # this server grew operations that are not near-instant.
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((bind, WORKER_PORT), Handler)
    print(f"podfs listening on {bind}:{WORKER_PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
