"""Sandbox-side filesystem tools: glob, grep, edit.

Runs INSIDE the sandbox, imported by shim.py. Stdlib only — the sandbox is
python:3.12-slim with nothing installed, which is what keeps build_spec's
hardcoded image viable.

Every tool returns the same shape as the rest of the bridge:
{"output": str, "error": str|None, "exit_code": int}. A tool that fails
returns a typed error the model can act on; it never raises across the
bridge, because an exception there is an opaque 500 to the harness.
"""

import fnmatch
import os
import re

import errors
from paths import contain

MAX_OUTPUT = 32 * 1024
MAX_MATCHES = 200
#: How many files one search may OPEN. `MAX_MATCHES` bounds the hits and
#: bounded nothing about the walk: a pattern that matches little walks
#: everything under the root looking for it.
MAX_FILES = 20_000
#: How much of ONE file a search may pull in. Line iteration has no
#: bound of its own, and a file with no newline is a single "line" --
#: which is how a grep that reached /proc took a 9GiB sandbox down.
MAX_FILE_BYTES = 4 * 1024 * 1024


def _resolve(workspace: str, path: str):
    """`(root, None)` or `(None, error)` -- the error is returnable."""
    return contain(workspace, path)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + "\n... [truncated]"


def tool_glob(args: dict, workspace: str) -> dict:
    """Find files by glob pattern. Results are sorted for determinism.

    Sorted rather than in os.walk order: LR5's progress clock hashes the
    (tool, target) pair, and an unstable ordering would make the same
    search look like different work on every call.
    """
    pattern = args.get("pattern", "")
    if not pattern:
        return errors.fail("E_BAD_ARGUMENT", "pattern is required")

    root, bad = _resolve(workspace, args.get("path", ""))
    if bad:
        return bad
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__",
                                                        "node_modules", ".venv")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, workspace)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fn, pattern):
                matches.append(rel.replace(os.sep, "/"))
            if len(matches) >= MAX_MATCHES:
                break
        if len(matches) >= MAX_MATCHES:
            break

    matches.sort()
    if not matches:
        # Not an error: "nothing matched" is a real answer, and returning
        # exit_code 1 would teach the model it had done something wrong.
        return errors.ok("(no files matched)", matches=0)
    return errors.ok(_truncate(chr(10).join(matches)), matches=len(matches))


def tool_grep(args: dict, workspace: str) -> dict:
    """Search file contents by regular expression."""
    pattern = args.get("pattern", "")
    if not pattern:
        return errors.fail("E_BAD_ARGUMENT", "pattern is required")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return errors.fail("E_BAD_ARGUMENT", f"invalid regex: {e}")

    root, bad = _resolve(workspace, args.get("path", ""))
    if bad:
        return bad

    hits, scanned, capped = [], 0, False
    for path in _walk_files(root):
        scanned += 1
        if scanned > MAX_FILES:
            capped = True
            break
        # SORTED PER DIRECTORY, NOT GLOBALLY. The old code built the
        # whole file list and sorted it before reading anything, so the
        # cost of a wide root was paid in full before the first match
        # could stop it. `_walk_files` yields in a stable order instead.
        if not _search_one(path, rx, workspace, hits):
            continue
        if len(hits) >= MAX_MATCHES:
            break

    if not hits:
        note = (f" (stopped after {MAX_FILES} files)" if capped else "")
        return errors.ok(f"(no matches){note}", matches=0, files_scanned=scanned)
    return errors.ok(_truncate(chr(10).join(hits)), matches=len(hits),
                     files_scanned=scanned)


def _walk_files(root: str):
    """Every file under `root`, in a stable order, one at a time."""
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".git", "__pycache__",
                                          "node_modules", ".venv"))
        for f in sorted(filenames):
            yield os.path.join(dirpath, f)


