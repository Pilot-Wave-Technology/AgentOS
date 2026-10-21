"""The control panel's file explorer, served from the kernel IN PROCESS.

The chat page already has the surface: a tree on the right, a file
viewer under it, both hitting
`/goals/{pid}/stories/{sid}/workspace/{tree,file}`. Those endpoints
walked the ASSISTANT's own directory over the gateway RPC, which worked
only because the old assistant was an operator holding the story
workspace. Under the kernel it holds none -- the files are the
executor's.

WHO CAN ACTUALLY SEE THE FILES. Not this process. The executor's tree
lives on a volume mounted into the kernel's container; the control
panel runs beside it, not in it. So this read has to leave the process
-- calling `browse()` in-process returns an empty tree, because the
paths simply are not on this filesystem.

TWO WAYS IT WENT WRONG, AND THEY PULL IN OPPOSITE DIRECTIONS. First it
fetched with a synchronous `urllib.urlopen` from inside an async
handler, which blocks the event loop. That was survivable while the
target was the console -- a different process -- but
`KERNEL_ASSISTANT_URL` is the control panel's OWN `/internal/kernel`
mount, so the fetch became the server asking itself for an answer on
the loop it needed to answer with. It waited for its own socket until
the timeout, and the page showed `Failed to load: 502 Bad Gateway`.

Hence: an async client, and a URL that is deliberately NOT this
process. `KERNEL_WORKSPACE_URL` names the container that holds the
volume. KERNEL_ASSISTANT_URL is never used here, precisely because in
this topology it points back at us.

MOUNTS ARE FLATTENED INTO THE PATH. After E7e a story can hold several
repositories and the page has no mount selector, so each entry is
prefixed with its mount -- `repos/acme__api/src/x.py`. Multi-repo works
in the existing UI with no change, and the paths shown are exactly the
ones the executor sees, which is what somebody reading a diff needs.
"""

from __future__ import annotations

import logging
import os
import urllib.parse

logger = logging.getLogger(__name__)


TIMEOUT_S = 20


def base_url() -> str:
    """The container that holds the workspace volume.

    Deliberately does NOT consider KERNEL_ASSISTANT_URL: in this
    topology that is the control panel's own mount, and reading through
    it means the process fetching from itself.
    """
    return (os.environ.get("KERNEL_WORKSPACE_URL", "").rstrip("/")
            or os.environ.get("KERNEL_CONSOLE_URL", "").rstrip("/"))


def enabled() -> bool:
    """Kernel mode. Unset it and the old gateway-RPC path is used.

    Still an env var and not a column: a `kernel_execution_id` on
    `stories` would be the production answer, and it is a migration, a
    backfill and a second story model to keep in step.
    """
    return bool(base_url())


def _url(params: dict) -> str:
    q = {k: v for k, v in params.items() if v not in ("", None)}
    base = base_url()
    # The kernel surface serves this as a VIEW; the console serves the
    # same thing at its own path. Both now take a story id.
    if "/view/" in base or base.endswith("/view"):
        path = "/workspace"
    elif ":8770" in base or base.endswith("/api"):
        path = "/api/workspace"
    else:
        path = "/view/-/workspace"
    return f"{base}{path}?{urllib.parse.urlencode(q)}"


async def _browse(story_id: str, mount: str = "", rel: str = "",
                  want: str = "tree") -> dict:
    import httpx

    params = {"story": story_id, "mount": mount, "path": rel,
              "view": want if want != "tree" else ""}
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.get(_url(params))
        r.raise_for_status()
        return r.json()


def _split(path: str, mounts: list) -> tuple:
    """A flattened path back into (mount, relative). Longest match wins.

    Longest-first because one mount's `at` can prefix another's --
    `repos/api` and `repos/api-client` -- and matching the shorter one
    would read the wrong repository while looking correct.
    """
    for at in sorted((m["at"] for m in mounts), key=len, reverse=True):
        if at and path.startswith(at + "/"):
            return at, path[len(at) + 1:]
    return (mounts[0]["at"] if mounts else ""), path


async def tree(story_id: str) -> dict:
    """Every mount's files, flattened, in the shape the page renders."""
    first = await _browse(story_id)
    mounts = first.get("mounts") or []
    if not mounts:
        return {"entries": [], "truncated": False,
                "detail": first.get("detail") or "no workspace yet"}

    # ONE CALL, EVERY MOUNT. This used to fetch each mount separately --
    # N round trips to build one panel, and the first mount fetched
    # twice. `view=all` returns them together, which also means the
    # panel cannot show a half-built workspace because one request
    # failed midway.
    whole = await _browse(story_id, want="all")
    entries, truncated = [], bool(whole.get("truncated"))
    for tree in whole.get("trees") or []:
        at = tree.get("at") or ""
        for e in tree.get("entries", []):
            # A SINGLE-FILE MOUNT IS ITS OWN PATH. An uploaded document
            # mounts at `docs/NAME.md` -- the file itself -- so joining
            # the mount to the entry gave `docs/NAME.md/NAME.md`, a path
            # that exists nowhere and opens nothing.
            path = at if tree.get("single_file") else (
                f"{at}/{e['path']}" if at else e["path"])
            entries.append({
                "path": path,
                "type": "file",
                "size": e["size"],
                # Carrying git status through means a person can see
                # WHICH files this leg touched without reading a diff,
                # which is the question they actually have.
                "status": e.get("status") or "",
                "writable": bool(tree.get("writable")),
                "mtimeMs": 0,
            })
    return {"entries": entries, "truncated": truncated,
            "mounts": [{"at": m["at"], "writable": m["writable"]}
                       for m in mounts],
            "changed": sum(1 for e in entries if e["status"])}


async def read_file(story_id: str, path: str) -> dict:
    """One file, in the shape the page renders. Uncommitted edits included."""
    listing = await _browse(story_id)
    mount, rel = _split(path, listing.get("mounts") or [])
    out = await _browse(story_id, mount=mount, rel=rel, want="file")
    if out.get("error"):
        return {"error": out["error"]}
    return {"path": path, "content": out.get("text", ""),
            "mime": "text/plain", "size": out.get("size", 0),
            "truncated": False}
