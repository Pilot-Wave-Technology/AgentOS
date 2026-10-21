"""Ec1c/Ec1d/Ec1e — the three shell defects an errand runs into first.

Each is the same shape: a mechanism that was built, and a second place that
had to agree with it and did not.

  Ec1c  `bash` reads `timeout` and caps it at 120s. `timeout` was not in
        the schema, so no model could send one and the 120s the
        description advertised was unreachable. And `call_bridge` waits
        30s of its own, so declaring the parameter without raising that
        would have turned a 90s command into `bridge unreachable`.
  Ec1d  The unrecorded-install refusal told the model to call `env_setup`
        — a tool the errand profile did not hold. A correction whose only
        route out is refused by name. The regex was porous besides:
        `pip --quiet install` walked straight through it.
  Ec1e  Polling one background job produced an identical signature every
        time, so a twelve-minute build could be watched seven times before
        the progress clock ended the leg as stalled.

The sandbox modules are stdlib-only and import with `kernel/bridge` on the
path, the same way the shim runs them.
"""
from __future__ import annotations

import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "kernel" / "bridge"))

import limits                                              # noqa: E402


# ── Ec1c: the timeout ────────────────────────────────────────────────────

def test_timeout_is_in_the_schema_the_model_is_shown():
    """THE DEFECT. Implemented, documented, and impossible to send."""
    from kernel.tools.catalog import schemas_for

    props = schemas_for(["bash"])[0]["parameters"]["properties"]
    assert "timeout" in props, "no model can ask for longer than the default"
    assert props["timeout"]["type"] == "integer"


def test_the_default_is_unchanged_for_everyone_who_asks_for_nothing():
    """Story executors send no `timeout` and must behave exactly as before."""
    assert limits.bash_timeout({}) == 30
    assert limits.bash_timeout({"timeout": None}) == 30


@pytest.mark.parametrize("seconds", [1, 30, 90, 120])
def test_a_timeout_inside_the_ceiling_is_honoured(seconds):
    assert limits.bash_timeout({"timeout": seconds}) == seconds


@pytest.mark.parametrize("bad", [0, -5, 121, 600, "ninety", 1.5, True, []])
def test_a_bad_timeout_is_refused_not_clamped(bad):
    """`min(args.get("timeout", 30), 120)` silently clamped 600 to 120, and
    the model then got `E_TIMEOUT: no output within 120s` — which reads as
    a slow command rather than an out-of-range request, so it retries
    instead of reaching for bash_background. It also raised TypeError on a
    string."""
    with pytest.raises(limits.BadTimeout):
        limits.bash_timeout({"timeout": bad})


def test_the_over_ceiling_message_names_the_way_out():
    with pytest.raises(limits.BadTimeout) as caught:
        limits.bash_timeout({"timeout": 600})
    assert "bash_background" in str(caught.value)


def test_the_worker_always_out_waits_the_sandbox():
    """THE HALF THAT WOULD HAVE MADE THE FIX A LIE. If the HTTP read
    timeout is not above the command's own, a long command comes back as a
    dead bridge with the model's argument apparently ignored."""
    for seconds in (1, 30, 90, 120):
        assert limits.bridge_timeout("bash", {"timeout": seconds}) > seconds
    assert limits.bridge_timeout("read", {}) == limits.BRIDGE_DEFAULT_S
    # A refused timeout still needs long enough to carry the refusal back.
    assert limits.bridge_timeout("bash", {"timeout": 9999}) >= 30


def test_the_executor_passes_a_derived_timeout_to_the_bridge():
    """By source: importing the executor pulls in `anthropic`."""
    src = (ROOT / "kernel/harness/executor.py").read_text(encoding="utf-8")
    assert "bridge_timeout(tool_name, tool_args)" in src, (
        "sandbox calls still take the flat 30s default")


