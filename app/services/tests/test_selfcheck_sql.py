"""Ec2e — an errand checks its own claim, then a person decides.

THE BUG THIS CLOSES, which is bug 16 in the errand review. The worker
tested `result.get("proposals") and profile.has_verifier` together, so an
errand's `propose_completion` was IGNORED — no Verifier, no branch, nothing
listening. It fell through to the `terminal_command` path, `start_story`
never sets one, and the run exhausted its legs and ended FAILED with
"12 legs without converging". **A successful errand reported as a model
failure.**

The shape now: the claim is measured against the contract the plan
registered, and if it holds the run parks on an `acceptance` — the same
machinery a verified story uses, which already knows that approve means
DONE and decline means another leg with the reason in the brief.

WHY MEASURE AT ALL rather than just asking. Nothing stops a model proposing
completion on its first step. A claim that does not meet its own plan
should cost a leg, not somebody's attention.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from kernel.worker import selfcheck

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-selfcheck-test"


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
            # `search_path = kernel, public` IS THE RUNTIME. Every kernel
            # pool sets it, and `contracts.py` writes unqualified SQL
            # against it. A test connection on the default search_path
            # would exercise different SQL than production runs.
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'kernel' AND table_name IN "
            "('executions','artifact_manifest','criteria','contracts')")
        if ok == 4:
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


async def _errand_with_checks(conn, goal: str, checks) -> tuple[int, dict]:
    """An execution, a contract and its criteria — the real tables.

    Built through `contracts.new_version`, not hand-inserted, so the shape
    under test is the one `register_plan` actually produces. A fixture that
    writes its own rows tests the fixture.
    """
    from kernel.worker.contracts import new_version

    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id) "
        "VALUES ('errand','ops',$1) RETURNING id", goal)
    await new_version(conn, eid, checks, "a plan", "tester")
    return eid, {"id": eid, "container_id": None}


class _Env:
    def __init__(self, root):
        self.workspace_path = root


async def _emit(conn, eid, root, **args):
    from kernel.harness.emit import emit_artifact
    return await emit_artifact(conn, _Env(root), eid, args, "tc")


# A DRIVER FAKE USED TO LIVE HERE, because a command criterion was
# `exec`d back into the live sandbox. Nothing execs any more: artifact
# criteria are answered from the manifest and command criteria are not
# re-run at all -- the executor runs them as its work and reports the
# numbers, and only a Verifier re-measures them. A fake for a call that
# no longer happens would be the test keeping a shape the code has left.


# ── measuring the claim ──────────────────────────────────────────────────

def test_an_artifact_check_refuses_the_claim_until_the_thing_exists(tmp_path):
    """The whole point. An errand that proposes completion having emitted
    nothing has not finished, and the person is not asked."""
    async def body(conn):
        eid, row = await _errand_with_checks(
            conn, f"{PREFIX}-unmet",
            [{"expect": "artifact", "role": "report", "label": "goal"}])

        before = await selfcheck.evaluate(conn, eid)
        assert before["ok"] is False
        assert "0/1 checks passed" in before["summary"]

        (tmp_path / "r.md").write_text("the answer", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="what we found")

        after = await selfcheck.evaluate(conn, eid)
        assert after["ok"] is True
        assert "1/1 checks passed" in after["summary"]
    _run(body)


def test_a_command_check_is_not_re_run_and_the_summary_says_so():
    """THE TRADE, made visible. A command criterion used to be `exec`d
    back into the executor's own sandbox: a second full run of a suite
    the executor had just run as its work, and -- because it went
    through a foreground `bash` -- one that could never exceed two
    minutes, so any slower repository could never claim at all.

    It is not re-run now. What a person is shown must therefore say
    which checks were MEASURED and which were taken on the executor's
    word, because "1/1 checks passed" over a contract whose only check
    nobody ran is the green tick this design exists to withhold."""
    async def body(conn):
        eid, _ = await _errand_with_checks(
            conn, f"{PREFIX}-cmd",
            [{"run": "test -f out.csv", "label": "goal"}])

        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is True, "an unmeasured check must not refuse"
        assert got["results"][0]["outcome"] == "unmeasured"
        assert "0/1 checks passed" in got["summary"]
        assert "taken on the executor's word" in got["summary"]
        assert "test -f out.csv" in got["summary"]
    _run(body)


def test_nothing_records_an_observation_nobody_made():
    """`criterion_results.passes` is constrained to
    pass/fail/flake/error, so a row saying `unmeasured` would be refused
    by the CHECK and swallowed by the try/except that logs and carries
    on -- the Stage L shape. It is also a contradiction in terms: this
    table holds observations, and a check nobody ran produced none."""
    async def body(conn):
        eid, _ = await _errand_with_checks(
            conn, f"{PREFIX}-norow",
            [{"run": "pytest", "label": "goal"}])
        await selfcheck.evaluate(conn, eid)
        rows = await conn.fetch(
            "SELECT passes FROM criterion_results WHERE execution_id = $1",
            eid)
        assert rows == [], (
            "a criterion nobody ran has a row claiming it was observed")
    _run(body)


def test_a_check_nobody_ran_does_not_refuse():
    """R1, in its surviving form: only a check that RAN and said no may
    refuse. Verification being off is not a fault, and must never read
    as a verdict about the work."""
    async def body(conn):
        eid, _ = await _errand_with_checks(
            conn, f"{PREFIX}-nosandbox",
            [{"run": "pytest", "label": "goal"}])

        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is True, "an unmeasured check must not refuse"
        assert got["results"][0]["outcome"] == "unmeasured"
    _run(body)


def test_a_run_with_no_checks_says_the_claim_rests_on_nothing():
    """Honest rather than convenient. The person is still asked — they are
    the last word — but the summary tells them what they are accepting."""
    async def body(conn):
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", f"{PREFIX}-bare")
        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is True
        assert "rests on the executor's word alone" in got["summary"]
    _run(body)


def test_mixed_checks_are_all_measured(tmp_path):
    """A plan may carry both. One failing anywhere refuses the claim."""
    async def body(conn):
        eid, row = await _errand_with_checks(
            conn, f"{PREFIX}-mixed",
            [{"expect": "artifact", "role": "report", "label": "goal"},
             {"run": "test -f x", "label": "guard"}])
        row["container_id"] = "c1"

        (tmp_path / "r.md").write_text("done", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="s")

        # The artifact half is measured and passes; the command half is
        # not measured and cannot refuse. The claim holds, and the
        # summary is what carries the caveat to a person.
        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is True
        kinds = {r["kind"]: r["outcome"] for r in got["results"]}
        assert kinds == {"artifact": "pass", "command": "unmeasured"}
        assert "taken on the executor's word" in got["summary"]
    _run(body)


def test_the_criteria_come_back_with_their_kind_intact():
    """`_insert_criteria` used to hardcode kind='command'. If it regresses,
    an artifact criterion is read back as a command and shelled out."""
    async def body(conn):
        eid, _ = await _errand_with_checks(
            conn, f"{PREFIX}-kinds",
            [{"expect": "artifact", "role": "report", "label": "goal"},
             {"run": "pytest", "label": "guard"}])
        kinds = [c["kind"] for c in await selfcheck.criteria_for(conn, eid)]
        assert sorted(kinds) == ["artifact", "command"]
    _run(body)


# ── what the executor is told ────────────────────────────────────────────

def test_the_corrective_names_the_check_and_what_it_saw():
    """"your checks failed" teaches nothing, and the executor has a
    bounded number of legs left to work it out."""
    said = selfcheck.corrective({"results": [
        {"outcome": "fail", "label": "goal", "check": "artifact[role=report]",
         "detail": "this run emitted log:run.log"},
        {"outcome": "pass", "label": "guard", "check": "test -f x",
         "detail": ""},
    ]})
    assert "artifact[role=report]" in said
    assert "log:run.log" in said
    assert "test -f x" not in said, "a passing check is not a correction"
    assert "propose completion again" in said


def test_the_corrective_says_what_to_do_about_a_wrong_check():
    """Without this an executor that believes the check is wrong has one
    move left — work around it — which is the failure the env guard
    already learned about the hard way."""
    said = selfcheck.corrective({"results": [
        {"outcome": "fail", "label": "goal", "check": "c", "detail": "d"}]})
    assert "do not work around it" in said


# ── the worker routes it ─────────────────────────────────────────────────

def test_the_worker_no_longer_ignores_an_unverified_completion():
    """BUG 16. `proposals` was tested together with `has_verifier`, so an
    errand's claim went nowhere and the run died of old age."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/worker/loop.py", encoding="utf-8").read()
    assert 'result.get("proposals") and profile.allows_completion' in src
    assert "_finish_unverified" in src

    body = src[src.index("async def _finish_unverified"):]
    body = body[:body.index("async def _run_terminal")]

    # IT MUST NEVER FINISH A RUN NOBODY AUTHORISED. Approve is a
    # person's word, and `resolve_request` is what turns it into DONE.
    #
    # BACK TO ITS ORIGINAL, STRONGER FORM. I5d narrowed this to "every
    # DONE sits under the grant", because a standing grant was a
    # person's word recorded before the fact and finished the run here.
    # That branch is gone: finishing is not only "was the plan carried
    # out" but "is this enough?", and the second cannot be answered in
    # advance. The function parks on an acceptance and finishes nothing,
    # so the guard can assert what it always meant -- NO DONE, anywhere
    # in this path.
    assert "'DONE'" not in body and '"DONE"' not in body, (
        "the self-check path finishes the run without asking anybody")
    assert "standing_approval" not in body, (
        "the completion gate honours a grant again; an errand would "
        "close with nobody reading what it produced")

    assert "open_request" in body and "acceptance" in body
    # And a failed park must not fall through to finishing — the story
    # path learned that a gate which opens when its own machinery fails
    # is not a gate.
    assert "acceptance_blocked" in body


