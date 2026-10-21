"""The executor's files, addressed the way the rest of the world addresses them.

WHY THIS IS NOT THE CONSOLE'S JOB. The chat page's file explorer used to
call the E0 console, which takes an EXECUTION id and parses it with
`int()`. The page has a STORY id, so every open of the explorer during
the first live story raised `invalid literal for int() with base 10:
'de5512b2'` and the panel showed `Failed to load: 502 Bad Gateway`.

Two things were wrong and only one of them was the int(). The console
exists to drive tests -- it is not a service the product may depend on,
and pointing the control panel at it made a test harness load-bearing.
So the read moves here, onto the kernel's own surface, and it takes the
identifier the caller actually holds.

A STORY, NOT A RUN. A story may be attempted more than once; the files
a person wants are the ones from the attempt that is live now, or the
last one if none is. Resolving that here means no caller has to know
about generations.

AND ON A POD SUBSTRATE THE FILES ARE NOT HERE. `explorer.py` is
`os.walk`, `open()` and `git status` against a path, and on Kubernetes
that path names a ReadWriteOncePod claim owned by the execution's own
pod — so the walk finds nothing and the panel reports an empty
workspace for a story that is mid-edit. The read crosses to the pod's
WORKER, which runs the same explorer functions (P3/D5).

THE WORKER AND NOT THE SANDBOX, and that is not an implementation
convenience: the sandbox is the container the model controls, and a
panel whose contents the model can shape is not a view of the truth.
"""

from __future__ import annotations

import asyncio
import functools

from kernel.workspace import explorer as ws


def local(what: str, root: str, **args):
    """The three reads the panel makes, on this filesystem.

    `podfs.op_explore` answers to the same three names with the same
    argument names, which is what lets one call site serve both ends
    with no translation table in between — and a table is exactly what
    this must not become: a dict of bound functions built at import
    would keep serving whatever `explorer` exported at that moment,
    which is the divergence the shared naming exists to prevent.
    """
    return {"tree": ws.tree, "read": ws.read, "diff": ws.diff}[what](
        root, **args)


async def workspace_host(conn, execution_id: int, driver=None) -> str:
    """Where this execution's tree is. EMPTY MEANS "on this filesystem".

    The same convention `ExecutionEnvelope.workspace_host` uses, and
    empty is not "unknown": Docker and E0 compose the workspace in this
    process and every read below takes the path it always took.

    THE CLASS, NOT AN INSTANCE, until there is something to ask. Reading
    a class attribute must not construct a substrate client — on Docker
    there is no question to put to one, and browsing a local workspace
    is a read of a directory.

    THE ROW FIRST, BECAUSE THE PROCESS IS THE WRONG PLACE TO ASK.
    `registry.resolve()` reads AGENTOS_DRIVER from whatever process is
    running, and only the conductor's deployment sets it. The panel is
    served by the control panel on Cloud Run -- a different deployment
    -- so asking the registry there answers "docker" for a Kubernetes
    execution and walks an empty container, presenting it as the truth.
    An empty panel that looks authoritative is worse than an error.

    So the conductor writes `executions.workspace_host` at dispatch,
    where the address was actually resolved, and this reads it. The
    registry stays as the fallback for a row written before the column
    existed, and `driver=` as the seam for a caller that knows better.
    """
    from kernel.workspace.podfs_client import WorkspaceUnreachable, is_pod

    recorded = await conn.fetchval(
        "SELECT workspace_host FROM executions WHERE id = $1", execution_id)
    if recorded:
        return recorded

    if driver is None:
        from kernel.drivers.registry import resolve
        driver = resolve()
    if not is_pod(driver):
        return ""

    cid = await conn.fetchval(
        "SELECT container_id FROM executions WHERE id = $1", execution_id)
    if not cid:
        raise WorkspaceUnreachable(
            "this run has no pod, so its workspace cannot be read — it "
            "was never dispatched, or it finished and the claim was "
            "released with it. That is a different fact from an empty "
            "workspace and the panel must not show it as one.")
    owned = driver() if isinstance(driver, type) else None
    try:
        host = await (owned or driver).resolve_bridge_host(cid)
    except Exception as exc:                                # noqa: BLE001
        # ANY failure to resolve becomes the refusal, because the
        # alternative is what this module was written to end: an
        # unhandled exception reaching the panel as "Failed to load:
        # 502 Bad Gateway", which says nothing a person can act on.
        raise WorkspaceUnreachable(
            f"the pod holding this workspace ({cid}) could not be "
            f"reached: {exc}") from exc
    finally:
        if owned is not None:
            await owned.aclose()
    if not host:
        # EMPTY MEANS LOCAL in `explore` below, so a driver answering ""
        # for a pod it could not find would send this read to the
        # conductor's own filesystem and report the empty directory it
        # finds there as an empty workspace.
        raise WorkspaceUnreachable(
            f"the substrate gave no address for pod {cid}")
    return host


