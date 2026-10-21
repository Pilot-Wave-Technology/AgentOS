"""Ec2c — `emit_artifact`: a deliberate publish, not the 32 KB accident.

WHAT WAS BACKWARDS. An artifact existed only because a tool output crossed
`INLINE_THRESHOLD`, so a 50 MB test log was durable and a 2 KB report was
not. For a story that is tolerable — the deliverable is a branch. For an
errand it is the whole problem: the scratch tree is discarded, and the
small careful thing is exactly what had to survive.

WHY NOT `publish_artifact`. That one requires the story to already hold the
target resource with a `produces` or `modifies` relation — the grant is the
authority, and rightly so, because it writes a new VERSION of something the
goal owns. An errand usually owns nothing. Granting it that tool would have
handed the model something that answers `E_NOT_GRANTED` every time, which
is worse than a tool that is absent.

So this writes to the execution's own manifest instead. No grant is needed
because nothing outside the run is being changed: a row saying "this run
produced this, and it is a report called X" is a claim about the run
itself. Promoting an artifact into a goal resource stays `publish_artifact`
and still needs the grant.

CHAINED AUTOMATICALLY. Run the same errand twice and you get two reports;
without a link the reader has two answers and no way to tell which is
current. The executor cannot supply `supersedes` itself — it would have to
know the previous run's sha256 — so the chain is derived from (goal, role,
name), which is what a person means by "the same report as last time". An
explicit `supersedes` overrides it, for the case where a model genuinely
knows better.
"""

from __future__ import annotations

import asyncio
import logging
import os

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)

ROLES = ("report", "dataset", "method", "log", "figure")

# Enough for a report, a dataset extract or a chart. The store would take
# 256 MB, but something that large is a repo or a bucket, not an answer —
# and it would have to be read into worker memory to hash.
MAX_BYTES = 32 * 1024 * 1024

# Extension → media type. Deliberately short: this is a hint for a reader,
# not content negotiation, and a wrong guess is worse than `text/plain`.
_MEDIA = {
    ".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
    ".json": "application/json", ".html": "text/html",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".py": "text/x-python", ".sql": "application/sql",
    ".yaml": "text/yaml", ".yml": "text/yaml",
}


def media_type_for(name: str) -> str:
    return _MEDIA.get(os.path.splitext(name)[1].lower(), "text/plain")


def _refuse(tool_call_id: str, message: str) -> ToolResult:
    return ToolResult(tool_call_id=tool_call_id, output="", error=message,
                      exit_code=1)


