"""Reaching a workspace that is not on this filesystem.

The conductor orchestrates a run whose tree lives in a pod. Everything
that needs BYTES goes through here; everything that needs the DATABASE
stays where it is. `kernel/workspace/podfs.py` is the other end and
explains why the split is by resource rather than by tool.

ONE SEAM, DEFAULTED TO LOCAL. `local()` is what Docker and E0 do today:
call the function directly, on a filesystem this process can see. There
is no client involved and no behaviour to change. A pod substrate
supplies an address and the same call goes over HTTP.

REFUSES RATHER THAN FALLING BACK. A pod-model execution whose address
cannot be resolved must not quietly run the operation locally: on the
conductor that would read an empty directory and report an empty
workspace, which is the silent-wrong-answer failure this whole stage
keeps finding. `emit_artifact` reading zero bytes and reporting success
is worse than `emit_artifact` refusing.

`execution_id` IS REQUIRED, AND KEYWORD-ONLY, ON EVERY CALL. The worker
now demands a per-execution token (kernel/workspace/podfs_auth.py) and
this side mints it -- `derive` is a pure function of the id, so the id
IS the credential's subject and there is nothing sensible to do without
it. It is keyword-only so no existing positional argument can silently
slide into it, and it has NO DEFAULT: a default would be one token
shared by every execution, which is precisely the property that makes a
leaked token worthless here.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from kernel.workspace.podfs import WORKER_PORT
from kernel.workspace.podfs_auth import (ENV_VAR, HEADER, configured_token,
                                         derive, derive_read_only,
                                         read_only_of)

logger = logging.getLogger(__name__)

# WHAT AN OPERATION IS ALLOWED TO TAKE, and it is not one number.
#
# A single 120s timeout covered both connecting and reading, so
# discovering that a pod no longer exists cost TWO MINUTES OF A THREAD.
# The credential sweep dials terminal executions -- whose pods reclaim
# has usually already destroyed -- every four seconds. Threads
# accumulated faster than they drained, the pool emptied, every k8s and
# podfs call queued behind it, no pass finished, no heartbeat was
# written, and the liveness probe killed a process whose event loop was
# idle. Caught by the stack dump the probe now takes on its way out.
#
# `k8s_call` learned this the same day and states it plainly: a scalar
# timeout leaves the dropped-SYN case hanging. urllib takes one number
# for both, so the split here is BY OPERATION instead -- which is the
# honest version anyway, because `archive` legitimately takes minutes
# and `revoke_credentials` legitimately takes milliseconds.
TIMEOUT = 120

# Small, in-cluster, request/response. A pod that has not answered in
# this long is not slow, it is gone.
QUICK_TIMEOUT = 10

# Operations whose size is the WORK's business rather than the network's:
# a tree, a prepared mount, a dependency directory.
_SLOW_OPS = frozenset({"archive", "archive_mounts", "publish", "commit",
                       "plan", "explore"})


def timeout_for(op: str) -> int:
    """How long `op` may take. Quick unless it moves a tree.

    `archive` keeps its 120s here for the callers that still want the
    tar as `bytes`. `archive_to_file` does NOT use this number: a
    STREAMED read is a different question from a connect, and urllib
    cannot split them -- see kernel/workspace/podfs_stream.py, which
    uses `http.client` so it can give the handshake ten seconds and the
    transfer a per-read idle timeout plus a wall-clock cap.
    """
    return TIMEOUT if op in _SLOW_OPS else QUICK_TIMEOUT


def token_for(execution_id: int, read_only: bool) -> str:
    """The credential this call presents.

    READ-ONLY IS AN OPT-IN AND NOT A DEFAULT, because a caller that
    quietly loses `publish` is a story that quietly fails to deliver --
    fault 3 and fault 6 were both that. A caller asks for the narrow
    token when it knows it only reads; the verification pod always does.
    """
    # THE WORKER HAS THE ANSWER AND NOT THE KEY, and `derive` says so in
    # its own docstring: "Called on the CONDUCTOR side... The worker
    # never calls this." `read_only_of` says the same from the other
    # end -- "the conductor from `derive`, the worker from the value it
    # was given". Both halves were written; nothing joined them, because
    # when they were written the only podfs client WAS the conductor.
    #
    # THE LEG-IN-POD CUTOVER MADE THE WORKER A CLIENT. `publish_execution`
    # now runs in the pod's worker container and dials podfs over
    # loopback, and this asked for a signing key that is mounted from
    # `conductor-secrets` and deliberately absent from the pod --
    # `POD_ENV_ALLOWLIST` is a closed list and carries no credentials.
    # So every publish from a leg raised `TokenMissing`, the claim was
    # recorded and never delivered, and the run bounced on
    # `publish failed` until it ran out of legs. Measured on execution
    # 223.
    #
    # The pod already HOLDS its token -- `AGENTOS_PODFS_TOKEN`, injected
    # into the worker container so podfs can tell it from the sandbox
    # sharing its network namespace. Presenting that is both correct and
    # narrower than shipping a signing key that mints tokens for every
    # execution.
    # FOR THIS POD'S OWN EXECUTION AND NO OTHER. The held token belongs
    # to one execution, so presenting it for any other id would hand back
    # a token that does not match and, worse, would make a call ABOUT
    # another execution look authentic to a server that shares this one's
    # secret. `test_a_token_for_another_execution_is_not_recognised_at
    # _all` caught exactly that: a first cut of this returned the held
    # token whatever was asked for, and a 401 became a 200.
    #
    # A conductor talks to many executions and holds no token, so it
    # falls through to `derive` as it always did. A pod talks about
    # itself, and `AGENTOS_EXECUTION_ID` is what says which self.
    if (ENV_VAR in os.environ and in_execution_pod()
            and str(execution_id) == os.environ[EXECUTION_POD_ENV].strip()):
        token = configured_token()
        return read_only_of(token) if read_only else token
    return (derive_read_only(execution_id) if read_only
            else derive(execution_id))


class WorkspaceOpFailed(RuntimeError):
    """The pod ANSWERED, and the answer was a failure.

    NOT `WorkspaceUnreachable`, and conflating them cost a wrong
    diagnosis the first time this fired. A real push reached GitHub and
    was refused, and the operator was told

        publish failed: the workspace pod could not be reached

    which is false -- the pod was reached, ran git, and reported exactly
    what went wrong. The same distinction `call` already draws between
    an HTTP refusal and an unreachable host, one layer down: "a refusal
    is not an unreachable pod, and reporting it as one costs an
    afternoon".
    """


class WorkspaceUnreachable(RuntimeError):
    """The tree is in a pod this process cannot reach."""


def is_pod(driver) -> bool:
    """Is this execution's tree inside a pod?

    ASKS A NARROWER QUESTION THAN ITS CALLERS WANT. Nine call sites use
    it to decide whether to CROSS -- to ask the cluster where the pod is
    and then speak podfs to it. That is a different question, and
    `tree_is_remote` below is the one they mean.
    """
    return getattr(driver, "WORKSPACE_MODEL", "local") == "pod"


# ── which SIDE of the pod boundary this process is on ──────────────────
#
# THE MARKER, and it is the pod's alone. `k8s_spec.pod_manifest` puts
# `AGENTOS_EXECUTION_ID` on the worker container; neither the
# conductor's Deployment nor the verifier's carries it, and the pod's
# entire environment is a closed allowlist, so this cannot arrive by
# accident.
EXECUTION_POD_ENV = "AGENTOS_EXECUTION_ID"


def in_execution_pod() -> bool:
    """Is THIS process running inside an execution pod?

    `in` rather than `os.environ.get(x, default)` -- the B1 rule, whose
    reason is that a default nobody can see is a policy nobody reviewed.
    """
    return EXECUTION_POD_ENV in os.environ


def tree_is_remote(driver=None) -> bool:
    """Must this process cross a network to reach the execution's tree?

    WHAT `is_pod` WAS BEING ASKED, and it answered a different question
    for two years without anybody noticing, because until the cutover
    every caller was outside the pod and the two answers agreed.

    THEY INVERT INSIDE THE POD. `is_pod` says "the tree is in a pod", and
    a leg running IN that pod reads it as "so cross to it" -- whereupon
    it asks the Kubernetes API where it itself is (routed through the
    egress proxy, which answers `Tunnel connection failed: 403`, because
    `agentos-execution` is bound to no Role at all), and then makes an
    HTTP call it does not need to a filesystem already mounted in its own
    container at `WORKSPACE_PATH`.

    MEASURED, in two shapes, both on the leg's path:

        worker/loop.py     `resolve_bridge_host` -> 403, leg dead
        harness/planfiles  caught, logged at WARNING, returns []
                           -- the plan file silently never written

    The second is the worse one. It degrades quietly by design ("a
    courtesy copy must not block an approval"), so the failure is a story
    whose plan is simply absent.
    """
    if in_execution_pod():
        # The PVC is mounted in this very container and the bridge
        # shares this pod's network namespace. Nothing to cross.
        return False
    if driver is None:
        from kernel.drivers.registry import resolve         # noqa: PLC0415

        driver = resolve()
    return is_pod(driver)


# NEVER THROUGH A PROXY, and this is not a preference.
#
# podfs answers on an in-cluster address -- the pod's own IP, or loopback
# now the leg runs beside it -- and `k8s_egress` says pod-to-pod "is not
# the proxy's business either". It relied on `NO_PROXY` carrying
# `10.0.0.0/8` to arrange that, which works for anything reading that
# list as a CIDR and NOT for `urllib`, whose bypass matches hostnames and
# suffixes. A pod IP therefore goes to squid, squid refuses a private
# destination, and the answer is HTTP 403.
#
# THAT 403 IS THEN MISREAD, which is what made it expensive. `call`
# translates any 403 into "that token may read this workspace and not
# change it" -- a scope refusal -- so a proxy denial arrives dressed as
# an authorisation bug and sends the reader to `podfs_auth.py`. It is not
# reachable there: `heads` is in READ_ONLY_OPS and a full token permits
# everything, so podfs's own gate cannot refuse it at all.
#
# MEASURED ON EXECUTION 224: publish failed three generations running on
# `'heads' was REFUSED ... HTTP 403`. The conductor never hit it because
# the conductor has no proxy variables at all -- the same reason
# `k8s_egress` records that the metadata-server outage "only ever bit the
# Jobs". The leg-in-pod cutover made a proxied process the client.
#
# An empty ProxyHandler settles it at the one place that dials podfs,
# rather than depending on every future caller's environment.
_DIRECT = build_opener(ProxyHandler({}))


def call(host: str, op: str, *, execution_id: int, read_only: bool = False,
         **args) -> dict:
    """One operation against the container that holds the workspace."""
    if not host:
        raise WorkspaceUnreachable(
            f"no address for the pod holding this workspace, so {op!r} "
            f"cannot run. Running it here would read the conductor's own "
            f"filesystem and report an empty workspace as an empty result.")
    payload = json.dumps({"op": op, **args}).encode()
    # `TokenMissing` PROPAGATES rather than becoming `WorkspaceUnreachable`.
    # They are different facts and the callers above treat the second one
    # as "the pod is gone" and degrade quietly -- the credential sweep
    # returns False, `tree_state` returns no tree. An unset
    # AGENT_TOKEN_SIGNING_KEY is a conductor that can talk to NO
    # execution's workspace, and it must not arrive looking like one
    # unlucky pod.
    request = Request(f"http://{host}:{WORKER_PORT}/",
                      data=payload,
                      headers={"Content-Type": "application/json",
                               HEADER: token_for(execution_id, read_only)})
    try:
        with _DIRECT.open(request, timeout=timeout_for(op)) as response:
            body = json.loads(response.read() or b"{}")
    except HTTPError as exc:
        # A REFUSAL IS NOT AN UNREACHABLE POD, and reporting it as one
        # costs an afternoon: the message would send somebody to look at
        # the network for a mismatched token. Caught before `URLError`
        # because it is a subclass of it.
        # 401 AND 403 ARE DIFFERENT DIAGNOSES and the message has to say
        # which, or a scope refusal sends somebody to look at the network
        # for a mismatched token. E5's read-only scope made 403 reachable.
        why = ("that token may read this workspace and not change it"
               if exc.code == 403 else
               f"the token this conductor derived for execution "
               f"{execution_id} is not the one that pod was given")
        raise WorkspaceUnreachable(
            f"{op!r} was REFUSED by the workspace at {host}: HTTP "
            f"{exc.code}. {why} -- see "
            f"kernel/workspace/podfs_auth.py.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise WorkspaceUnreachable(
            f"{op!r} could not reach the workspace at {host}: {exc}") from exc
    return body


def read_file(host: str, root: str, path: str, *,
              execution_id: int, read_only: bool = False) -> tuple[bytes, str]:
    """Bytes of one file in the pod's tree. Same contract as
    `kernel.harness.emit.read_in_scope`: (bytes, problem)."""
    body = call(host, "read", root=root, path=path,
                execution_id=execution_id, read_only=read_only)
    if body.get("error"):
        return b"", body["error"]
    return base64.b64decode(body.get("bytes") or ""), ""


def archive(host: str, repo: str, commit: str, prefix: str = "verify/",
            uid: int = 0, exclude=None, *, execution_id: int) -> bytes:
    body = call(host, "archive", repo=repo, commit=commit, prefix=prefix,
                uid=uid, exclude=list(exclude or ()),
                execution_id=execution_id)
    if body.get("error"):
        raise WorkspaceUnreachable(body["error"])
    return base64.b64decode(body.get("bytes") or "")


def archive_to_file(host: str, repo: str, commit: str, dst: str,
                    prefix: str = "verify/", uid: int = 0, exclude=None, *,
                    execution_id: int, read_only: bool = True) -> int:
    """`archive`, streamed onto this process's disk. Bytes written.

    THE STREAMING FORM, and the one a tier above `small` needs. The
    byte-returning `archive` above holds the whole tree twice on each
    side of the wire -- fine at the 45MB measured on execution 210 and
    fatal on a 50GiB volume, where it is not slow but OOM-killed, and an
    OOM lands as `error`, which loops back to the executor to repair
    code that was never broken. Plan 9.6.

    READ-ONLY BY DEFAULT here and nowhere else: pulling a tree is the
    one operation whose caller is, by design, something that must never
    be able to `publish` -- a criterion that could write to the pod
    could rewrite the tree it is checking. Section 5.5.
    """
    from kernel.workspace import podfs_stream               # noqa: PLC0415

    return podfs_stream.fetch(
        host, {"op": "archive", "repo": repo, "commit": commit,
               "prefix": prefix, "uid": uid, "exclude": list(exclude or ())},
        dst, token=token_for(execution_id, read_only))


def commit(host: str, root: str, mounts, args: dict, call_id: str, *,
           execution_id: int) -> dict:
    rows = [{"at": m.at, "binding": m.binding, "source": m.source,
             "ref": m.ref, "writable": bool(m.writable), "kind": m.kind}
            for m in mounts]
    return call(host, "commit", root=root, mounts=rows, args=args,
                id=call_id, execution_id=execution_id)


def _or_raise(body: dict, op: str) -> dict:
    """An error reply is a FAILURE, not a result. Measured on 203.

    `call` returns whatever the pod said, and for `commit` that is right
    -- its error becomes the tool's error and reaches the model. For
    these two it is catastrophic, because the caller reads a LIST out of
    the reply and an error dict has no list in it:

        {"error": "E_UNKNOWN_OP: 'publish'; expected one of ..."}

    `publish_execution` did `got.get("refs") or []`, got an empty tuple,
    found no absent mounts in it, and returned (True, "nothing to
    push"). Execution 203 was marked DONE with its commit still sitting
    in a pod and the remote branch untouched -- the exact false delivery
    `composite.publish` has a scar comment about, reintroduced one layer
    up. It used to REFUSE this case correctly.

    The reply came from a pod created BEFORE the deploy that added these
    ops, so its podfs did not have them. That trigger is transient; the
    swallowed error is not.
    """
    if body.get("error"):
        raise WorkspaceOpFailed(f"{op!r} failed in the workspace pod: "
                                f"{body['error']}")
    return body


def heads(host: str, root: str, mounts, *, execution_id: int) -> dict:
    """{at: branch} for the writable git mounts, read in the pod."""
    return _or_raise(call(host, "heads", root=root, mounts=_rows(mounts),
                          execution_id=execution_id), "heads")


def changed(host: str, root: str, mounts, lanes: dict, *,
            execution_id: int) -> dict:
    """{at: "diff --stat text"} against each mount's lane merge-base.

    READ-ONLY, so it travels on the read-only token the Verifier holds:
    it runs `git merge-base` and `git diff --stat` and returns text.
    """
    return _or_raise(call(host, "changed", root=root, mounts=_rows(mounts),
                          lanes=lanes, execution_id=execution_id,
                          read_only=True), "changed")


def sync(host: str, root: str, mounts, lanes: dict, *,
         execution_id: int) -> dict:
    """Fetch each mount's trunk lane and merge it in. `{at: {state,...}}`.

    NOT READ-ONLY, unlike `changed`, which takes the same `lanes` and is
    the reason to say so here. This one fetches -- it needs the
    credential the sandbox deliberately does not have -- and it writes
    the working tree. It travels on the execution's own token.
    """
    return _or_raise(call(host, "sync", root=root, mounts=_rows(mounts),
                          lanes=lanes, execution_id=execution_id), "sync")


def publish(host: str, root: str, mounts, *, execution_id: int) -> dict:
    """Push the writable mounts from the container that holds them."""
    return _or_raise(call(host, "publish", root=root, mounts=_rows(mounts),
                          execution_id=execution_id), "publish")


def _rows(mounts):
    return [{"at": m.at, "binding": m.binding, "source": m.source,
             "ref": m.ref, "writable": bool(m.writable), "kind": m.kind}
            for m in mounts]


def archive_mounts(host: str, root: str, mounts, prefix: str = "verify/",
                   uid: int = 0, *, execution_id: int) -> bytes:
    """The whole workspace -- git AND copied-in documents -- as one tar."""
    import base64

    out = call(host, "archive_mounts", root=root, mounts=_rows(mounts),
               prefix=prefix, uid=uid, execution_id=execution_id)
    if out.get("error"):
        raise WorkspaceUnreachable(out["error"])
    return base64.b64decode(out.get("result") or "")


def credentials(host: str, root: str, lease_id: str, payload: dict,
                sidecars: dict | None = None, *,
                execution_id: int) -> list:
    """Write a cloud lease into the pod's workspace."""
    out = call(host, "credentials", root=root, lease_id=lease_id,
               payload=payload, sidecars=sidecars or {},
               execution_id=execution_id)
    if out.get("error"):
        raise WorkspaceUnreachable(out["error"])
    return out.get("result") or []


def revoke_credentials(host: str, root: str, lease_id: str, *,
                       execution_id: int) -> bool:
    out = call(host, "revoke_credentials", root=root, lease_id=lease_id,
               execution_id=execution_id)
    if out.get("error"):
        raise WorkspaceUnreachable(out["error"])
    return bool(out.get("result"))


def plan(host: str, root: str, mounts, story: str, cycle: int,
         body: str, *, execution_id: int) -> list:
    """Write the plan files in the pod. Returns the relative paths."""
    out = call(host, "plan", root=root, mounts=_rows(mounts), story=story,
               cycle=cycle, body=body, execution_id=execution_id)
    if out.get("error"):
        raise WorkspaceUnreachable(out["error"])
    return out.get("result") or []


def digest(host: str, root: str, mounts, *, execution_id: int) -> dict:
    """{'head': sha|None, 'digest': [[at, sha], ...]} from the pod."""
    out = call(host, "digest", root=root, mounts=_rows(mounts),
               execution_id=execution_id)
    if out.get("error"):
        raise WorkspaceUnreachable(out["error"])
    return out.get("result") or {}


def explore(host: str, what: str, root: str, *, execution_id: int,
            **args) -> dict:
    body = call(host, "explore", what=what, root=root,
                execution_id=execution_id, **args)
    if body.get("error"):
        raise WorkspaceUnreachable(body["error"])
    return body.get("result")