def test_env_install_is_a_job_and_not_a_foreground_call():
    """`apt-get update && apt-get install` routinely exceeds 30 seconds and
    this call once took the flat default, so a slow install came back as
    `bridge unreachable` — a dead bridge, not a long download. Raising it
    to the model's ceiling only moved the cliff: one large wheel goes
    past two minutes. E14 made it a job under a budget the pod enforces,
    so there is no ceiling here to be over.

    READS THE WHOLE EXECUTOR FAMILY, not `executor.py` alone. Wave 4
    moved `env_install` to `kernel/harness/executor_env_install.py` to
    get `executor.py` under the house ceiling; a guard pinned to the old
    path would have gone on passing against a file the call had left,
    which is the failure `executor_tools.py` cited for two waves as the
    reason this code could not move. Following the code is the fix.
    Every `executor*.py` is in scope, so the union cannot be emptied by
    moving the call again."""
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "kernel/harness").glob("executor*.py")))
    assert "jobs_client.run_job(" in src
    assert '"timeout": BASH_MAX_S' not in src


def test_both_sides_read_one_file():
    """The sandbox caps and the worker waits. Two constants would drift,
    and when they drift the shell hangs up mid-command.

    COMMENTS STRIPPED BEFORE MATCHING. The comment explaining why the old
    clamp was wrong QUOTES the old clamp, so a raw search finds the text
    describing the rule it is checking — the same way a guard on
    `get_adapter(` was defeated by the prose explaining it.
    """
    import io as _io
    import tokenize

    core_path = ROOT / "kernel/bridge/tools_core.py"
    src = core_path.read_text(encoding="utf-8")
    assert "limits.bash_timeout" in src

    code = "".join(
        tok.string if tok.type not in (tokenize.COMMENT, tokenize.STRING)
        else " "
        for tok in tokenize.generate_tokens(
            _io.StringIO(src).readline))
    assert "min(" not in code or "timeout" not in code.split("min(")[1][:40], (
        "the silent clamp is back in executable code")


# ── Ec1d: the install refusal ────────────────────────────────────────────

@pytest.mark.parametrize("command,manager", [
    ("pip install requests", "pip"),
    ("pip --quiet install requests", "pip"),
    ("pip3 --disable-pip-version-check install requests", "pip"),
    ("pip --cache-dir /tmp install requests", "pip"),
    ("python3 -m pip install -U black", "pip"),
    ("apt-get install curl", "apt"),
    ("apt-get -y install curl", "apt"),
    ("apt-get -o Foo=bar -y install curl", "apt"),
    ("npm install", "npm"),
    ("npm --silent install typescript", "npm"),
    ("yarn add lodash", "npm"),
    ("uv pip install ruff", "pip"),
    ("cd /src && pip --quiet install -r requirements.txt", "pip"),
])
def test_the_regex_catches_a_flagged_install(command, manager):
    """`pip --quiet install` was the porous case: the pattern required the
    verb to sit immediately after the manager."""
    from kernel.harness.env_guard import install_manager

    assert install_manager(command) == manager, command


@pytest.mark.parametrize("command", [
    "pip --help",
    "pip install --help",
    "pip list",
    "pip show requests",
    "pip freeze > frozen.txt",
    "pip --version",
    "python -c 'print(1)'",
    "ls install",
    "pip download -d install_dir requests",
])
def test_the_regex_does_not_catch_what_is_not_an_install(command):
    """The opposite failure, and the one that gets a guard switched off.
    Widening the pattern must not make inspection a refusal."""
    from kernel.harness.env_guard import install_manager

    assert install_manager(command) == "", command


def test_the_refusal_only_names_a_tool_the_caller_holds():
    """THE DEAD END. The errand profile held neither tool, and the refusal
    said "call env_setup" regardless — so the only route out was a tool
    chain stage 2 refuses by name."""
    from kernel.harness.env_guard import refusal

    both = refusal("pip install x", "pip",
                   ["read", "bash", "env_setup", "env_install"])
    assert "env_setup" in both and "env_install" in both

    only_install = refusal("pip install x", "pip", ["read", "bash",
                                                    "env_install"])
    assert "env_install" in only_install
    assert "env_setup" not in only_install

    # Naming both to say the caller has NEITHER is correct — what must not
    # appear is an instruction to call one.
    neither = refusal("pip install x", "pip", ["read", "bash"])
    assert "call env_setup" not in neither
    assert "env_install(manager=" not in neither
    assert "neither env_setup nor env_install" in neither
    assert "ask_human" in neither, "a refusal with no route must say so"