def _search_one(path: str, rx, workspace: str, hits: list) -> bool:
    """Match one file, reading a bounded amount of it.

    READ IN CHUNKS AND SPLIT, rather than iterating lines. Line
    iteration will build a single string as long as the file has no
    newline in it, with no bound and no error to catch -- the sandbox
    just dies. Nothing legitimate in a workspace needs more than
    MAX_FILE_BYTES searched.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            blob = fh.read(MAX_FILE_BYTES)
    except (OSError, UnicodeDecodeError, ValueError):
        return False  # binary, unreadable, or a device: not a failure
    for n, line in enumerate(blob.splitlines(), start=1):
        if rx.search(line):
            rel = os.path.relpath(path, workspace).replace(os.sep, "/")
            hits.append(f"{rel}:{n}:{line.rstrip()}")
            if len(hits) >= MAX_MATCHES:
                return True
    return True


def tool_edit(args: dict, workspace: str) -> dict:
    """Replace an exact string. Refuses a non-unique match by default.

    An edit tool that silently changes the first of three occurrences is
    how a model corrupts a file it cannot see it corrupted. So ambiguity
    is a REFUSAL, and it is the one that matters most in the taxonomy:
    E_MATCH_AMBIGUOUS is the near-miss that would otherwise BE a silent
    wrong edit, and typing it is what makes the rate measurable.

    `replace_all` is the escape hatch for the case where refusing is
    itself wrong. A rename wants EVERY occurrence, and forcing three
    disambiguated edits is how two of three get renamed — the same
    defect arriving by the safe-looking road. It must be asked for.
    """
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))

    if not path:
        return errors.fail("E_BAD_ARGUMENT", "path is required")
    if not old:
        return errors.fail("E_BAD_ARGUMENT",
                           "old_string is required; use write to create "
                           "a file")
    if old == new:
        return errors.fail("E_NO_CHANGE",
                           "old_string and new_string are identical")

    target, bad = _resolve(workspace, path)
    if bad:
        return bad
    try:
        with open(target, encoding="utf-8") as fh:
            body = fh.read()
    except OSError as e:
        return errors.from_oserror(e, path)

    count = body.count(old)
    if count == 0:
        return errors.fail("E_MATCH_NOT_FOUND",
                           f"old_string does not appear in {path}")
    if count > 1 and not replace_all:
        return errors.fail(
            "E_MATCH_AMBIGUOUS",
            f"old_string appears {count} times in {path}",
            occurrences=count)

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(body.replace(old, new))
    except OSError as e:
        return errors.from_oserror(e, path)

    # The count is reported even on success. `replace_all` changing six
    # places when the caller expected one is a thing they should find
    # out now rather than from a failing check later.
    where = f"{count} occurrence(s) in {path}" if count > 1 else path
    return errors.ok(f"edited {where}", occurrences=count)


def tool_list_dir(args: dict, workspace: str) -> dict:
    """List one directory. Directories marked, entries sorted.

    Added at G4 because the baseline needed it: a task edited the right
    file, then searched src/**/test_*.py and src/**/*_test.py for the
    suite, ran out of ideas and ASKED A HUMAN where the tests were —
    with tests/ sitting in the repository root the whole time. `glob`
    answers "where is the thing I can name"; nothing answered "what is
    actually here", which is the question you have when your guess about
    the layout is wrong.
    """
    path = args.get("path", "")
    if path:
        target, bad = _resolve(workspace, path)
        if bad:
            return bad
    else:
        target = workspace
    try:
        names = sorted(os.listdir(target))
    except OSError as e:
        return errors.from_oserror(e, path or ".")

    if not os.path.isdir(target):
        return errors.fail("E_BAD_ARGUMENT", f"{path} is not a directory")

    rows, hidden = [], 0
    for name in names:
        if name in (".git", "__pycache__", "node_modules", ".venv",
                    ".pytest_cache"):
            hidden += 1
            continue
        full = os.path.join(target, name)
        if os.path.isdir(full):
            rows.append(name + "/")
        else:
            try:
                rows.append(f"{name}  ({os.path.getsize(full)}B)")
            except OSError:
                rows.append(name)
    if not rows:
        # An empty listing and a listing of nothing but noise read the
        # same to a model, and one of them means "you are in the wrong
        # place".
        return errors.ok(f"(empty: {path or chr(46)})", entries=0)
    note = f"  [{hidden} build/vcs entries hidden]" if hidden else ""
    return errors.ok(chr(10).join(rows) + note, entries=len(rows))
