"""D2 — an artifact is readable, downloadable, and a resource.

WHAT ROUND 2 SHOWED. An errand produced a 13KB report, a dataset and a
method script. Then both agents, asked what it found, answered from
domain knowledge having made zero tool calls — and one reported an error
code for a call it never made. A person could read an agent's account of
the report and never the report.

Three gaps, and they compound:

  * GO held `list_artifacts` and not `read_artifact` — it could see a
    report existed and never what it said, which made inventing the
    contents the path of least resistance;
  * `emit_artifact` wrote the manifest and stopped, so an artifact was
    readable by AGENTS and was not a RESOURCE — ungrantable, unmountable,
    unreachable by any later executor;
  * nothing let a PERSON download one, so the agents' accounts could not
    be checked.

Provenance runs through all of it: the first question asked of a finding
is where it came from, and a catalogue of files called `report.md`
answers nothing.
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"

asyncpg = pytest.importorskip("asyncpg")


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
    pytest.skip("no local database")


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


async def _run_with_artifact(conn, role="report", kind="errand"):
    """A goal, an errand, and one emitted artifact in the manifest."""
    tag = os.urandom(4).hex()
    goal, story = f"zz-d2-{tag}", f"zzs{tag[:6]}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by, work_kind) "
        "VALUES ($1,$2,'t','accepted','alice',$3)", story, goal,
        "errand" if kind == "errand" else None)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state) "
        "VALUES ($3,'ops',$1,$2,'RUNNING') RETURNING id", goal, story, kind)
    art = "a" * 64
    await conn.execute(
        "INSERT INTO kernel.artifact_manifest (execution_id, artifact_id, "
        "role, name, media_type, summary, bytes) "
        "VALUES ($1,$2,$3,'findings.md','text/markdown','what it found',99)",
        eid, art, role)
    return {"goal": goal, "story": story, "eid": eid, "artifact": art}


# ── the artifact becomes a resource ──────────────────────────────────────

def test_an_emitted_report_is_catalogued_with_its_provenance():
    """THE CHAIN THE ERRAND SHAPE EXISTS FOR. "Investigate X and fix it"
    is an errand whose report is the input to a story — and before this
    the report could inform what GO planned while being unreachable by
    the run that acted on it."""
    async def body(conn):
        from kernel.harness.artifact_resource import register

        f = await _run_with_artifact(conn)
        rid = await register(conn, f["eid"], f["artifact"], "report",
                             "findings.md", "what it found", "text/markdown",
                             99)
        assert rid, "a report was not catalogued"
        row = await conn.fetchrow(
            "SELECT kind, name, uri, role, default_writable, metadata::text, "
            "file_size_bytes FROM goal_resources WHERE id = $1", rid)
        assert row["kind"] == "document"
        assert row["role"] == "artifact"
        assert row["uri"] == f"artifact://{f['artifact']}"
        # PROVENANCE IN THE NAME, because that is what a person reads in
        # a picker and what an agent grants BY.
        assert f["story"] in row["name"], row["name"]
        assert "errand" in row["name"]
        # AND AS DATA, so a query can answer "what did story X produce".
        import json

        meta = json.loads(row["metadata"])
        assert meta["story_id"] == f["story"]
        assert meta["execution_id"] == f["eid"]
        assert meta["artifact_id"] == f["artifact"]
    _run(body)


def test_a_catalogued_artifact_is_read_only():
    """An artifact is a RECORD of what a run found. A later story editing
    it in place would rewrite history other work has already cited; a new
    finding is a new artifact, chained by `supersedes`."""
    async def body(conn):
        from kernel.harness.artifact_resource import register

        f = await _run_with_artifact(conn)
        rid = await register(conn, f["eid"], f["artifact"], "report",
                             "findings.md", "s", "text/markdown", 99)
        assert await conn.fetchval(
            "SELECT default_writable FROM goal_resources WHERE id=$1",
            rid) is False
    _run(body)


@pytest.mark.parametrize("role,catalogued", [
    ("report", True), ("dataset", True), ("method", True),
    ("log", False), ("figure", False)])
def test_only_the_roles_meant_to_outlive_the_run_are_catalogued(role,
                                                                catalogued):
    """Registering every log would fill the catalogue with scratch, and a
    catalogue nobody can scan is one nobody uses. `method` is in because
    a dataset without the script that made it is not reproducible."""
    async def body(conn):
        from kernel.harness.artifact_resource import register

        f = await _run_with_artifact(conn, role=role)
        rid = await register(conn, f["eid"], f["artifact"], role, "x", "s",
                             "text/plain", 10)
        assert (rid is not None) is catalogued
    _run(body)


def test_re_emitting_identical_bytes_does_not_grow_the_catalogue():
    """The id is derived from the content hash, so a recurring errand
    that produces the same report every week registers one row, not
    fifty-two."""
    async def body(conn):
        from kernel.harness.artifact_resource import register

        f = await _run_with_artifact(conn)
        a = await register(conn, f["eid"], f["artifact"], "report", "x", "s",
                           "text/plain", 10)
        b = await register(conn, f["eid"], f["artifact"], "report", "x", "s",
                           "text/plain", 10)
        assert a == b
        assert 1 == await conn.fetchval(
            "SELECT count(*) FROM goal_resources WHERE id = $1", a)
    _run(body)


def test_emit_registers_without_being_asked():
    """The registration has to happen IN `emit_artifact`. A second tool
    would need the errand to hold a resource it has not produced yet,
    which is why `publish_artifact` could not do this job."""
    src = _read("kernel/harness/emit.py")
    assert "artifact_resource import register" in src
    # And it must not be able to lose the artifact: the manifest row is
    # what keeps the work.
    tail = src[src.index("artifact_resource import register"):]
    assert "except Exception" in tail, (
        "a catalogue failure can lose an artifact the errand cannot "
        "produce again")


# ── a person can see and download them ───────────────────────────────────

def test_the_download_route_is_scoped_to_the_goal():
    """The content store is addressed by hash and is GOAL-BLIND — it
    cannot enforce who may read what. The manifest join is what scopes
    it, so an id from another goal's run is a 404 even though the bytes
    exist."""
    src = _read("app/routers/artifacts.py")
    body = src[src.index("async def download_artifact"):]
    assert "_require_member(" in body
    # THE PROPERTY, NOT ONE SPELLING. This asserted the literal
    # `AND e.goal_id = $2`, which stopped being the whole truth when an
    # artifact gained a second possible owner: a LOCAL run has no execution,
    # so the row carries its own `goal_id` and the filter became
    # `COALESCE(e.goal_id, m.goal_id)`. The guard is unchanged -- every row,
    # whichever lane wrote it, is still matched against the goal in the URL.
    assert "goal_id) = $2" in body or "goal_id = $2" in body, (
        "the download is not scoped to the goal, so any member of any "
        "goal can fetch any artifact by hash")
    assert "m.goal_id" in body, (
        "a story-keyed artifact has no execution to scope through, so "
        "omitting m.goal_id either hides it or leaves it unscoped")


def test_the_listing_says_which_run_produced_each_artifact():
    src = _read("app/routers/artifacts.py")
    assert '"produced_by"' in src
    for field in ("execution_id", "story_id", "story_title"):
        assert field in src, f"provenance is missing {field}"


def test_a_missing_blob_is_not_reported_as_a_missing_row():
    """A manifest row without bytes is a different fault from an id that
    does not exist, and saying so saves somebody checking permissions
    when the store is what is wrong."""
    src = _read("app/routers/artifacts.py")
    assert "status_code=410" in src
    assert "status_code=404" in src


def test_superseded_versions_are_listed_and_marked():
    """A person comparing what a re-run changed needs both versions; a
    list that silently drops the old one makes that impossible."""
    src = _read("app/routers/artifacts.py")
    assert '"supersedes"' in src


# ── both agents can read, not only list ──────────────────────────────────

def test_go_can_read_an_artifact_and_not_only_list_it():
    """THE ASYMMETRY THAT CAUSED THE FABRICATION. GO could see a report
    existed and never what it said, so answering from domain knowledge
    was the path of least resistance."""
    from kernel.assistant.api import GO_READS, GO_ROUTES
    from kernel.go import TOOLS

    assert "read_artifact" in TOOLS
    assert "read_artifact" in GO_READS
    assert set(TOOLS) == set(GO_ROUTES), sorted(set(TOOLS) ^ set(GO_ROUTES))


def test_go_dispatches_read_artifact_to_the_same_function():
    """Two implementations of "read this artifact" would answer
    differently the first time one grew a bound."""
    src = _read("kernel/assistant/api.py")
    go = src[src.index("async def _dispatch_go"):]
    assert 'if tool == "read_artifact":' in go
    assert "return read_artifact(" in go


@pytest.mark.parametrize("rel", ["kernel/go/AGENTS.md",
                                 "kernel/assistant/AGENTS.md"])
def test_both_prompts_forbid_describing_what_was_not_read(rel):
    """The tool alone would not have prevented this: the ASSISTANT had
    `read_artifact`, made no call, and described the contents anyway."""
    import re

    text = re.sub(r"\s+", " ", _read(rel)).lower()
    # THE HARDENED WORDING. The first version said "if a tool WOULD
    # answer the question, call it" — escapable by a model that decides
    # it would not, which is exactly the judgement a confabulating model
    # has already got wrong. And it fenced the rule to artifacts, while
    # the failure mode covers plans, check output and decisions too.
    assert "if you have not called it this turn, you have not read it" in text
    assert "never state the contents of something you have not read" in text
    assert "reporting an error from a call you never made" in text
    # It must not be escapable by scope: not artifacts only.
    assert "artifacts, plans, a check's output, a decision" in text


@pytest.mark.parametrize("rel", ["kernel/go/AGENTS.md",
                                 "kernel/assistant/AGENTS.md"])
def test_both_agents_are_told_artifacts_join_the_catalogue(rel):
    """D2 PUT A NEW CLASS OF ROW IN A TOOL BOTH AGENTS ALREADY CALL. An
    `art_<hash>` row appears in the catalogue with nobody having
    registered it, and an agent that does not know why will either
    ignore it or try to write it. Two things have to be said: where the
    rows come from, and that they are read-only — the second because the
    Resources section otherwise promises documents and datasets may be
    granted write, which is exactly what an artifact resource refuses."""
    import re

    text = re.sub(r"\s+", " ", _read(rel)).lower()
    assert "art_" in text, "the new row class is never named"
    assert "read_artifact" in text and "read_resource" in text, (
        "the prompt does not say which reader reaches an artifact — and "
        "`read_resource` resolves documents through GCS, which knows "
        "nothing about a content-store hash")
    assert "a new finding is a new artifact" in text, (
        "nothing tells the agent what to do INSTEAD of editing a record")


def test_go_is_told_the_grant_chain_closes_by_itself():
    """The point of registering artifacts was the errand-then-story
    chain: "investigate X" produces a report that "fix X" must read. GO
    is the only agent that can grant, so GO is the one that has to know
    the report is grantable without anybody registering it."""
    import re

    text = re.sub(r"\s+", " ", _read("kernel/go/AGENTS.md")).lower()
    assert "grant_resource` its report" in text or "grant its report" in text
    assert "`needs`" in text
