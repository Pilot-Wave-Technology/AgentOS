"""One background-job registry, for the model's tools and the harness's.

WHY THIS EXISTS. There were two. `tools_jobs` served the model
(`bash_background`/`bash_output`/`bash_kill`) and `harness_jobs` served
the harness (`job_start`/`job_poll`/`job_cancel`), with separate dicts,
separate locks, separate ids and separate kill behaviour. Only one of
them was finished: the harness side had a budget watchdog, a group kill,
a reap grace for a grandchild holding the pipe, and an `ended` reason
saying whether a job stopped by exiting, by budget or by cancel. The
model's side had none of those -- `bash_background` took no budget at
all and `bash_kill` called `proc.kill()`, which signals the shell and
leaves `pip`'s children writing into the tree.

Building the missing half twice was the alternative, and two
implementations of one mechanism drift. So the machinery lives here once
and the two modules are presenters over it: they differ in what they
SAY, not in what they do.

WHAT STAYS DIFFERENT, on purpose:

    lane      how many jobs may run at once, and whose they are
    cap       the model keeps a small window it reads as text; the
              harness keeps megabytes it reads as base64 chunks
    id        `job-…` and `hjob-…`, so a caller's mistake names itself

BINARY BUFFER, TEXT AT THE EDGE. `produced` counts every byte the job
ever wrote, so a cursor survives the buffer dropping its front. Decoding
happens in the presenter, because a byte offset into a decoded string
depends on what the command emitted.
"""

import jobspill
import os
import signal
import subprocess
import threading
import time
import uuid

# A grandchild can hold the pipe open after its parent is gone --
# `bash -c "sleep 30"` killed at the shell leaves the sleep holding
# stdout, and the read never returns EOF. Without this the job reads as
# running for ever and the caller waits for a process that is gone.
FINALISE_GRACE_S = 2.0

READ_CHUNK = 65536

# Finished jobs are not free: each holds its buffer. Kept rather than
# dropped at once because a caller reads the result AFTER the job exits.
KEEP_FINISHED = 8

_jobs: dict = {}
_lock = threading.Lock()


def log(lane: str, event: str, job_id: str, **fields) -> None:
    """One line per lifecycle event, to stdout where Cloud Logging is.

    START AND STOP, ALWAYS. A pod that logs only on failure leaves a
    silence identical to a pod that never ran.
    """
    tail = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{lane}-job] {event} job={job_id} {tail}".rstrip(), flush=True)


def signal_group(proc) -> None:
    """Stop the job and everything it started.

    THE GROUP, not the process. `pip` and `npm` spawn children; killing
    only the parent leaves a compiler writing into a workspace the next
    leg is already editing. Both spawners pass `start_new_session=True`,
    so a group exists to signal.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass


def _finish(job_id: str, code, reason: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or not job["running"]:
            return
        job["running"] = False
        job["exit_code"] = code
        job["finished_at"] = time.time()
        job["ended"] = job["ended"] or reason
        lane = job["lane"]
        elapsed = job["finished_at"] - job["started_at"]
        produced, ended = job["produced"], job["ended"]
    log(lane, "stop", job_id, ended=ended, exit_code=code,
        elapsed_s=f"{elapsed:.1f}", bytes=produced)


def reap(job_id: str, proc, reason: str, done: threading.Event) -> None:
    """Finalise a killed job the drain thread has not caught up with.

    `proc.poll()` rather than a fabricated code: if nothing has reaped
    the process yet this is None, and None on an exited job is honest
    where -9 would be an invention. `ended` already says why it stopped.
    """
    if done.wait(FINALISE_GRACE_S):
        return
    _finish(job_id, proc.poll(), reason)


def _drain(job_id: str, proc, done: threading.Event) -> None:
    """Read the pipe continuously so the child never blocks on a full one.

    A subprocess whose pipe fills stops scheduling and looks hung -- the
    classic "works interactively, wedges under an agent" build.

    AND EVERY BYTE GOES TO DISK on the way past. The window in memory is
    what the model reads and is deliberately small; the file is what a
    forty-minute build's real failure is in, thousands of lines before
    the end. Writing here rather than at exit means a killed job still
    has its log.
    """
    with _lock:
        job = _jobs.get(job_id)
        handle = job["spill"] if job else None
    written = 0
    try:
        while True:
            chunk = proc.stdout.read1(READ_CHUNK)
            if not chunk:
                break
            written = jobspill.write_spill(handle, written, chunk)
            with _lock:
                job = _jobs.get(job_id)
                if job is None:
                    break
                job["buf"].extend(chunk)
                job["produced"] += len(chunk)
                # Drop from the HEAD and keep the tail: the reason to
                # poll is to see how it is going NOW. An early version
                # kept the head, so watching a twelve-minute build showed
                # its startup banner and never the failure.
                excess = len(job["buf"]) - job["cap"]
                if excess > 0:
                    del job["buf"][:excess]
                    job["dropped"] = True
    finally:
        jobspill.close_spill(handle)
        try:
            proc.stdout.close()
        except OSError:
            pass
        code = proc.wait()
        _finish(job_id, code, "exit")
        done.set()


def _watchdog(job_id: str, proc, budget_s, done: threading.Event) -> None:
    """Enforce the budget IN THE POD. The caller's clock never does this."""
    if done.wait(budget_s):
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or not job["running"]:
            return
        # Recorded BEFORE the kill, so the -9 the drain thread then reads
        # is explained. A signal death cannot distinguish budget from
        # cancel from OOM, and telling a caller its suite "failed" when
        # it ran out of time is the misread this field exists to stop.
        job["ended"] = "budget"
        lane = job["lane"]
    log(lane, "budget", job_id, budget_s=budget_s)
    signal_group(proc)
    reap(job_id, proc, "budget", done)


