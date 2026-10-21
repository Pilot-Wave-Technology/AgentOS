"""What a story has actually changed in one checkout, as a summary.

SEPARATE FROM `explorer.diff` ON PURPOSE. That returns the patch, for a
person reading code in a panel. This returns counts and paths, for an
assistant answering "what's happening" in a sentence -- and the two have
opposite size constraints: a patch should be complete, a summary must
never be long enough that reading it is work.

THE BASE MATTERS AND IS EASY TO GET WRONG. "What has this story
changed" is everything since the engine cut the branch, which is
`git_nodes.base_sha` -- NOT `git diff` (uncommitted only, so a story
that committed reports nothing) and NOT `HEAD~1` (the last leg only, so
a three-leg story reports a third of its work). The caller supplies the
base because only the caller can look it up; with no base this falls
back to the working tree and SAYS SO, rather than quietly reporting a
smaller number than the truth.
"""

from __future__ import annotations

import subprocess

# Enough to see the shape of the work. A story touching more files than
# this is one whose summary should say "and 40 more" rather than list
# them: the point is orientation, not an inventory.
MAX_FILES = 25


def _run(root: str, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if out.returncode != 0:
        return ""
    return out.stdout.decode(errors="replace")


def _numstat(text: str) -> list:
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add, rem, path = parts
        files.append({
            "path": path,
            # A binary file reports "-" for both, which int() will not
            # take. Recording None keeps it in the list -- a changed
            # binary is still a change -- without inventing a count.
            "added": int(add) if add.isdigit() else None,
            "removed": int(rem) if rem.isdigit() else None,
        })
    return files


def changed(root: str, base_sha: str = "") -> dict:
    """Files this checkout has changed, committed and not.

    `committed` counts against `base_sha`; `uncommitted` is the working
    tree including untracked files, because a story's first file is
    always untracked and "no changes" is the most misleading thing this
    could say at that moment.
    """
    out: dict = {"base": base_sha or None, "files": [], "commits": 0,
                 "added": 0, "removed": 0, "uncommitted": 0}

    if base_sha:
        committed = _numstat(_run(root, "diff", "--numstat",
                                  f"{base_sha}..HEAD"))
        log = _run(root, "log", "--oneline", f"{base_sha}..HEAD")
        out["commits"] = len([ln for ln in log.splitlines() if ln.strip()])
    else:
        committed = []
        out["complete"] = False
        out["detail"] = ("no base commit known for this branch, so only "
                         "uncommitted work is counted")

    working = _numstat(_run(root, "diff", "--numstat"))
    untracked = [n for n in
                 _run(root, "ls-files", "--others", "--exclude-standard")
                 .splitlines() if n.strip()]
    out["uncommitted"] = len(working) + len(untracked)

    seen = {}
    for f in committed + working:
        # A file changed in a commit AND again in the working tree is
        # one changed file, and its counts add rather than replace.
        cur = seen.setdefault(f["path"], {"path": f["path"], "added": 0,
                                          "removed": 0})
        for k in ("added", "removed"):
            if f[k] is not None and cur[k] is not None:
                cur[k] += f[k]
            else:
                cur[k] = None
    for path in untracked:
        seen.setdefault(path, {"path": path, "added": None, "removed": None,
                               "new": True})

    files = sorted(seen.values(), key=lambda f: f["path"])
    out["added"] = sum(f["added"] or 0 for f in files)
    out["removed"] = sum(f["removed"] or 0 for f in files)
    out["file_count"] = len(files)
    out["files"] = files[:MAX_FILES]
    if len(files) > MAX_FILES:
        out["and_more"] = len(files) - MAX_FILES
    return out
