"""H12 — seeing the files an executor is changing, per mount.

The live viewer calls `shca.workspace.listFiles` over the gateway RPC and
walks the ASSISTANT's own directory. That works only because today's
assistant is an operator holding the story workspace. Here it holds none
(§1.2: it produces no files, so there is nothing to corrupt, leak between
users, or clean up) and the files live in the executor's tree. So this is
a repoint, not a port.

IT MUST SHOW THE WORKING TREE, NOT THE LAST COMMIT. "See files being
modified" is the requirement, and a committed-only view misses the whole
interesting window: a leg can run twenty minutes between checkpoints, and
the moment worth watching is the one where the edit exists and the commit
does not. So every entry carries its git status, and a file reads from
disk rather than from `git show`.

A TREE PER MOUNT, after E7e. A story granted three repositories has three
trees and one of them may be the only writable one; showing them merged
would hide which repo a change landed in, which is the first question
anybody asks. `writable` travels with each so the view can say why an
edit is not possible rather than letting somebody wonder.

READ-ONLY, AND CONTAINED. Paths are resolved and checked against the
mount root before anything is opened: this serves a browser, and a
`../../etc/passwd` that merely looks wrong is a path traversal.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# A directory listing is for orientation, not for a repository dump. A
# tree with 40,000 entries is slower to render than to regenerate and
# nobody reads past the first screen.
MAX_ENTRIES = 4000

# Big enough for any source file, small enough that a stray binary or a
# vendored bundle does not arrive in a browser tab.
MAX_FILE_BYTES = 512 * 1024

SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
        "dist", "build", ".mypy_cache", ".ruff_cache"}


def _status(root: str) -> dict:
    """`git status --porcelain` as {path: code}. Empty when not a repo."""
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z"],
                         cwd=root, capture_output=True)
    if out.returncode != 0:
        return {}
    marks: dict = {}
    for entry in out.stdout.decode(errors="replace").split("\0"):
        if len(entry) > 3:
            marks[entry[3:]] = entry[:2].strip() or "?"
    return marks


def tree(root: str, at: str = "", writable: bool = False) -> dict:
    """One mount's files, each with its git status."""
    if os.path.isfile(root):
        # A MOUNT CAN BE ONE FILE. An uploaded document mounts at
        # `docs/<name>` -- the document itself, not a directory holding
        # it. This returned "not present" for those, so the panel opened
        # on an empty listing and the documents looked missing when they
        # were sitting right there on disk.
        try:
            size = os.path.getsize(root)
        except OSError:
            size = 0
        name = os.path.basename(root)
        return {"at": at, "writable": writable, "present": True,
                "single_file": True, "entries": [
                    {"path": name, "size": size, "status": ""}],
                "truncated": False, "changed": 0}
    if not os.path.isdir(root):
        return {"at": at, "writable": writable, "present": False,
                "entries": [], "truncated": False}

    marks = _status(root)
    entries, truncated = [], False
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            entries.append({"path": rel, "size": size,
                            # "" committed and unchanged; M modified;
                            # ?? untracked. The whole point of the view.
                            "status": marks.get(rel, "")})
        if truncated:
            break
    return {"at": at, "writable": writable, "present": True,
            "entries": entries, "truncated": truncated,
            "changed": sum(1 for e in entries if e["status"])}


def read(root: str, rel: str) -> dict:
    """One file, from DISK — including edits that are not committed."""
    # Resolved and contained before anything is opened. This serves a
    # browser, and a `..` that merely looks wrong is a traversal.
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(os.path.realpath(root) + os.sep):
        return {"error": "that path is outside the mount"}
    if not os.path.isfile(full):
        return {"error": "no such file"}

    size = os.path.getsize(full)
    if size > MAX_FILE_BYTES:
        return {"error": f"{size:,} bytes is larger than this view carries; "
                         f"read it through the artifact store instead"}
    with open(full, "rb") as fh:
        raw = fh.read()
    if b"\0" in raw[:8000]:
        return {"error": "binary"}
    return {"path": rel, "size": size,
            "text": raw.decode("utf-8", errors="replace")}


def diff(root: str, rel: str = "") -> str:
    """What has changed and is not committed. The reviewable answer."""
    args = ["git", "diff", "--no-color"]
    if rel:
        args += ["--", rel]
    out = subprocess.run(args, cwd=root, capture_output=True)
    text = out.stdout.decode(errors="replace")
    if not text.strip():
        # Untracked files are invisible to `git diff`, and a story's very
        # first file is always untracked -- reporting "no changes" then
        # is the most misleading moment this view has.
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root, capture_output=True).stdout.decode(errors="replace")
        names = [n for n in untracked.splitlines() if n]
        if names:
            return ("(nothing modified; new and not yet committed:\n  "
                    + "\n  ".join(names[:40]) + ")")
    return text


async def mounts_of(conn, execution_id: int) -> list:
    """Every mount of one story, resolved to a path on disk."""
    from kernel.workspace.mounts import mounts_for

    row = await conn.fetchrow(
        "SELECT workspace_path, state, goal_id FROM executions WHERE id = $1",
        execution_id)
    if row is None or not row["workspace_path"]:
        return []
    root = row["workspace_path"]
    out = []
    for m in await mounts_for(conn, execution_id):
        out.append({"at": m.at, "kind": m.kind, "writable": m.writable,
                    "root": os.path.join(root, m.at) if m.at else root})

    # I4a — THE SCRATCH TREE, which had no mount and so was invisible.
    #
    # `mounts_for` returns () when an execution has no `execution_mounts`
    # rows and no `workspace_source`. That is the ERRAND SHAPE exactly:
    # `workspace_driver = "none"`, nothing composed, nothing cloned. But
    # the tree is real — `prepare_workspace` makes `<root>/exec-<id>`
    # before any driver runs, because Docker will not start a container
    # whose volume subpath is missing — and `writable_scope_for` returns
    # it, so the errand writes there all run.
    #
    # The explorer simply never looked, so every errand's workspace panel
    # said "No files in workspace yet." for its entire life, while the
    # files sat one directory away.
    #
    # ONLY WHEN THERE ARE NO MOUNTS AND A PATH EXISTS, which is why this
    # cannot affect a story: a story is refused at `start_story` without
    # resources, and an execution predating `execution_mounts` has a
    # `workspace_source` and takes the legacy branch inside `mounts_for`.
    # Both come back with at least one mount and never reach this.
    if not out and os.path.isdir(root):
        out.append({"at": "", "kind": "scratch", "writable": True,
                    "root": root})
    return out