def new_id(lane_prefix: str, key=None) -> str:
    if not key:
        return f"{lane_prefix}-{uuid.uuid4().hex[:8]}"
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(key))
    return f"{lane_prefix}-{safe[:48]}"


def prune() -> None:
    """Forget the oldest finished jobs. Call with `_lock` held."""
    finished = sorted((j["finished_at"] or 0.0, i) for i, j in _jobs.items()
                      if not j["running"])
    for _, job_id in finished[:max(0, len(finished) - KEEP_FINISHED)]:
        _jobs.pop(job_id, None)


def live_in_lane(lane: str) -> int:
    with _lock:
        return sum(1 for j in _jobs.values()
                   if j["running"] and j["lane"] == lane)


def start(job_id: str, command: str, *, cwd, env, budget_s, cap: int,
          lane: str, spill_in: str = "") -> str | None:
    """Spawn, register, and arm the drain and the watchdog.

    Returns None on success, or a message describing why it could not
    start. The caller owns the refusal's shape.
    """
    try:
        proc = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
    except OSError as exc:
        return f"cannot start: {exc}"

    handle, spill_path = jobspill.open_spill(spill_in, job_id) if spill_in         else (None, "")
    done = threading.Event()
    with _lock:
        prune()
        _jobs[job_id] = {
            "proc": proc, "buf": bytearray(), "produced": 0, "cap": cap,
            "command": command, "lane": lane, "dropped": False,
            "running": True, "exit_code": None, "ended": None,
            "started_at": time.time(), "finished_at": None, "done": done,
            "spill": handle, "spill_path": spill_path,
            # KEPT SO THE KILL CAN QUOTE IT. A job stopped at its budget
            # has to be able to say which budget, and the watchdog's own
            # log line is not in the model's context.
            "budget_s": budget_s,
        }
    log(lane, "start", job_id, budget_s=budget_s, cwd=cwd or "-",
        command=repr(command[:200]))
    threading.Thread(target=_drain, args=(job_id, proc, done),
                     daemon=True).start()
    # NO BUDGET MEANS NO WATCHDOG, and that is a real state rather than a
    # very large number. `bash_background` has never had a budget; giving
    # it one is a deliberate change with its own item, not a side effect
    # of moving the registry. Until then it passes None and nothing arms.
    if budget_s is not None:
        threading.Thread(target=_watchdog, args=(job_id, proc, budget_s, done),
                         daemon=True).start()
    return None


def snapshot(job_id: str, since: int = 0, window: int = 0) -> dict | None:
    """What the job looks like now, plus the slice after `since`.

    One read under one lock, so the bytes and the state a caller reports
    cannot disagree with each other.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        produced = job["produced"]
        kept_from = produced - len(job["buf"])
        start_at = min(max(since, kept_from), produced)
        head = start_at - kept_from
        buf = job["buf"][head:head + window] if window else job["buf"][head:]
        return {
            "id": job_id, "command": job["command"], "lane": job["lane"],
            "running": job["running"], "exit_code": job["exit_code"],
            "ended": job["ended"], "produced": produced,
            "kept_from": kept_from, "dropped": job["dropped"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "chunk": bytes(buf), "offset": start_at + len(buf),
            "spill_path": job["spill_path"],
            # `.get`, because a job registered before this field existed
            # -- or by a test that builds the dict directly -- must still
            # be readable. A snapshot that raises would turn a missing
            # nicety into an unreadable job.
            "budget_s": job.get("budget_s"),
        }


def forget(job_id: str) -> None:
    """Drop a finished job so a deliberate re-run under the same key does
    not read the previous run's exit code as its own."""
    with _lock:
        _jobs.pop(job_id, None)


def known(lane: str | None = None) -> list[str]:
    with _lock:
        return sorted(i for i, j in _jobs.items()
                      if lane is None or j["lane"] == lane)


def running_jobs(lane: str | None = None) -> list[dict]:
    with _lock:
        return [{"id": i, "command": j["command"], "produced": j["produced"],
                 "started_at": j["started_at"], "lane": j["lane"]}
                for i, j in _jobs.items()
                if j["running"] and (lane is None or j["lane"] == lane)]


def cancel(job_id: str, reason: str) -> tuple[bool, bool]:
    """(known, was_running). Idempotent."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False, False
        was_running = job["running"]
        if was_running:
            job["ended"] = reason
        proc, done, lane = job["proc"], job["done"], job["lane"]
    if was_running:
        signal_group(proc)
        log(lane, reason, job_id)
        threading.Thread(target=reap, args=(job_id, proc, reason, done),
                         daemon=True).start()
    return True, was_running


def kill_all(lane: str | None = None) -> int:
    """Stop every live job. A job outliving its sandbox would keep
    writing into a workspace the next leg has already started editing."""
    killed = 0
    for job_id in known(lane):
        _, was_running = cancel(job_id, "cancel")
        killed += 1 if was_running else 0
    return killed
