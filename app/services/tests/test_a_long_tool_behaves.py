"""The job mechanism, against a real subprocess, in seconds.

L2. Above the pure functions and below a pod. Every test here starts a
real process through the real registry and reads it back through the
real tool -- because the things this phase claims are about a pipe, a
thread and a signal, and none of those can be asserted from a mock.

THE FIXTURE FORKS ON PURPOSE. `bash -c "sleep 3"` killed at the shell is
a process group of one, so a test that kills it and sees it die proves
nothing about the bug being guarded: `pip` and `npm` spawn children, and
a bare `proc.kill()` leaves them writing into the tree AND holding
stdout, so the drain thread never sees EOF and the job reads as running
for ever.

KEPT UNDER A FEW SECONDS EACH. A suite nobody runs because it is slow is
a suite that does not guard anything.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3] / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)

jobcore = pytest.importorskip("jobcore")
tools_jobs = pytest.importorskip("tools_jobs")

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "longtool.py"

posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process groups are POSIX; the fixture degrades on Windows")


# A JOB SPILLS ITS WHOLE LOG INTO ITS WORKSPACE, which is the point of
# A5 -- and a test passing the repository root as the workspace writes
# those logs into the checkout. It did: 1.2 MB of `.agentos-out/` under
# version control's nose, growing every run. A scratch directory instead,
# made once for the module so the jobs that must share a workspace still
# do.
_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="agentos-jobtest-"))


def _start(*flags: str, workspace: str = "") -> str:
    """Start the fixture as a background job and return its id."""
    command = f'"{sys.executable}" "{_FIXTURE}" ' + " ".join(flags)
    answer = tools_jobs.tool_bash_background(
        {"command": command}, workspace or str(_SCRATCH),
        dict(os.environ))
    assert answer["error"] is None, answer
    return answer["output"].split()[1].rstrip(":")


def _poll(job_id: str, since: int = 0) -> str:
    return tools_jobs.tool_bash_output(
        {"job_id": job_id, "since": since})["output"]


def _wait_until(job_id: str, predicate, limit: float = 12.0) -> str:
    deadline = time.monotonic() + limit
    body = ""
    while time.monotonic() < deadline:
        body = _poll(job_id)
        if predicate(body):
            return body
        time.sleep(0.2)
    return body


# ── the ordinary path ───────────────────────────────────────────────

def test_a_job_runs_and_reports_its_exit_code():
    job = _start("--seconds", "1", "--exit", "7", "--no-fork")
    body = _wait_until(job, lambda b: "finished" in b)
    assert "finished (exit 7)" in body


def test_output_arrives_while_it_is_still_running():
    """Publishing only at exit made `bash_output` on a running job return
    the status and nothing else, which defeats the point: the reason to
    poll a twelve-minute build is to watch it."""
    job = _start("--seconds", "4", "--bytes-per-second", "80", "--no-fork")
    try:
        body = _wait_until(job, lambda b: "parent[0]" in b)
        assert "running" in body
        assert "parent[0]" in body
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_the_cursor_advances_and_returns_only_what_is_new():
    job = _start("--seconds", "4", "--bytes-per-second", "80", "--no-fork")
    try:
        _wait_until(job, lambda b: "parent[0]" in b)
        first = _poll(job)
        cursor = int(first.split("bytes ")[1].split("-")[1].split()[0])
        again = _poll(job, since=cursor)
        assert "parent[0]" not in again
        assert f"bytes {cursor}-" in again
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


# ── the window ──────────────────────────────────────────────────────

def test_a_chatty_job_drops_the_head_and_says_so(monkeypatch):
    """The first version kept the FIRST bytes and discarded the rest, so
    watching a build showed its startup banner and never the failure."""
    monkeypatch.setenv("AGENTOS_JOB_OUTPUT_MAX", "1024")
    job = _start("--seconds", "3", "--bytes-per-second", "2000", "--no-fork")
    try:
        body = _wait_until(job, lambda b: "earlier output dropped" in b)
        assert "earlier output dropped" in body
        assert "showing the last 1 KB" in body
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_a_cursor_the_window_outran_says_how_much_was_missed(monkeypatch):
    monkeypatch.setenv("AGENTOS_JOB_OUTPUT_MAX", "1024")
    job = _start("--seconds", "3", "--bytes-per-second", "2000", "--no-fork")
    try:
        _wait_until(job, lambda b: "earlier output dropped" in b)
        behind = _poll(job, since=1)
        assert "between your cursor and the buffer" in behind
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


# ── stopping it ─────────────────────────────────────────────────────

def test_killing_a_job_says_it_was_cancelled_not_that_it_failed():
    """A signal death cannot distinguish a cancel from a failure, and
    "your suite was stopped" is a different sentence from "your suite
    failed"."""
    job = _start("--seconds", "20", "--no-fork")
    tools_jobs.tool_bash_kill({"job_id": job})
    body = _wait_until(job, lambda b: "finished" in b)
    assert "cancelled, not because it failed" in body