def read_in_scope(root: str, path: str) -> tuple[bytes, str]:
    """The file's bytes, or ("", reason). Refuses an escape from the root.

    `os.path.normpath` before the prefix check, and the separator in the
    prefix: without the separator `/workspace/exec-4` accepts
    `/workspace/exec-42`, which is another execution's tree.
    """
    root = os.path.normpath(root or "/workspace")
    full = os.path.normpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        return b"", ("E_ENVELOPE_VIOLATION: the path escapes the workspace")
    try:
        with open(full, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return b"", f"E_NO_FILE: cannot read {path!r}: {exc}"
    if len(data) > MAX_BYTES:
        return b"", (f"E_TOO_LARGE: {path!r} exceeds "
                     f"{MAX_BYTES // (1024 * 1024)}MB — an artifact that big "
                     f"belongs in a repo or a dataset store, not in a report")
    return data, ""


def read_workspace_file(envelope, path: str) -> tuple[bytes, str]:
    """The file's bytes, from wherever this execution's tree lives.

    THE SCOPE CHECK DOES NOT MOVE. `read_in_scope` runs at whichever end
    holds the tree, so a path that escapes the workspace is refused by
    the same function either way -- what changes is which filesystem it
    is refused against.

    A REFUSAL, NOT A FALLBACK, when the pod cannot be reached. Reading
    locally instead would find nothing and hand back ("", E_NO_FILE),
    which the model reads as "I wrote the report to the wrong path" and
    retries -- against a workspace this process was never going to see.
    """
    host = getattr(envelope, "workspace_host", "") or ""
    root = getattr(envelope, "workspace_path", "") or ""
    if not host:
        return read_in_scope(root, path)

    from kernel.workspace.podfs_client import (WorkspaceUnreachable,
                                               read_file)

    # The id travels WITH the host on the envelope, because podfs
    # authenticates per execution -- see ExecutionEnvelope. An envelope
    # carrying one and not the other is a bug in whoever built it, and
    # saying so here beats a 401 that reads as an unreachable pod.
    execution_id = getattr(envelope, "execution_id", None)
    if execution_id is None:
        return b"", (
            "E_WORKSPACE_UNREACHABLE: this envelope names a workspace "
            f"host ({host}) but no execution, so its workspace token "
            "cannot be derived")

    try:
        return read_file(host, root, path, execution_id=execution_id)
    except WorkspaceUnreachable as exc:
        return b"", f"E_WORKSPACE_UNREACHABLE: {exc}"


async def _prior(conn, execution_id: int, role: str, name: str) -> str:
    """The artifact this one supersedes, by (goal, role, name).

    Scoped to the GOAL rather than the execution: the point of the chain is
    to link a rerun to the run before it, and a rerun is a different
    execution. Falls back to the execution when the run has no goal, which
    is the console's ad-hoc case.

    ORDERED BY `id`, NOT `created_at`. `now()` in Postgres is the
    TRANSACTION's start time, so two emits in one transaction carry
    identical timestamps and `ORDER BY created_at` picks between them
    arbitrarily — which builds a chain that forks, and a reader walking it
    finds two heads. `id` is a BIGSERIAL and strictly increasing whatever
    the transaction boundaries are.
    """
    goal_id = await conn.fetchval(
        "SELECT goal_id FROM kernel.executions WHERE id = $1", execution_id)
    if goal_id:
        return await conn.fetchval(
            """
            SELECT m.artifact_id
              FROM kernel.artifact_manifest m
              JOIN kernel.executions e ON e.id = m.execution_id
             WHERE e.goal_id = $1 AND m.role = $2 AND m.name = $3
             ORDER BY m.id DESC LIMIT 1
            """, goal_id, role, name) or ""
    return await conn.fetchval(
        """
        SELECT artifact_id FROM kernel.artifact_manifest
         WHERE execution_id = $1 AND role = $2 AND name = $3
         ORDER BY id DESC LIMIT 1
        """, execution_id, role, name) or ""


class NotDurable(Exception):
    """This process cannot store bytes anywhere that outlives the run."""


async def _store_durably(data: bytes, execution_id: int) -> str:
    """The artifact's id, from a store that outlives this pod.

    TWO LINKS, IN THIS ORDER, and the order is the whole fix.

    THE BROKER FIRST, wherever there is one. `emit_artifact` runs in the
    leg, and since the leg-in-pod cutover the leg runs INSIDE the
    execution pod -- `kernel/worker/pod_main.py` is "the workspace
    surface AND the leg loop". That pod is handed
    ARTIFACT_STORE=filesystem rooted on its own workspace, deliberately:
    it is denied the metadata server, because its sandbox shares the
    network namespace and a credential there is one a model can mint.

    So the local put SUCCEEDED and was useless. It returned a real
    content hash, wrote the bytes to a PVC, and this function went on to
    catalogue a goal resource promising something durable and mountable
    into later runs. The pod ended and the bytes went with it: the
    artifact bucket took nothing between 2026-09-18 and the day this was
    written, while five artifacts across four stories were registered as
    though it had.

    THE LOCAL STORE SECOND, and only where there is no broker -- E0, a
    laptop, a test. There the filesystem store IS the durable one and
    the old behaviour is exactly right.

    NOT THE SAME DECISION AS `loop_tool`'s put, which stays local on
    purpose: that one spills oversized TOOL OUTPUT so a leg can
    range-read its own bytes, and its own comment says storing is an
    optimisation over truncating. Run-scoped is the honest limit there.
    Here it is a broken promise.
    """
    from kernel.artifacts.aio import aput
    from kernel.workspace import broker, podfs_auth

    url = broker.endpoint(broker.ARTIFACT_OBJECT, "ARTIFACT_OBJECT_URL")
    # THIS EXECUTION'S TOKEN, which the worker holds and the sandbox
    # does not. Not the gateway bearer: a token that opens every
    # internal route has no business in a pod whose sandbox shares its
    # network namespace.
    token = (os.environ[podfs_auth.ENV_VAR]
             if podfs_auth.ENV_VAR in os.environ else "")
    if url and token:
        return await asyncio.to_thread(
            _put_over_http, url, token, data, execution_id)

    # NO BROKER. Only defensible when this process's own store outlives
    # the run, which on a pod it does not.
    from kernel.artifacts.store import BINDING, describe

    if BINDING == "filesystem" and "AGENTOS_EXECUTION_ID" in os.environ:
        raise NotDurable(
            f"this leg can only write {describe()}, which is this pod's "
            f"own disk -- the bytes would not survive the run, and the "
            f"manifest row would promise that they had. Needs "
            f"CONTROL_PANEL_URL and {podfs_auth.ENV_VAR} so the "
            f"artifact can be stored by the control panel")
    # OFF THE LOOP. Under the filesystem binding a put is a local write
    # and this is free; against a bucket it is an upload, and blocking
    # here stops every other leg in the process for its duration.
    return await aput(data)


def _put_over_http(url: str, token: str, data: bytes,
                   execution_id: int) -> str:
    """POST the bytes to the control panel, which holds the credential.

    SYNCHRONOUS, and called through a thread for the reason the docstring
    above gives. `urllib` rather than a client library because the pod's
    image carries no cloud SDK and this road must work without one.
    """
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{url}?execution_id={execution_id}", data=data, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            answer = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 413:
            # THE SAME ERROR THE SAME ARTIFACT GETS ON E0. Locally an
            # oversized put raises ArtifactTooLarge and the executor is
            # told E_TOO_LARGE; through the panel the identical refusal
            # arrived as E_NOT_DURABLE, which reads as "the platform is
            # broken" rather than "your artifact is too big".
            #
            # This also catches a ceiling BELOW the endpoint's own: the
            # bytes cross an ingress that may refuse them before the
            # handler sees them. Whichever limit was hit, its own
            # message says which, and the code stays actionable.
            from kernel.artifacts.store import ArtifactTooLarge
            raise ArtifactTooLarge(
                f"the control panel would not store the artifact: "
                f"{detail}") from exc
        raise NotDurable(
            f"the control panel refused the artifact ({exc.code}): "
            f"{detail}") from exc
    except Exception as exc:                                # noqa: BLE001
        raise NotDurable(
            f"could not reach the control panel to store the artifact: "
            f"{exc}") from exc
    artifact_id = str(answer.get("artifact_id") or "")
    if not artifact_id:
        raise NotDurable(
            "the control panel stored the artifact and returned no id, "
            "so nothing can address it")
    return artifact_id


async def emit_artifact(conn, envelope, execution_id: int,
                        tool_args: dict, tool_call_id: str) -> ToolResult:
    role = str(tool_args.get("role") or "").strip().lower()
    path = str(tool_args.get("path") or "").strip()
    name = str(tool_args.get("name") or "").strip() or os.path.basename(path)
    summary = str(tool_args.get("summary") or "").strip()

    if not path or role not in ROLES:
        return _refuse(tool_call_id, (
            f"E_BAD_ARGS: emit_artifact needs `path` (workspace-relative) "
            f"and `role` (one of {', '.join(ROLES)}); got role={role!r}"))
    if not name:
        return _refuse(tool_call_id, (
            "E_BAD_ARGS: `name` is what a reader sees in a list, and the "
            "path was empty of one — pass it explicitly"))
    if not summary:
        # NOT COSMETIC. A list of hashes answers nothing, and the executor
        # is the only party that knows what it just produced. Asking later
        # means asking a leg that no longer exists.
        return _refuse(tool_call_id, (
            "E_BAD_ARGS: `summary` is required — one or two sentences on "
            "what this contains and what it shows. It is what makes the "
            "artifact findable without fetching the bytes."))

    # OFF THE LOOP: reads a file whose size is the artifact's business,
    # and on a pod substrate goes through podfs -- a synchronous urlopen.
    data, problem = await asyncio.to_thread(
        read_workspace_file, envelope, path)
    if problem:
        return _refuse(tool_call_id, problem)

    # Ec4d — AN ARTIFACT IS THE MOST DURABLE PLACE A SECRET CAN LAND.
    #
    # D2 changed the stakes here. An emitted artifact is no longer a file
    # in a workspace that dies with the run: it is content-addressed in
    # the store, catalogued as a goal resource with provenance, readable
    # by both agents, mountable into later runs, and downloadable by a
    # person over HTTP. A token that reaches this function outlives every
    # rotation and every workspace.
    #
    # REFUSED, NOT SCRUBBED. Silently altering the bytes would hand back
    # a report the executor believes it wrote and a reader believes was
    # produced by the run -- and the difference between them would be
    # invisible. A refusal that names the problem lets the executor
    # redact and emit again, which is the outcome we actually want.
    from kernel.workspace.credentials import holds_secret

    if holds_secret(data.decode("utf-8", "ignore")):
        return _refuse(tool_call_id, (
            "E_ARTIFACT_HOLDS_CREDENTIAL: this content contains something "
            "credential-shaped -- an access token, a private key, or a "
            "JWT -- and an artifact is permanent: content-addressed, "
            "catalogued as a goal resource, readable by later runs and "
            "downloadable by a person. Nothing was stored. Remove the "
            "secret and emit again. If the finding is ABOUT a credential, "
            "describe it without reproducing the value."))

    from kernel.artifacts.store import ArtifactTooLarge

    try:
        artifact_id = await _store_durably(data, execution_id)
    except ArtifactTooLarge as exc:
        return _refuse(tool_call_id, f"E_TOO_LARGE: {exc}")
    except NotDurable as exc:
        # REFUSED RATHER THAN REGISTERED, for the same reason the
        # credential check above refuses rather than scrubs: a manifest
        # row is a promise, and one nothing can keep is worse than no
        # row at all. See `_store_durably`.
        return _refuse(tool_call_id, f"E_NOT_DURABLE: {exc}")

    supersedes = str(tool_args.get("supersedes") or "").strip()
    if not supersedes:
        supersedes = await _prior(conn, execution_id, role, name)
    # An unchanged rerun produces the same bytes and so the same id. A row
    # pointing at itself would make the chain a loop and every reader that
    # walks it hang.
    if supersedes == artifact_id:
        supersedes = ""

    await conn.execute(
        """
        INSERT INTO kernel.artifact_manifest
            (execution_id, artifact_id, role, name, media_type, summary,
             supersedes, bytes)
        VALUES ($1, $2, $3, $4, $5, $6, NULLIF($7, ''), $8)
        ON CONFLICT (execution_id, artifact_id) DO UPDATE
            SET role = EXCLUDED.role, name = EXCLUDED.name,
                summary = EXCLUDED.summary,
                media_type = EXCLUDED.media_type
        """,
        execution_id, artifact_id, role, name,
        str(tool_args.get("media_type") or "").strip() or media_type_for(name),
        summary, supersedes, len(data))

    # D2 — AND THE GOAL NOW OWNS IT. The manifest makes the artifact
    # readable by AGENTS; the catalogue makes it grantable to later WORK.
    # Without this an errand's report can inform what GO plans and cannot
    # reach the run that acts on it, which is the chain the errand shape
    # exists for.
    #
    # Best-effort, deliberately: the manifest row above is what keeps the
    # work, and a catalogue write failing must not lose an artifact an
    # errand cannot produce again.
    # IN ITS OWN SAVEPOINT, and that is not belt-and-braces. A failed
    # statement POISONS the enclosing transaction in Postgres: catching
    # the Python exception does nothing about that, and every statement
    # after it dies with `InFailedSQLTransactionError`. So a catalogue
    # write that hit, say, a missing goal row would take the caller's
    # whole transaction with it -- and this is `emit_artifact`, where the
    # manifest row that keeps the work is written.
    #
    # Caught the first time by nineteen unrelated artifact tests failing
    # at once with "current transaction is aborted".
    resource_id = None
    try:
        from kernel.harness.artifact_resource import register

        async with conn.transaction():
            resource_id = await register(
                conn, execution_id, artifact_id, role, name, summary,
                str(tool_args.get("media_type") or "").strip()
                or media_type_for(name), len(data))
    except Exception as exc:                                # noqa: BLE001
        logger.warning("artifact %s emitted but not catalogued: %s",
                       artifact_id[:12], exc)

    logger.info("emit_artifact: %s %s (%d bytes) as %s%s%s", role, name,
                len(data), artifact_id[:12],
                f" superseding {supersedes[:12]}" if supersedes else "",
                f" catalogued as {resource_id}" if resource_id else "")
    return ToolResult(
        tool_call_id=tool_call_id,
        output=(f"emitted {name} as {role} ({len(data)} bytes), artifact "
                f"{artifact_id}."
                + (f" Supersedes {supersedes[:12]}." if supersedes else "")
                + " It is durable now — the workspace is not."
                + (f" It is also in the goal's catalogue as {resource_id}, "
                   f"so later work can be granted it by name."
                   if resource_id else "")),
        error=None, exit_code=0)
