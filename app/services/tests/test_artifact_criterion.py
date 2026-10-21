"""Ec2d — `artifact[role=report]`: done means the deliverable exists.

Every criterion before this is a `command`: a shell line run in a fresh
container against the committed tree, judged by its exit code. That is the
right shape when the deliverable IS the tree. It is the wrong shape for an
errand, whose tree is discarded — there is no command that proves a report
was written, only commands that inspect a workspace nobody keeps.

Ec2d's test is "a plan whose goal check is `artifact[role=report]` fails
before the report exists and passes after", and it is at the bottom of this
file against a real database. Everything above it is the parsing and the
refusals, which need none.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from kernel.verifier.artifact_kind import (ROLES, SPEC_KEYS, describe,
                                           evaluate, matches, parse, problems)
from kernel.verifier.outcome import ERROR, FAIL, PASS
from kernel.worker.contracts import normalise

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-crit-test"


# ── the notation, both directions ────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("artifact[role=report]", {"role": "report"}),
    ("artifact[role=report,name=march.md]", {"role": "report",
                                             "name": "march.md"}),
    ("artifact[ role = report ]", {"role": "report"}),
    ("ARTIFACT[role=report]", {"role": "report"}),
    ("artifact", {}),
])
def test_the_notation_the_prompt_teaches_is_the_notation_that_parses(
        text, expected):
    """A model shown `artifact[role=report]` will type it, and a person
    amending a story types into a textarea. If the notation in the prompt
    is not the notation that works, the prompt is a trap."""
    assert parse(text) == expected


@pytest.mark.parametrize("text", [
    "pytest tests/", "artifactory --version", "cd repos/x && artifact",
    "", "echo artifact[role=report]",
])
def test_a_command_that_merely_mentions_it_is_still_a_command(text):
    """`artifactory --version` starts with the word. Treating it as an
    artifact check would silently stop running a real check."""
    assert parse(text) is None


def test_none_and_empty_are_different_answers():
    """THE DISTINCTION THAT MATTERS. `None` is "not an artifact check";
    `{}` is a badly written one. Conflating them sends a bare `artifact`
    to a container to be shelled out, where it returns `command not found`
    — an answer about the image, dressed as an answer about the check."""
    assert parse("pytest") is None
    assert parse("artifact") == {}
    assert parse("artifact") is not None


def test_describe_round_trips():
    for spec in ({"role": "report"},
                 {"role": "dataset", "name": "rows.csv"},
                 {"role": "log", "name": "run.log", "min_bytes": "10"}):
        assert parse(describe(spec)) == spec


# ── what is refused at registration ──────────────────────────────────────

def test_a_check_that_cannot_fail_is_refused():
    """Satisfied by any artifact at all, including a stray log. A check
    that cannot distinguish done from not-done is not a check."""
    said = problems({})
    assert said and all(k in said[0] for k in SPEC_KEYS)


@pytest.mark.parametrize("spec,fragment", [
    ({"role": "summary"}, "not one of"),
    ({"min_bytes": "lots"}, "not a number"),
    ({"colour": "red"}, "unknown key"),
])
def test_an_unusable_spec_says_what_is_wrong(spec, fragment):
    """Refused at REGISTRATION, where somebody can still fix it. The
    alternative is the errand running in full and then being turned down by
    a check that was broken when it was written."""
    said = " ".join(problems(spec))
    assert fragment in said


@pytest.mark.parametrize("role", ROLES)
def test_every_role_the_manifest_accepts_is_a_valid_check(role):
    """The criterion's vocabulary and the table's CHECK constraint are two
    statements of one list. A role the manifest stores and the criterion
    rejects is an artifact nothing can ever check for."""
    assert problems({"role": role}) == []


def test_register_plan_refuses_an_unusable_artifact_check():
    """The claim in `problems`'s docstring, made true."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/harness/tools_worker/plan.py",
                  encoding="utf-8").read()
    assert "artifact_kind import problems" in src
    assert "not usable as" in src


# ── matching ─────────────────────────────────────────────────────────────

def _row(role="report", name="march.md", size=500):
    return {"role": role, "name": name, "bytes": size,
            "artifact_id": "sha", "summary": ""}


def test_name_matching_is_exact_not_a_glob():
    """A check matching `report*` passes on `report-draft-DO-NOT-USE.md`,
    and an executor choosing its own filenames must not be able to widen
    its own check by naming things loosely."""
    assert matches({"name": "march.md"}, _row(name="March.MD"))
    assert not matches({"name": "march"}, _row(name="march.md"))
    assert not matches({"name": "march.md*"}, _row(name="march.md"))


def test_min_bytes_stops_an_empty_file_passing():
    assert matches({"min_bytes": "400"}, _row(size=500))
    assert not matches({"min_bytes": "600"}, _row(size=500))
    assert not matches({"role": "report", "min_bytes": "1"}, _row(size=0))


def test_an_unconstrained_key_does_not_narrow():
    assert matches({"role": "report"}, _row(name="anything.txt"))
    assert not matches({"role": "dataset"}, _row(role="report"))


# ── the check shape reaches the contract ─────────────────────────────────