def test_an_unknown_surface_still_offers_both():
    """`None` means the caller could not say. Naming too much is a smaller
    error than telling somebody their only option is a tool they lack."""
    from kernel.harness.env_guard import refusal

    said = refusal("pip install x", "pip", None)
    assert "env_setup" in said and "env_install" in said


def test_the_errand_profile_now_has_the_route_the_refusal_names():
    """The join. Ec1a granted both, so the correction is reachable."""
    from kernel.harness.env_guard import refusal
    from kernel.profiles.loader import load_all

    tools = load_all()[("errand", "ops")].tools
    said = refusal("pip install x", "pip", tools)
    for named in ("env_setup", "env_install"):
        assert named in said and named in tools


def test_the_executor_passes_the_profile_surface():
    src = (ROOT / "kernel/harness/executor.py").read_text(encoding="utf-8")
    assert "refusal(command, manager," in src and "self._profile" in src


# ── Ec1e: polling without stalling ───────────────────────────────────────

def test_polling_with_an_advancing_cursor_is_progress():
    """THE DEFECT. Twenty polls of one job signed identically, so the leg
    died `stalled` on the eighth — the tool that exists to watch a
    twelve-minute build could be used seven times."""
    from kernel.harness.stall import check_stall, signature

    steps = [{"tool": "bash_output",
              "args": {"job_id": "job-1", "since": n * 400},
              "signature": signature("bash_output",
                                     {"job_id": "job-1", "since": n * 400})}
             for n in range(20)]
    assert not check_stall(steps).fired
    assert len({s["signature"] for s in steps}) == 20


def test_polling_the_same_position_still_stalls():
    """Which is the clock working, not misfiring. A model asking one
    question twenty times and getting one answer is stuck."""
    from kernel.harness.stall import check_stall, signature

    sig = signature("bash_output", {"job_id": "job-1", "since": 400})
    steps = [{"tool": "bash_output", "signature": sig} for _ in range(20)]
    assert check_stall(steps).fired


def test_polling_two_jobs_is_not_one_repeated_call():
    from kernel.harness.stall import signature

    assert (signature("bash_output", {"job_id": "a", "since": 0})
            != signature("bash_output", {"job_id": "b", "since": 0}))


def test_the_operand_must_include_the_cursor_not_just_the_job():
    """WHAT THE PLAN GOT WRONG, kept because the wrong fix looks right.

    Ec1e was written as `_OPERAND["bash_output"] = "job_id"`. Measured
    against a 20-poll loop, that is not a fix — it is the bug:

        (job_id, since)   20 distinct signatures, no stall
        absent entirely   20 distinct signatures, no stall
        job_id alone       1 signature, STALLS

    Signing on `job_id` alone makes every poll of one job identical, which
    is exactly the condition that killed the leg. The fallback — signing
    the whole argument set — already included `since` and so was never the
    problem; `since` not EXISTING was. The entry earns its place by
    pinning the operand against a future argument that would otherwise
    join the hash, not by fixing the stall.
    """
    from kernel.harness.stall import _OPERAND, check_stall, signature

    assert _OPERAND["bash_output"] == ("job_id", "since"), (
        "signing on job_id alone makes every poll of one job identical")

    identical = [
        {"tool": "bash_output",
         "signature": signature("bash_output", {"job_id": "j"})}
        for _ in range(20)]
    assert check_stall(identical).fired, (
        "a poll loop that never advances its cursor must still stall")