# ── I1g · the self-check's observations are recorded ─────────────────────

def test_the_selfcheck_records_what_it_observed(tmp_path):
    """They went into the acceptance payload and nowhere else, so the
    ticker's check half was dead for every errand and `story_progress`
    showed a check with no observation beside it."""
    async def body(conn):
        eid, row = await _errand_with_checks(
            conn, f"{PREFIX}-record",
            [{"expect": "artifact", "role": "report", "label": "goal"}])
        (tmp_path / "r.md").write_text("the answer", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="what we found")

        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is True

        rows = await conn.fetch(
            "SELECT passes, written_by, claim_seq, detail::text AS detail "
            "FROM criterion_results WHERE execution_id = $1", eid)
        assert len(rows) == 1, "the observation was not recorded"
        assert rows[0]["passes"] == "pass"
        assert rows[0]["written_by"] == "selfcheck", (
            "a self-check's row must be distinguishable from a Verifier's "
            "— one ran in a fresh container under another uid, the other "
            "in the executor's own live sandbox")
        assert rows[0]["claim_seq"] == 0
        assert "artifact[role=report]" in rows[0]["detail"]
    _run(body)


def test_re_proposing_completion_does_not_fail_on_its_own_rows():
    """APPEND-ONLY AND IDEMPOTENT. A declined acceptance sends the errand
    back for another leg, and it re-runs the same checks — a unique
    violation there would take down the second proposal."""
    async def body(conn):
        eid, _ = await _errand_with_checks(
            conn, f"{PREFIX}-again",
            [{"expect": "artifact", "role": "report", "label": "goal"}])
        # AN ARTIFACT CRITERION, because a command one now records
        # nothing at all -- nobody observed it. The property under test
        # is unchanged: the same observation, written twice, must not
        # take down the second proposal.
        for _ in range(3):
            await selfcheck.evaluate(conn, eid)
        count = await conn.fetchval(
            "SELECT count(*) FROM criterion_results WHERE execution_id = $1",
            eid)
        assert count == 1, f"{count} rows for one criterion in one run"
    _run(body)


