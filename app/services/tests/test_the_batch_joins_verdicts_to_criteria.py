"""E19. The runner reports by INDEX; this is the join, and the joins it
must refuse to make.

`test_the_batch_runner_survives_its_checks.py` proves the shell program
against a real `sh`. It says nothing about the Python that turns its
indexed output back into a verdict per criterion, because that module
did not exist -- its docstring has been citing `batch._timed_out` since
E2 and E19 is what finally wrote it.

EVERY TEST HERE IS A WAY A BATCH LOSES OR INVENTS A VERDICT. One Job per
criterion made all of them impossible for free: a container is a
verdict, and a container that never ran is a container nobody joined to
the wrong check.
"""
from __future__ import annotations

from kernel.verifier import batch, runner
from kernel.verifier.outcome import ERROR, FAIL, FLAKE, PASS

CRITERIA = [
    {"id": "a", "kind": "command", "spec": {"run": "pytest tests/a"}},
    {"id": "b", "kind": "command", "spec": {"run": "pytest tests/b"}},
    {"id": "c", "kind": "command", "spec": {"run": "pytest tests/c"}},
]


def _checks(criteria=None):
    return batch.checks_for({}, criteria or CRITERIA, {})


def _row(exit_code, output="", elapsed=1):
    return {"exit": exit_code, "output": output, "elapsed": elapsed}


# ── the join ───────────────────────────────────────────────────────────

def test_verdicts_land_on_the_criteria_that_earned_them():
    """Positional, so an off-by-one is a story refused for another
    story's failing test."""
    checks = _checks()
    parsed = {0: _row(0), 1: _row(1, "assert 1 == 2"), 2: _row(0)}

    got = batch.outcomes(CRITERIA, checks, parsed, "")

    assert [c["id"] for c, _ in got] == ["a", "b", "c"]
    assert got[0][1].passes == PASS
    assert got[1][1].passes == FAIL
    assert "assert 1 == 2" in str(got[1][1].detail)
    assert got[2][1].passes == PASS


def test_a_criterion_with_no_command_still_takes_its_slot():
    """It is refused on its own account rather than dropped -- dropping
    it would shift every verdict after it onto the wrong check."""
    criteria = [CRITERIA[0], {"id": "empty", "kind": "command", "spec": {}},
                CRITERIA[2]]
    checks = _checks(criteria)

    assert len(checks) == 3
    got = batch.outcomes(criteria, checks, {0: _row(0), 1: _row(127),
                                            2: _row(0)}, "")
    assert [c["id"] for c, _ in got] == ["a", "empty", "c"]
    assert got[2][1].passes == PASS


# ── what it must never invent ──────────────────────────────────────────

def test_a_criterion_the_runner_did_not_report_is_error_not_fail():
    """THE RULE THIS MODULE EXISTS TO HOLD. Truncated output, an evicted
    pod, a batch stopped at its budget -- all machines failing. `fail`
    sends the executor to repair work that was never broken, which is
    the defect execution 201 spent 168 tool calls on."""
    got = batch.outcomes(CRITERIA, _checks(), {0: _row(0), 2: _row(0)}, "")

    assert got[1][1].passes == ERROR
    assert got[0][1].passes == PASS and got[2][1].passes == PASS


def test_a_shell_missing_its_tools_refuses_every_criterion():
    """The preflight is not a property of any one check. A shell with no
    `base64` produced no verdicts at all, and the honest report is that
    none was run -- not that the first one passed."""
    got = batch.outcomes(CRITERIA, _checks(), {0: _row(0)}, "base64 tr")

    assert [o.passes for _, o in got] == [ERROR, ERROR, ERROR]
    assert "base64" in str(got[0][1].detail)


def test_a_transport_failure_says_so_on_every_missing_verdict():
    """An unreachable pod is not a verdict. The message reaches the row
    rather than a log line nobody correlates."""
    got = batch.outcomes(CRITERIA, _checks(), {}, "",
                         "the bridge did not answer")

    assert all(o.passes == ERROR for _, o in got)
    assert "bridge did not answer" in str(got[0][1].detail)