def test_every_other_tool_signs_exactly_as_it_did():
    """The tuple form must not have changed the single-key path — every
    other tool's signature is a stored value in `step_journal`."""
    from kernel.harness.stall import signature

    assert (signature("read", {"path": "a.py", "offset": 40})
            == signature("read", {"path": "a.py"}))
    assert signature("bash", {"command": "ls"}) != signature(
        "bash", {"command": "pwd"})
    assert signature("nobody_declared_me", {"x": 1}) != signature(
        "nobody_declared_me", {"x": 2})


def test_since_is_in_the_schema_and_optional():
    """Optional, so a caller that ignores it behaves as before — which is
    what keeps story executors unaffected."""
    from kernel.tools.catalog import schemas_for

    schema = schemas_for(["bash_output"])[0]
    assert "since" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["job_id"]


def _inject(job_id, *, produced, buf, running, exit_code=None,
            dropped=False, ended=None):
    """Put a job row straight into the shared registry.

    WHITE-BOX, AND POINTED AT THE RIGHT BOX. The registry moved from
    `tools_jobs` to `jobcore` when the model's and harness's job systems
    merged; the prose these tests assert on did not change, which is the
    point of checking it here rather than through a real subprocess.
    `kept_from` is derived (`produced - len(buf)`) rather than stored, so
    a fixture sets the buffer length to place the cursor.
    """
    import jobcore
    jobcore._jobs[job_id] = {
        "proc": None, "buf": bytearray(buf), "produced": produced,
        "cap": len(buf) or 1, "command": "x", "lane": "model",
        "dropped": dropped, "running": running, "exit_code": exit_code,
        "ended": ended, "started_at": 0.0, "finished_at": None,
        "done": None,
        # A5 added these. A fixture row that omits a key `snapshot`
        # reads raises KeyError, which reads as a broken tool rather
        # than a stale fixture -- so the helper carries the whole shape.
        "spill": None, "spill_path": "",
    }


def _forget(job_id):
    import jobcore
    jobcore._jobs.pop(job_id, None)


def test_the_poll_returns_only_what_is_new_and_says_the_next_cursor():
    """Run for real against the sandbox module — it is stdlib only."""
    import tools_jobs

    _inject("job-t", produced=11, buf=b"alpha\nbeta\n", running=True)
    try:
        first = tools_jobs.tool_bash_output({"job_id": "job-t"})["output"]
        assert "alpha" in first and "beta" in first
        assert "since=11" in first, "the next cursor must be in the text"

        again = tools_jobs.tool_bash_output(
            {"job_id": "job-t", "since": 11})["output"]
        assert "alpha" not in again and "beta" not in again
        assert "no new output" in again

        _inject("job-t", produced=17, buf=b"alpha\nbeta\ngamma\n",
                running=True)
        more = tools_jobs.tool_bash_output(
            {"job_id": "job-t", "since": 11})["output"]
        assert "gamma" in more and "alpha" not in more
    finally:
        _forget("job-t")


def test_a_cursor_the_buffer_has_outrun_says_what_was_missed():
    """The job produced 9000 bytes, the window holds the last 2000, and the
    poller's cursor points at byte 100. Silently returning the buffer would
    let the model believe it had seen everything."""
    import tools_jobs

    # kept_from is produced - len(buf), so a 2000-byte window over 9000
    # produced bytes puts the front of the buffer at 7000.
    _inject("job-t", produced=9000, running=False, exit_code=0, dropped=True,
            buf=b"tail-bytes" + b"." * 1990)
    try:
        out = tools_jobs.tool_bash_output(
            {"job_id": "job-t", "since": 100})["output"]
        assert "6900 bytes" in out and "dropped" in out
        assert "tail-bytes" in out
    finally:
        _forget("job-t")


def test_an_unknown_job_still_names_the_ones_that_exist():
    import tools_jobs

    out = tools_jobs.tool_bash_output({"job_id": "nope"})
    assert out["error_code"] == "E_BAD_ARGUMENT"
    assert "nope" in out["error"]