async def explore(host: str, what: str, root: str, *, execution_id: int | None,
                  **args):
    """One explorer read, wherever the tree is.

    OFF THE EVENT LOOP when it crosses. `podfs_client` is a blocking
    urlopen with a two-minute timeout and this is awaited inside the
    process serving the panel, so a slow pod would stall every other
    request rather than the one waiting on it.

    `execution_id` is required WITH `host` and unused without it: podfs
    authenticates per execution, so an address alone opens nothing. None
    is correct for a local tree and only for a local tree.
    """
    if not host:
        return local(what, root, **args)
    from kernel.workspace.podfs_client import explore as remote

    return await asyncio.to_thread(
        functools.partial(remote, host, what, root,
                          execution_id=execution_id, **args))


async def resolve_execution(conn, story_id: str) -> int | None:
    """The attempt whose files a person means. Live first, else latest."""
    if not story_id:
        return None

    # ASK THE STORY FIRST, always. This used to short-circuit on
    # `str(story_id).isdigit()` -- "a bare integer IS an execution id", which
    # the console relies on -- and a story id is EIGHT HEX CHARACTERS, so
    # roughly one in forty-three of them is all decimal digits. Story 52793688
    # was read as execution 52793688, which does not exist, and the file
    # explorer answered "this story has no workspace yet" for a run that had
    # one. Intermittent by construction: the story beside it, 648f26a1, worked
    # perfectly, so the bug looks like flakiness rather than a rule.
    #
    # Order fixes it without breaking the console: an id that names a story
    # resolves as a story, and only one that names none is tried as an
    # execution. The two id spaces cannot collide in that direction, because
    # an execution id is a bigserial and no story is created with one.
    got = await conn.fetchval(
        """
        SELECT e.id FROM executions e
         WHERE e.story_id = $1
         ORDER BY (e.container_id IS NOT NULL) DESC, e.id DESC
         LIMIT 1
        """, str(story_id))
    if got is not None:
        return got

    # No run for that story -- or it was an execution id all along.
    if str(story_id).isdigit():
        return int(story_id)
    return None


async def browse(conn, story_id: str, mount: str = "", rel: str = "",
                 want: str = "tree", driver=None) -> dict:
    """tree | file | diff for a story's mounts, flattened by the caller."""
    eid = await resolve_execution(conn, story_id)
    if eid is None:
        return {"story": story_id, "mounts": [],
                "detail": "no run for this story yet"}
    mounts = await ws.mounts_of(conn, eid)
    if not mounts:
        return {"story": story_id, "execution": eid, "mounts": [],
                "detail": "this story has no workspace yet"}

    # THE WRITABLE MOUNT BY DEFAULT. `mounts[0]` is whatever sorts
    # first, and once documents mount at `docs/<name>` that is a
    # document -- so the panel opened on a single uploaded file while
    # the work was in a repo further down the list. What somebody opens
    # this to see is where the work is happening.
    chosen = next((m for m in mounts if m["at"] == mount),
                  next((m for m in mounts if m.get("writable")),
                       mounts[0]))
    head = {"story": story_id, "execution": eid, "mount": chosen["at"],
            "mounts": [{"at": m["at"], "writable": m["writable"]}
                       for m in mounts]}

    # A REFUSAL CARRIES NO TREE. Every early return above hands back a
    # `detail` and no entries, and this keeps that shape: a payload with
    # `entries: []` renders as "no files in this workspace", which is
    # the one thing an unreachable pod must never be mistaken for.
    from kernel.workspace.podfs_client import WorkspaceUnreachable
    try:
        host = await workspace_host(conn, eid, driver)
        return await _read(host, head, mounts, chosen, rel, want, eid)
    except WorkspaceUnreachable as exc:
        return {**head, "error": str(exc)}


async def _read(host, head, mounts, chosen, rel, want, eid) -> dict:
    """The panel's answer, from whichever end holds the files."""
    # ── THE WHOLE WORKSPACE, which is what a person came to see ──────
    #
    # One mount at a time was the original shape and it made the panel
    # nearly useless the moment a story had more than one: three
    # documents and a repo meant four dropdown entries, three of them a
    # single file, and no way to see the story's work as a whole. A
    # person opening a workspace wants the workspace.
    #
    # Every mount, each labelled and marked writable or not, in the
    # order that reads: what the story WRITES first, then what it only
    # reads. Per-mount entry caps still apply, so one enormous checkout
    # cannot crowd the others out of the answer.
    if want == "all":
        parts, total, clipped = [], 0, False
        for m in sorted(mounts, key=lambda x: (not x["writable"], x["at"])):
            got = await explore(host, "tree", m["root"], execution_id=eid,
                                at=m["at"], writable=m["writable"])
            got["kind"] = m.get("kind") or ""
            total += len(got.get("entries") or [])
            clipped = clipped or bool(got.get("truncated"))
            parts.append(got)
        return {**head, "showing": "all", "trees": parts,
                "file_count": total, "truncated": clipped}

    if want == "file":
        return {**head, **await explore(host, "read", chosen["root"],
                                        execution_id=eid, rel=rel)}
    if want == "diff":
        return {**head, "diff": await explore(host, "diff", chosen["root"],
                                              execution_id=eid, rel=rel)}
    return {**head, "showing": chosen["at"],
            **await explore(host, "tree", chosen["root"], execution_id=eid,
                            at=chosen["at"], writable=chosen["writable"])}