# ── the distinction `_timed_out` exists for ────────────────────────────

def test_a_check_that_exits_124_early_has_FAILED():
    """`timeout` is an ordinary program and a suite may return its
    status. Only a 124 AT the budget is a timeout; reading every 124 as
    one would turn a real failure into `error` and never refuse it."""
    assert not batch._timed_out({"exit": 124, "elapsed": 2}, 600)

    got = batch.outcomes([CRITERIA[0]], _checks([CRITERIA[0]]),
                         {0: _row(124, elapsed=2)}, "")
    assert got[0][1].passes == FAIL


def test_a_check_that_exits_124_at_its_budget_timed_out():
    assert batch._timed_out({"exit": 124, "elapsed": 600}, 600)
    assert batch._timed_out({"exit": 137, "elapsed": 601}, 600)


def test_a_timeout_exit_with_no_clock_is_read_as_a_timeout():
    """The runner signalled a deadline and lost the elapsed time.
    Reading a machine's own signal as the work failing is the wrong
    direction to guess in."""
    assert batch._timed_out({"exit": 124}, 600)


# ── the batch's own budget ─────────────────────────────────────────────

def test_the_batch_budget_is_the_SUM_and_not_the_longest_check():
    """They run in sequence. A budget of the longest check stops the
    batch part-way through, and the checks that never ran come back
    absent -- which this module reports as `error`. A machine's
    arithmetic would become several criteria reported unrunnable."""
    checks = [runner.Check("a", 100), runner.Check("b", 200),
              runner.Check("c", 300)]

    assert batch.budget_for(checks) == 600 + batch.BATCH_MARGIN_S


# ── the per-criterion environment, which one container could lose ──────

def test_two_criteria_targeting_two_mounts_get_two_environments():
    """One container, two mounts. `cd repos/B && pytest` must resolve
    B's pytest -- the shadowing `criterion_env` was written to end, and
    the thing a single shared PATH would silently reintroduce."""
    mounts_env = {
        "repos/A": {"prepend": ["/verify/tree/repos/A/.venv/bin"],
                    "env": {"VIRTUAL_ENV": "/verify/tree/repos/A/.venv"}},
        "repos/B": {"prepend": ["/verify/tree/repos/B/.venv/bin"],
                    "env": {"VIRTUAL_ENV": "/verify/tree/repos/B/.venv"}},
    }
    criteria = [
        {"id": "a", "spec": {"run": "cd repos/A && pytest"}},
        {"id": "b", "spec": {"run": "cd repos/B && pytest"}},
    ]

    checks = batch.checks_for({}, criteria, mounts_env)

    assert checks[0].env.get("VIRTUAL_ENV") == "/verify/tree/repos/A/.venv"
    assert checks[1].env.get("VIRTUAL_ENV") == "/verify/tree/repos/B/.venv"
    assert checks[0].path_prepend.startswith("/verify/tree/repos/A/.venv/bin")
    assert checks[1].path_prepend.startswith("/verify/tree/repos/B/.venv/bin")


def test_the_batch_runs_against_the_volume_and_not_the_tar_path():
    """`volume.TREE`, not `kinds.TREE_PATH`. The second is where the tar
    path unpacked a copy, and 5.5's whole point is that no tree is
    shipped -- a batch pointed there would run against an empty
    directory or, worse, a previous round's."""
    import inspect

    from kernel.verifier import volume

    assert inspect.signature(batch.run).parameters["cwd"].default == \
        volume.TREE


# ── F3's quorum, which the first draft of the volume path dropped ──────

