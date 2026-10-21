"""The whole of a job's output, on disk, beside the window in memory.

WHY BOTH. The in-memory window is what the MODEL reads, and it is small
on purpose -- every byte of it is context. But the reason to run a suite
in the background is that it is long, and a forty-minute build's real
failure is often thousands of lines before the end. Dropping those bytes
because a model does not want them in its prompt is throwing away the
only copy.

So the window stays small and everything is also written to a file the
model can `read`, `grep` and `glob` like any other. A poll answers "what
is new"; the file answers "what happened".

THREE THINGS THIS HAD TO GET RIGHT, each of which has bitten before:

  IT MUST NOT BE COMMITTED. The file lands inside the workspace, and
  `story_commit` runs `git add -A` over it. `setups.py` already paid for
  this -- "story_commit's `git add -A` would commit the .venv the setup
  just built: five hundred vendored files again, this time made by us"
  -- and its answer is `.git/info/exclude`, repo-LOCAL and never itself
  committed. Same answer here.

  IT MUST NOT INVALIDATE A CACHED CHECK RUN. A7 keys a recorded check on
  a tree fingerprint. A log file growing inside the tree while the job
  runs would move that fingerprint on every write, so every run would be
  stale the moment it finished -- A5 would defeat A7 entirely. The
  directory name is a constant so the fingerprint can exclude it by
  name, and `SPILL_DIR` is that name in the one place both read.

  IT MUST NOT FILL THE DISK. The sandbox has a fixed ephemeral
  allowance, and an evicted pod is a story that vanishes with no verdict
  -- the "compute vanished" shape. So a per-job ceiling, and when a job
  reaches it the file says so rather than growing without limit.
"""

import os

# ONE NAME, READ BY THREE THINGS: the spill writer here, the fingerprint
# that must ignore it, and the `.git/info/exclude` line that keeps it out
# of a commit. A second spelling anywhere is a file that gets committed
# or a check that is always stale.
SPILL_DIR = ".agentos-out"

# Per job. Generous next to the model's window -- the point of the file
# is to hold what the window cannot -- and small enough that eight of
# them cannot be why a pod is evicted.
MAX_SPILL_BYTES = 64 * 1024 * 1024

_EXCLUDE_LINE = SPILL_DIR + "/"


def _ensure_excluded(workspace: str) -> None:
    """Keep the spill out of `git add -A`, repo-locally.

    `.git/info/exclude` rather than `.gitignore`: it is never itself
    committed, so it works on a repo whose ignore file never anticipated
    us and leaves no trace in the diff a person reviews.

    Best-effort and silent. A workspace that is not a git repository, or
    one we cannot write `.git/info` in, is not a reason to refuse to run
    a job -- it only means the spill might be committed, which the
    reviewer will see.
    """
    for root, _, _ in ((workspace, None, None),):
        info = os.path.join(root, ".git", "info")
        try:
            if not os.path.isdir(os.path.join(root, ".git")):
                return
            os.makedirs(info, exist_ok=True)
            path = os.path.join(info, "exclude")
            existing = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
            if _EXCLUDE_LINE not in existing:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n{_EXCLUDE_LINE}\n")
        except OSError:
            return


def open_spill(workspace: str, job_id: str):
    """`(file, path)` for this job's full log, or `(None, "")`.

    Returns rather than raises: a job whose output cannot be spilled is
    still a job worth running, and the window in memory is unaffected.
    """
    if not workspace or not os.path.isdir(workspace):
        return None, ""
    _ensure_excluded(workspace)
    directory = os.path.join(workspace, SPILL_DIR)
    try:
        os.makedirs(directory, exist_ok=True)
        # GROUP-WRITABLE, because two uids are involved: the sandbox
        # writes as 65532 and the worker reads and commits as 65531,
        # with `fsGroup` joining them. A default-mode directory here is
        # a permission error at the moment somebody wants the log.
        try:
            os.chmod(directory, 0o775)
        except OSError:
            pass
        path = os.path.join(directory, f"{job_id}.log")
        return open(path, "ab", buffering=0), path
    except OSError:
        return None, ""


def write_spill(handle, written: int, chunk: bytes) -> int:
    """Append, up to the ceiling. Returns the new total.

    The ceiling is announced IN THE FILE rather than enforced silently:
    a log that simply stops looks like a job that simply stopped, which
    is the reading this whole phase exists to prevent.
    """
    if handle is None or written >= MAX_SPILL_BYTES:
        return written
    room = MAX_SPILL_BYTES - written
    try:
        if len(chunk) <= room:
            handle.write(chunk)
            return written + len(chunk)
        handle.write(chunk[:room])
        handle.write(
            f"\n\n[this log reached its {MAX_SPILL_BYTES // (1024 * 1024)} MB "
            f"ceiling and stops here. The job kept running; poll it for "
            f"what came after.]\n".encode())
        return MAX_SPILL_BYTES
    except OSError:
        return written


def close_spill(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass
