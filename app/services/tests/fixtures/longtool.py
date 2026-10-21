"""A long-running tool that behaves exactly as told. The test fixture.

WHY A FIXTURE AND NOT A REAL SUITE. Every claim this phase makes is
about time -- a job outliving a leg, a budget stopping one, a buffer
dropping bytes, a poller seeing progress. Proving them with a real
`pytest` run means waiting for a real `pytest` run, and the failure
modes you most need (silence, a flood, an exit at exactly the wrong
moment) are the ones a real suite will not produce on demand.

IT FORKS, AND THAT IS THE POINT, not an implementation detail.

    `bash -c "sleep 3"` killed at the shell is a process group of ONE.
    A test that kills it and sees it die proves nothing about the bug
    being guarded, which is that `pip` and `npm` spawn children and a
    bare `proc.kill()` leaves them running -- still writing into a tree
    the next leg is editing, and still holding stdout open so the drain
    thread never sees EOF and the job reads as running for ever.

So the default is a parent with a child, both writing. Kill the group
and both stop. Kill only the parent and the child keeps the pipe open,
which is the failure a group kill exists to prevent and which this
fixture can actually demonstrate.

    --seconds N           how long to run (default 3)
    --bytes-per-second N  0 for a silent job, large for a chatty one
    --exit N              the code to exit with
    --no-fork             a group of one, for testing the fixture itself
    --write PATH          touch a file each second, to prove a killed job
                          stopped writing to the tree

Stdlib only, no imports beyond the standard library, because it runs in
the sandbox image and that image is not ours to add packages to.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def _emit(prefix: str, per_second: int) -> None:
    """One second's worth of output, flushed so a poller sees it now."""
    if per_second <= 0:
        return
    line = f"{prefix} " + "x" * max(1, per_second - len(prefix) - 2)
    sys.stdout.write(line[:per_second] + "\n")
    sys.stdout.flush()


def _child(seconds: int, per_second: int) -> None:
    """The forked half. Writes on its own and would survive a bare kill."""
    for _ in range(seconds):
        _emit("child", per_second)
        time.sleep(1)
    sys.exit(0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=3)
    ap.add_argument("--bytes-per-second", type=int, default=64)
    ap.add_argument("--exit", type=int, default=0)
    ap.add_argument("--no-fork", action="store_true")
    ap.add_argument("--write", default="")
    args = ap.parse_args()

    if not args.no_fork and hasattr(os, "fork"):
        # A REAL SECOND PROCESS, sharing stdout. On Windows there is no
        # `fork`, so the fixture degrades to a single process rather than
        # failing -- the group-kill tests are POSIX-only anyway, because
        # `os.killpg` is.
        if os.fork() == 0:
            _child(args.seconds, args.bytes_per_second)

    for i in range(args.seconds):
        _emit(f"parent[{i}]", args.bytes_per_second)
        if args.write:
            # PROOF THAT A KILL STOPPED THE WORK, not just the pipe. A
            # job that keeps touching the tree after it was cancelled is
            # the reason the group matters.
            with open(args.write, "a", encoding="utf-8") as fh:
                fh.write(f"{i}\n")
        time.sleep(1)

    return args.exit


if __name__ == "__main__":
    sys.exit(main())