def test_killing_a_finished_job_is_not_an_error():
    job = _start("--seconds", "1", "--no-fork")
    _wait_until(job, lambda b: "finished" in b)
    answer = tools_jobs.tool_bash_kill({"job_id": job})
    assert answer["error"] is None
    assert "already finished" in answer["output"]


@posix_only
def test_killing_a_job_kills_the_child_it_forked(tmp_path):
    """THE BUG THIS PHASE FIXES. The child holds stdout open, so a parent
    killed alone leaves the job reading as `running` for ever -- and the
    child keeps writing into the tree the next leg is editing."""
    marker = tmp_path / "touched.txt"
    job = _start("--seconds", "25", "--write", str(marker))
    _wait_until(job, lambda b: "parent[0]" in b)
    tools_jobs.tool_bash_kill({"job_id": job})

    body = _wait_until(job, lambda b: "finished" in b, limit=8.0)
    assert "finished" in body, "a killed group must reach a terminal state"

    # And nothing is still writing.
    before = marker.read_text(encoding="utf-8") if marker.exists() else ""
    time.sleep(2.5)
    after = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert before == after, "a child survived the kill and kept writing"


# ── the ceiling ─────────────────────────────────────────────────────

def test_the_model_cannot_start_more_than_its_share(monkeypatch):
    """Eight, not the harness's four. Inheriting the smaller ceiling in
    the merge would have silently halved what a story can run.

    WAITS FOR THE LANE TO EMPTY FIRST, and that is the fix to a flake
    this docstring used to describe without actually avoiding. The
    ceiling was read BEFORE `first` started, as `live + 1`. A kill is
    finalised on its own thread, so a job another test stopped a moment
    earlier could finish in between: the count dropped by one, the
    ceiling was one too high, and the job this asserts is refused had
    room after all. Seen once in a full-suite run and never alone.

    With the lane quiet and a ceiling of one, the only job in it is the
    one started here, so nothing can drain to make room.
    """
    assert tools_jobs.MAX_JOBS == 8
    deadline = time.time() + 10
    while (jobcore.live_in_lane(tools_jobs.LANE)
           and time.time() < deadline):
        time.sleep(0.05)
    monkeypatch.setattr(tools_jobs, "MAX_JOBS", 1)
    first = _start("--seconds", "6", "--no-fork")
    try:
        refused = tools_jobs.tool_bash_background(
            {"command": "true"}, str(_SCRATCH), dict(os.environ))
        assert refused["error_code"] == "E_TOO_LARGE"
    finally:
        tools_jobs.tool_bash_kill({"job_id": first})


# ── A1 · the budget, and what it must not break ─────────────────────

def test_a_job_with_no_timeout_is_still_unbounded():
    """THE REGRESSION GUARD. `bash_background` has never had a default
    budget. Giving every job one would kill the forty-minute suites that
    work today -- a safety feature that breaks working stories is not
    one. The job below would die in 1 second if a default existed."""
    job = _start("--seconds", "3", "--no-fork")
    try:
        time.sleep(1.5)
        body = _poll(job)
        assert "running" in body, "an unbounded job was stopped by something"
        assert "TIMEOUT" not in body
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_the_start_message_says_whether_it_is_bounded():
    """A job id does not say whether it has a limit, and "runs until it
    finishes" is a different thing to plan around from "stopped at 600s"."""
    unbounded = tools_jobs.tool_bash_background(
        {"command": "true"}, str(_SCRATCH), dict(os.environ))
    assert "NO time limit" in unbounded["output"]

    bounded = tools_jobs.tool_bash_background(
        {"command": "true", "timeout": 30}, str(_SCRATCH),
        dict(os.environ))
    assert "stopped after 30s" in bounded["output"]