def test_a_plan_can_carry_an_artifact_check():
    got = normalise([{"expect": "artifact", "role": "report",
                      "label": "goal"}])[0]
    assert got["kind"] == "artifact"
    assert got["spec"] == {"role": "report"}
    assert got["run"] == "artifact[role=report]", (
        "every reader of a contract prints `run`; a criterion that renders "
        "blank is one a person cannot review")


def test_a_command_check_is_untouched():
    """Story executors write these, and nothing about them may change."""
    got = normalise([{"run": "cd repos/o__n && pytest", "label": "guard"}])[0]
    assert got == {"kind": "command", "run": "cd repos/o__n && pytest",
                   "spec": {}, "label": "guard"}


# THE DRY RUN'S VERSION OF THIS TEST RETIRED WITH THE DRY RUN (E9, plan
# 5.6). It asserted that `dryrun.py` split the command criteria out
# before shelling anything, because `artifact[role=report]` in a
# container returns `command not found` -- an answer about the image
# dressed as an answer about the check. The rule it protected is
# unchanged and is still covered below, one layer down, by the registry
# test: `artifact` routes to `evaluated_on_the_worker` and never to the
# container evaluator, so no caller can make that mistake again.


def test_the_verifier_registry_says_where_it_is_evaluated():
    """Reaching the container evaluator means one leaked past both worker
    paths. That is a wiring bug and must say so, not pass quietly."""
    from kernel.verifier.kinds import KINDS, evaluator_for

    assert KINDS["artifact"] is not KINDS["metric"], (
        "artifact is still the generic not_implemented stub")
    assert evaluator_for("artifact").__name__ == "evaluated_on_the_worker"


# ── against a real database ──────────────────────────────────────────────

def _candidate_dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    for dsn in _candidate_dsns():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'kernel' "
            "AND table_name IN ('executions','artifact_manifest')")
        if ok == 2:
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema — start the stack")


def _run(body):
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(_go())


def test_it_fails_before_the_report_exists_and_passes_after(tmp_path):
    """EC2D'S TEST, end to end and in that order.

    FAIL rather than ERROR when nothing matches: the check RAN and said no.
    R1 reserves `error` for a check that could not say anything, and "the
    deliverable is not there" is very much something.
    """
    async def body(conn):
        from kernel.harness.emit import emit_artifact

        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", f"{PREFIX}-order")
        criterion = {"id": -1, "kind": "artifact",
                     "spec": {"run": "artifact[role=report]",
                              "role": "report"}}

        before = await evaluate(conn, eid, criterion)
        assert before.passes == FAIL, before.detail
        assert "emitted nothing" in before.detail["reason"]

        class _Env:
            workspace_path = str(tmp_path)
        (tmp_path / "out.md").write_text("the answer", encoding="utf-8")
        await emit_artifact(conn, _Env(), eid,
                            {"path": "out.md", "role": "report",
                             "summary": "what we found"}, "tc")

        after = await evaluate(conn, eid, criterion)
        assert after.passes == PASS, after.detail
        assert after.detail["artifacts"][0]["name"] == "out.md"
    _run(body)


def test_the_wrong_role_fails_and_names_what_was_emitted(tmp_path):
    """"no matching artifact" leaves the executor guessing whether it
    emitted the wrong role, the wrong name, or nothing — and it has one leg
    left to work it out."""
    async def body(conn):
        from kernel.harness.emit import emit_artifact

        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", f"{PREFIX}-wrong")

        class _Env:
            workspace_path = str(tmp_path)
        (tmp_path / "run.log").write_text("noise", encoding="utf-8")
        await emit_artifact(conn, _Env(), eid,
                            {"path": "run.log", "role": "log",
                             "summary": "the run"}, "tc")

        out = await evaluate(conn, eid,
                             {"id": -1, "kind": "artifact",
                              "spec": {"role": "report"}})
        assert out.passes == FAIL
        assert "log:run.log" in out.detail["reason"]
    _run(body)


def test_another_execution_cannot_satisfy_this_ones_check(tmp_path):
    """Scoped to THIS execution. Widening it to the goal would let a run
    pass on last week's report — the "already done" false green the corpus
    work spent four attempts on."""
    async def body(conn):
        from kernel.harness.emit import emit_artifact

        goal = f"{PREFIX}-scope"
        first = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", goal)
        second = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", goal)

        class _Env:
            workspace_path = str(tmp_path)
        (tmp_path / "r.md").write_text("last week", encoding="utf-8")
        await emit_artifact(conn, _Env(), first,
                            {"path": "r.md", "role": "report",
                             "summary": "old"}, "tc")

        criterion = {"id": -1, "kind": "artifact", "spec": {"role": "report"}}
        assert (await evaluate(conn, first, criterion)).passes == PASS
        assert (await evaluate(conn, second, criterion)).passes == FAIL
    _run(body)


def test_a_broken_spec_is_error_not_fail():
    """R1: only a check that RAN and said no may refuse. A spec nobody can
    evaluate said nothing, and refusing the story for it blames the work
    for a mistake in the check."""
    async def body(conn):
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", f"{PREFIX}-broken")
        out = await evaluate(conn, eid,
                             {"id": -1, "kind": "artifact",
                              "spec": {"role": "nonsense"}})
        assert out.passes == ERROR
        assert "not one of" in out.detail["reason"]
    _run(body)