def test_the_volume_path_re_runs_only_what_has_not_passed(monkeypatch):
    """THE REGRESSION THIS FILE EXISTS TO STOP RECURRING.

    The first draft of `verify_claim`'s warm branch ran the batch once
    and returned, with a comment saying quorum was somebody else's item.
    `runs_allowed` exists because checks FLAKE, so a contract declaring
    a quorum would have quietly stopped getting one the day the volume
    path switched on -- and nothing would have failed to say so.

    Re-running the WHOLE batch would be the lazy repair: a green suite
    would pay its full runtime again every round. What F3 means, and
    what one-Job-per-criterion gave for free, is that only the criteria
    still not passing are run again.
    """
    import asyncio

    from kernel.verifier import verify

    rounds: list[list[str]] = []
    # 'b' fails once then passes -- the flake the quorum is for.
    answers = {"a": [PASS, PASS], "b": [FAIL, PASS], "c": [PASS, PASS]}

    async def fake_run(host, contract, criteria, mounts_env, **kw):
        rounds.append([c["id"] for c in criteria])
        out = []
        for criterion in criteria:
            seq = answers[criterion["id"]]
            verdict = seq[min(len(rounds) - 1, len(seq) - 1)]
            out.append((criterion, batch.outcomes(
                [criterion], batch.checks_for({}, [criterion], {}),
                {0: _row(0 if verdict == PASS else 1)}, "")[0][1]))
        return out

    monkeypatch.setattr(batch, "run", fake_run)
    monkeypatch.setattr(verify, "runs_allowed", lambda contract: 2)

    class _Graph:
        network = None
        sidecars = ()

        async def up(self, declared):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(verify, "Services", lambda driver, ident: _Graph())
    monkeypatch.setattr(verify, "declared", lambda contract: ())

    got = asyncio.run(verify.verify_claim(
        None, {}, CRITERIA, "/repo", "c0ffee" * 6,
        warm={"host": "10.0.0.7", "mounts_env": {}}))

    assert rounds[0] == ["a", "b", "c"]
    assert rounds[1] == ["b"], (
        "round two re-ran criteria that had already passed, so a green "
        "suite pays its whole runtime again for every quorum round")
    assert len(rounds) == 2
    # FLAKE, NOT PASS, and that is the point rather than a wrinkle.
    # `quorum.settle` calls a criterion that failed once and passed once
    # exactly that, and it is non-refusing (`outcome.REFUSING` is
    # `(FAIL,)`), so the story is not blocked and the flake is on the
    # record. The volume path must produce the SAME verdict the tar path
    # would for the same sequence -- if batching quietly turned flakes
    # into passes it would be hiding the thing the quorum exists to
    # surface.
    assert {c["id"]: o.passes for c, o in got} == {
        "a": PASS, "b": FLAKE, "c": PASS}


def test_the_volume_path_never_archives_a_tree(monkeypatch):
    """`stage()` is the tar path and the whole point of 5.5 is that the
    volume path does not ship one. A warm verification that still called
    it would pay the archive it exists to avoid, silently."""
    import asyncio

    from kernel.verifier import verify

    async def boom(*a, **kw):
        raise AssertionError("stage() was called on the volume path")

    monkeypatch.setattr(verify, "stage", boom)

    async def fake_run(host, contract, criteria, mounts_env, **kw):
        return batch.outcomes(criteria, batch.checks_for({}, criteria, {}),
                              {i: _row(0) for i in range(len(criteria))}, "")

    monkeypatch.setattr(batch, "run", fake_run)
    monkeypatch.setattr(verify, "runs_allowed", lambda contract: 1)

    class _Graph:
        network = None
        sidecars = ()

        async def up(self, declared):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(verify, "Services", lambda driver, ident: _Graph())
    monkeypatch.setattr(verify, "declared", lambda contract: ())

    got = asyncio.run(verify.verify_claim(
        None, {}, CRITERIA, "/repo", "c0ffee" * 6,
        warm={"host": "10.0.0.7", "mounts_env": {}}))
    assert [o.passes for _, o in got] == [PASS, PASS, PASS]