def test_a_declared_budget_stops_the_job_and_calls_it_a_timeout():
    """The whole point of `ended`: a signal death cannot distinguish a
    budget from a failure, and telling a model its suite failed when it
    ran out of time is the misread this field exists to stop."""
    job = _start("--seconds", "30", "--no-fork")
    # Re-start with a budget rather than reusing the one above, so the
    # assertion is about the budget and not about timing luck.
    tools_jobs.tool_bash_kill({"job_id": job})
    bounded = tools_jobs.tool_bash_background(
        {"command": f'"{sys.executable}" "{_FIXTURE}" --seconds 30 --no-fork',
         "timeout": 1},
        str(_SCRATCH), dict(os.environ))
    job = bounded["output"].split()[1].rstrip(":")
    body = _wait_until(job, lambda b: "finished" in b, limit=10.0)
    assert "TIMEOUT, not a failing command" in body


def test_a_budget_out_of_range_is_refused_not_shortened():
    """A caller that asked for three hours and silently got two reads
    the kill as its command failing."""
    refused = tools_jobs.tool_bash_background(
        {"command": "true", "timeout": 99999}, str(_SCRATCH),
        dict(os.environ))
    assert refused["error_code"] == "E_BAD_ARGUMENT"
    assert "7200" in refused["error"]


def test_the_budget_ceiling_follows_the_timings_profile(monkeypatch):
    """A compressed story can exercise the refusal in seconds instead of
    needing a two-hour request to trip it."""
    monkeypatch.setenv("AGENTOS_JOB_MAX_S", "180")
    refused = tools_jobs.tool_bash_background(
        {"command": "true", "timeout": 300}, str(_SCRATCH),
        dict(os.environ))
    assert refused["error_code"] == "E_BAD_ARGUMENT"
    assert "180" in refused["error"]


# ── A2 · finding a job again after a leg boundary ───────────────────

def test_bash_jobs_lists_what_is_still_running():
    """A leg boundary loses the model's memory of a job id, not the job.
    Without a list, the only way back is a `bash_output` on an id the
    model no longer has -- which answers with the known ids in an ERROR.
    Asking for a list should not require causing an error first."""
    job = _start("--seconds", "8", "--bytes-per-second", "60", "--no-fork")
    try:
        _wait_until(job, lambda b: "parent[0]" in b)
        body = tools_jobs.tool_bash_jobs({})["output"]
        assert job in body
        assert "bytes" in body
        assert "bash_output" in body, "say how to get back to it"
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_bash_jobs_says_so_when_there_are_none():
    for live in jobcore.running_jobs(tools_jobs.LANE):
        tools_jobs.tool_bash_kill({"job_id": live["id"]})
    time.sleep(3.0)
    body = tools_jobs.tool_bash_jobs({})["output"]
    assert "No background jobs" in body


def test_bash_jobs_does_not_list_finished_ones():
    """A finished job is in the registry for a while -- the buffer is what
    a caller reads AFTER it exits -- but it is not something to resume."""
    job = _start("--seconds", "1", "--no-fork")
    _wait_until(job, lambda b: "finished" in b)
    assert job not in tools_jobs.tool_bash_jobs({})["output"]


# ── A5 · the whole log on disk ──────────────────────────────────────

def test_the_full_stream_is_spilled_to_disk(tmp_path):
    """The window is small because it is context. A forty-minute build's
    real failure is thousands of lines before the end, and dropping it
    because a model does not want it in a prompt throws away the only
    copy."""
    monkey_ws = tmp_path / "ws"
    monkey_ws.mkdir()
    job = _start("--seconds", "2", "--bytes-per-second", "300",
                 "--no-fork", workspace=str(monkey_ws))
    _wait_until(job, lambda b: "finished" in b)

    log = monkey_ws / "agentos_out_placeholder"
    from jobspill import SPILL_DIR
    spilled = monkey_ws / SPILL_DIR / f"{job}.log"
    assert spilled.exists(), f"no spill file at {spilled}"
    body = spilled.read_text(encoding="utf-8", errors="replace")
    assert "parent[0]" in body and "parent[1]" in body