def test_a_failing_check_is_recorded_too():
    """Evidence is not only the good news. A person deciding whether to
    accept needs to see what the run's own checks said."""
    async def body(conn):
        eid, row = await _errand_with_checks(
            conn, f"{PREFIX}-fail",
            [{"expect": "artifact", "role": "report", "label": "goal"}])
        got = await selfcheck.evaluate(conn, eid)
        assert got["ok"] is False
        rows = await conn.fetch(
            "SELECT passes FROM criterion_results WHERE execution_id = $1",
            eid)
        assert [r["passes"] for r in rows] == ["fail"]
    _run(body)


def test_the_worker_may_actually_insert():
    """THE GRANT, checked as a grant. `agentos_worker` held SELECT only,
    so the insert would have been denied inside the try/except that logs
    and carries on — a dead feature reading as a working one. That is the
    Stage L failure, which cost 3,850 silent denials the first time."""
    async def body(conn):
        rows = await conn.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema='kernel' AND table_name='criterion_results' "
            "AND grantee='agentos_worker'")
        held = {r["privilege_type"] for r in rows}
        assert "INSERT" in held, "the worker cannot record its own checks"
        assert "UPDATE" not in held, (
            "append-only evidence: a judge that can rewrite its past "
            "observations is not producing evidence")
    _run(body)