def test_the_spill_is_excluded_from_git(tmp_path):
    """`story_commit` runs `git add -A` over the workspace. `setups.py`
    already paid for this once -- it would otherwise have committed the
    .venv the setup just built."""
    ws = tmp_path / "repo"
    (ws / ".git" / "info").mkdir(parents=True)
    job = _start("--seconds", "1", "--no-fork", workspace=str(ws))
    _wait_until(job, lambda b: "finished" in b)

    from jobspill import SPILL_DIR
    exclude = (ws / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert f"{SPILL_DIR}/" in exclude


def test_a_workspace_without_git_still_runs_the_job(tmp_path):
    """Not being a repository is not a reason to refuse to run."""
    ws = tmp_path / "plain"
    ws.mkdir()
    job = _start("--seconds", "1", "--no-fork", workspace=str(ws))
    body = _wait_until(job, lambda b: "finished" in b)
    assert "finished" in body


def test_the_start_message_names_the_log(tmp_path):
    ws = tmp_path / "ws2"
    ws.mkdir()
    answer = tools_jobs.tool_bash_background(
        {"command": "true"}, str(ws), dict(os.environ))
    from jobspill import SPILL_DIR
    assert SPILL_DIR in answer["output"]
    assert "read and grep" in answer["output"]


def test_dropped_bytes_point_at_the_file(tmp_path, monkeypatch):
    """SAID WHERE THEY ARE LOST. A model reading "earlier output dropped"
    has no reason to remember a path mentioned once at start, and the
    natural response -- poll harder -- cannot recover them."""
    monkeypatch.setenv("AGENTOS_JOB_OUTPUT_MAX", "1024")
    ws = tmp_path / "ws3"
    ws.mkdir()
    job = _start("--seconds", "3", "--bytes-per-second", "2000",
                 "--no-fork", workspace=str(ws))
    try:
        body = _wait_until(job, lambda b: "earlier output dropped" in b)
        assert "the whole log is in" in body
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_the_spill_directory_name_is_shared_with_the_fingerprint():
    """A7 keys a cached check run on a tree fingerprint. A log growing
    inside the tree would move it on every write, so every run would be
    stale the moment it finished -- A5 would defeat A7 entirely. One
    constant, so the fingerprint can exclude it by name."""
    import jobspill

    assert jobspill.SPILL_DIR.startswith(".")
    assert jobspill.MAX_SPILL_BYTES > 0


# ── A3 · bash_wait, one step ────────────────────────────────────────

def test_bash_wait_returns_the_result_when_the_job_finishes():
    """It hands back what a poll would say, so the model needs no second
    call to learn the outcome -- which would put the extra step back."""
    job = _start("--seconds", "1", "--exit", "5", "--no-fork")
    body = tools_jobs.tool_bash_wait({"job_id": job})["output"]
    assert "finished (exit 5)" in body


def test_bash_wait_on_a_slow_job_reports_an_incomplete_slice():
    """The sandbox bounds its own wait because the worker is holding an
    HTTP read open. This marker is what the worker re-arms on; the model
    sees it only if the worker's loop broke."""
    job = _start("--seconds", "30", "--no-fork")
    try:
        import tools_jobs as tj
        original = tj.WAIT_SLICE_S
        tj.WAIT_SLICE_S = 0.5
        try:
            answer = tools_jobs.tool_bash_wait({"job_id": job})
        finally:
            tj.WAIT_SLICE_S = original
        assert answer.get("wait_incomplete") is True
        assert answer["output"].startswith("[still running after the wait")
    finally:
        tools_jobs.tool_bash_kill({"job_id": job})


def test_bash_wait_on_an_unknown_job_names_what_exists():
    answer = tools_jobs.tool_bash_wait({"job_id": "job-nope"})
    assert answer["error_code"] == "E_BAD_ARGUMENT"
    assert "job-nope" in answer["error"]


def test_the_two_halves_spell_the_marker_the_same_way():
    """The sandbox module is stdlib-only and loaded by bare name, so it
    cannot import the harness's constant. Two spellings of one string is
    a silent infinite loop if they drift -- the worker would re-arm for
    ever on an answer it never recognises as final."""
    from kernel.harness.executor import _WAIT_INCOMPLETE

    import inspect
    src = inspect.getsource(tools_jobs.tool_bash_wait)
    assert _WAIT_INCOMPLETE in src


def test_the_bridge_out_waits_one_slice():
    """If the HTTP read is shorter than the sandbox's own wait, every
    wait comes back as a dead bridge with the model's request apparently
    ignored."""
    import limits

    assert limits.bridge_timeout("bash_wait", {}) > tools_jobs.WAIT_SLICE_S


def test_bash_wait_is_a_sandbox_tool_the_model_can_name():
    from kernel.tools.catalog import tools_by_side

    assert "bash_wait" in set(tools_by_side("sandbox"))


# ── A4 · the mirror ─────────────────────────────────────────────────

def test_the_sandbox_hands_up_structured_state_beside_the_prose():
    """The worker cannot parse the prose and should not try: it renders
    an exit code inside a sentence, beside notes about dropped bytes.
    Reading that programmatically is the fault shape that once reported
    a 45MB capture as "stored nothing"."""
    job = _start("--seconds", "1", "--no-fork")
    _wait_until(job, lambda b: "finished" in b)
    answer = tools_jobs.tool_bash_output({"job_id": job})
    facts = answer["job"]
    assert facts["id"] == job
    assert facts["running"] is False
    assert facts["ended"] == "exit"
    assert isinstance(facts["produced"], int)


def test_starting_a_job_reports_it_immediately():
    """A job the model starts and never polls is otherwise invisible to
    anyone asking how the story is going -- which is the state a long
    job is most likely to be in."""
    answer = tools_jobs.tool_bash_background(
        {"command": "true"}, str(_SCRATCH), dict(os.environ))
    assert answer["job"]["running"] is True


def test_the_mirror_throttles_the_middle_but_never_the_ends():
    """Start and finish always; in between only on the throttle. A POST
    per poll on a two-hour job is thousands of round trips for a number
    a person reads occasionally."""
    from kernel.worker import job_mirror

    job_mirror._last_sent.clear()
    assert job_mirror._due("j1", running=True) is True
    assert job_mirror._due("j1", running=True) is False, "throttled"
    assert job_mirror._due("j1", running=False) is True, "the end always goes"


def test_the_mirror_never_raises_without_a_panel(monkeypatch):
    """A local run with no panel is the ordinary case on E0, and a job
    that runs perfectly while nothing records it beats a job that
    refuses to run because nothing is listening."""
    from kernel.worker import job_mirror

    job_mirror._last_sent.clear()
    assert job_mirror.report(1, {"id": "j2", "running": True}) is False


def test_last_seen_moves_only_when_bytes_did():
    """`running` alone cannot tell a slow compile from a pod that
    vanished. Refreshing the timestamp on every report would make a
    silent job look healthy for ever."""
    import inspect

    from app.routers import execution_jobs

    upsert = inspect.getsource(execution_jobs.record)
    assert "EXCLUDED.produced > kernel.execution_jobs.produced" in upsert


def test_the_pod_is_not_granted_write_on_the_mirror():
    """Execution 245 paid for the alternative: three good story reviews,
    every one dead on `permission denied`."""
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, shipped_grants

    if IS_DISTRIBUTION:
        grants = [ln for ln in shipped_grants().splitlines()
                  if ln.startswith("GRANT ")
                  and "kernel.execution_jobs" in ln and "agentos_leg" in ln]
        assert "GRANT SELECT ON kernel.execution_jobs TO agentos_leg;" in grants
        assert not [g for g in grants
                    if any(w in g for w in ("INSERT", "UPDATE", "DELETE", "ALL"))], (
            f"the pod may write the mirror: {grants}")
        return
    sql = pathlib.Path("migrations/execution_jobs_mirror.sql").read_text(
        encoding="utf-8")
    assert "GRANT SELECT ON kernel.execution_jobs TO agentos_leg" in sql
    assert "INSERT" not in sql.split("TO agentos_leg")[1]


def test_no_job_in_this_suite_spills_into_the_repository():
    """A5 writes a job's whole log into its WORKSPACE, so a test that
    passes the checkout as the workspace writes logs into the checkout.
    It did -- 1.2 MB of `.agentos-out/` accumulating across runs, under
    version control's nose, in the one directory `story_commit` walks
    with `git add -A`.

    Reading the source rather than the filesystem, because the
    filesystem answer depends on which tests ran.
    """
    import inspect
    import sys as _sys

    src = inspect.getsource(_sys.modules[__name__])
    # Assembled rather than written out: the needle would otherwise be
    # in the haystack and this would fail on itself.
    needle = "pathlib.Path" + ".cwd()"
    assert needle not in src.replace('"pathlib.Path" + ".cwd()"', ""), (
        "a job started here would spill into the repository")
