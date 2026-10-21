"""The findings from the first live round, fixed.

Nine of the ten were the same shape — one fact declared in two places
that drifted apart — and the fixes are correspondingly small. What makes
them worth guarding is that every one was invisible to a passing test
suite, because nothing compared the two declarations.

  F1  GO was told it could register a repository; the code writes a row
      missing every column the engine needs.
  F2  A derived feature lane said `writable: true` and `can_write: false`
      at once — granted as `produces` with a ceiling forbidding writes.
  F5  GO could settle a clarification on a STORY, which the wake was
      filtered against in three places and the tool in none.
  F6  Deleting a thread left `agent_instances.thread_id` pointing at it,
      and the instance stayed `active` — a goal with no reachable
      assistant and nothing saying why.
  F7  A user could not open an assistant session without a story.
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

from kernel.assistant.context import Refused  # noqa: E402


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


# ── F1 · registering a repository takes a human ──────────────────────────

def test_go_cannot_register_a_repository():
    """It could, and the row it wrote was missing `repo_owner`,
    `repo_name`, `default_branch`, `branch_name` and `git_repo_id` — read
    by six modules including branch derivation and GO's own
    `plan_preview`. So it granted, mounted, and failed at derivation with
    nothing naming the cause."""
    async def body(conn):
        from kernel.go.tools import register_resource

        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES "
            "('zz-f1','t','a') ON CONFLICT DO NOTHING")
        got = await register_resource(
            conn, "zz-f1", "api", "https://github.com/o/n", kind="repo")
        assert isinstance(got, Refused)
        assert got.code == "E_REPO_NEEDS_A_HUMAN"
        # It must say who to ask, or GO retries with different wording.
        assert "person" in got.message or "human" in got.message.lower()
    _run(body)


@pytest.mark.parametrize("kind", ["doc"])
def test_go_can_still_register_what_it_can_complete(kind):
    """The refusal is about repositories, not about registering. A
    document needs none of the columns a repo does."""
    async def body(conn):
        from kernel.go.tools import register_resource

        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES "
            "('zz-f1b','t','a') ON CONFLICT DO NOTHING")
        got = await register_resource(
            conn, "zz-f1b", "spec", "https://example.com/spec.pdf", kind=kind)
        assert not isinstance(got, Refused), got
    _run(body)


# ── F2 · a lane's ceiling permits what it is granted for ─────────────────

def test_a_derived_feature_lane_may_be_written():
    """It was granted to its member stories as `produces` — a WRITE
    relation — with `default_writable` left at the column default of
    false. `list_resources` then reported `writable: true` from the mount
    and `can_write: false` from the catalogue, and the assistant is told
    in as many words that `can_write: false` means read-only by policy.

    So the lane was writable and never-writable at once, and the
    assistant correctly refused to guess which branch to work on."""
    import re

    src = _read("app/services/git_graph/branches.py")
    insert = src[src.index("INSERT INTO goal_resources"):]
    insert = insert[:insert.index("resource_id, goal_id,")]
    # COMMENTS STRIPPED. The explanation above the statement says
    # "default_writable" too, so a plain substring match stayed green
    # through a mutation that removed the column AND its value — caught
    # by red-checking, twice in one session.
    code = " ".join(l for l in insert.splitlines()
                    if not l.strip().startswith("#"))
    assert "default_writable" in code, (
        "the derived lane takes the column default (false) while being "
        "granted as `produces` in the same function")
    # And the value, not just the name in the column list.
    assert re.search(r"'active',\s*true,\s*true\)", code), (
        "default_writable is named but not given a value")


def test_the_lane_is_still_granted_as_produces():
    """The other half of the contradiction. If this ever became `needs`,
    setting the ceiling would be the wrong fix."""
    src = _read("app/services/git_graph/branches.py")
    assert "'produces'" in src


# ── F5 · a story is not GO's to answer ───────────────────────────────────

def test_go_cannot_settle_a_question_on_a_story():
    """I5e's wake was filtered to errands in three places; the tool in
    none. A story already has an assistant whose entire job is that
    story, so two agents could answer one question and whichever got
    there first would win."""
    async def body(conn):
        from kernel.go.answers import may_answer

        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES "
            "('zz-f5','t','a') ON CONFLICT DO NOTHING")
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
            "VALUES ('story','coding','zz-f5','RUNNING') RETURNING id")
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, question_text,"
            " payload, status, generation, content_hash) VALUES "
            "($1,'clarification','q','{}'::jsonb,'OPEN',0,'h-f5') RETURNING id",
            eid)
        got = await may_answer(conn, qid, "go")
        assert got is not None and got.code == "E_NOT_YOUR_RUN"
        assert "goal thread" in got.message, (
            "GO is refused without being told what IS useful, so it will "
            "either retry or do nothing")
    _run(body)


def test_go_can_still_settle_a_question_on_an_errand():
    """The capability I5e was built for, unchanged."""
    async def body(conn):
        from kernel.go.answers import may_answer

        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES "
            "('zz-f5b','t','a') ON CONFLICT DO NOTHING")
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
            "VALUES ('errand','ops','zz-f5b','RUNNING') RETURNING id")
        qid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, kind, question_text,"
            " payload, status, generation, content_hash) VALUES "
            "($1,'clarification','q','{}'::jsonb,'OPEN',0,'h-f5b') "
            "RETURNING id", eid)
        assert await may_answer(conn, qid, "go") is None
    _run(body)


# ── F6 · deleting a thread unbinds its instance ──────────────────────────

def test_deleting_a_thread_clears_the_instance_pointer():
    """The binding is one fact in two columns. Deleting the thread left
    `agent_instances.thread_id` pointing at a row that no longer existed
    while the instance stayed `active` — so every listing read as healthy
    and the goal had no assistant anybody could open."""
    src = _read("app/routers/chat.py")
    body = src[src.index("async def delete_thread"):]
    body = body[:body.index("DELETE FROM threads WHERE id = $1")]
    assert "UPDATE agent_instances SET thread_id = NULL" in body, (
        "the reverse pointer survives the thread it points at")


def test_the_instance_is_unbound_and_not_destroyed():
    """Destroying goes through the gateway and takes the workspace with
    it, including the accumulated MEMORY.md — which is why the cleanup
    watchdog is disabled. An unbound instance is re-bindable; a destroyed
    one has lost its memory."""
    src = _read("app/routers/chat.py")
    body = src[src.index("async def delete_thread"):]
    body = body[:body.index("DELETE FROM threads WHERE id = $1")]
    assert "status = 'destroyed'" not in body


# ── F7 · an assistant session without a story ────────────────────────────

def test_the_chat_page_creates_a_thread_when_there_is_none():
    """Asked for directly. The page used to fall through and render with
    NO thread, so the client connected to the empty string and sat on
    "connecting" for ever — and an assistant thread was only ever created
    as a side effect of a story."""
    src = _read("app/routers/chat.py")
    body = src[src.index("if not thread_id:"):]
    body = body[:body.index("# Load the newest window")]
    assert "INSERT INTO threads" in body, (
        "no thread is created, so a goal whose assistant has no thread "
        "has no way to get one")
    # REACHABLE, not merely present. `if False:` leaves the statement in
    # the file and unreachable, which a text assertion cannot see — so
    # the guard above it is checked explicitly.
    assert "if not existing:" in body, (
        "the creation is behind a condition that is not 'no thread yet'")
    # The same columns the Slack-path creator writes, so it is the row
    # the product would have made.
    for col in ("goal_id", "agent_id", "created_by", "title",
                "correlation_id"):
        assert col in body, f"the created thread has no {col}"


def test_it_prefers_an_existing_thread_over_making_another():
    """A fixed link must open the same conversation every time. Creating
    unconditionally would give "# assistant" a new thread per visit."""
    src = _read("app/routers/chat.py")
    body = src[src.index("if not thread_id:"):]
    body = body[:body.index("# Load the newest window")]
    assert body.index("SELECT id FROM threads") < body.index(
        "INSERT INTO threads")
    assert "if not existing:" in body


# ── F11 · the notation the prompt teaches must be the notation that works ─

def test_an_artifact_check_is_recognised_in_the_shape_plans_use():
    """FOUND LIVE, on the first errand ever to use an artifact goal check.

    `_normalise` applied `parse()` to a check written as a bare STRING and
    not to one written as a DICT — and the dict is the shape the
    plan-approval path uses: `questions.checks` is documented as
    `[{run, label}]`.

    So an executor wrote `artifact[role=report,name=X.md]` exactly as its
    prompt teaches, in the shape the system asked for, and it was adopted
    as `kind='command'`: a shell command that can never pass. The errand
    emitted all three artifacts, could not satisfy its own goal check,
    and replanned — costing a plan cycle and a second human approval.

    The comment two lines above the bug states the rule it broke: "the
    notation the prompt teaches has to be the notation that works — a
    model shown `artifact[role=report]` will type it".
    """
    from kernel.worker.contracts import normalise

    got = normalise([{"run": "artifact[role=report,name=X.md]",
                      "label": "goal"}])[0]
    assert got["kind"] == "artifact", (
        "an artifact check written in the taught notation is adopted as a "
        "shell command, so an errand's goal check can never pass")
    assert got["spec"] == {"role": "report", "name": "X.md"}


def test_the_string_shape_still_works():
    """The branch that was already right."""
    from kernel.worker.contracts import normalise

    got = normalise(["artifact[role=dataset,name=Z.csv]"])[0]
    assert got["kind"] == "artifact"
    assert got["spec"] == {"role": "dataset", "name": "Z.csv"}


def test_an_explicit_expect_still_wins():
    """An explicit declaration beats inference from a string — the
    `expect: artifact` form must keep working and must keep using its own
    fields rather than parsing `run`."""
    from kernel.worker.contracts import normalise

    got = normalise([{"run": "ignored", "expect": "artifact",
                      "role": "report", "name": "Y.md", "label": "goal"}])[0]
    assert got["kind"] == "artifact"
    assert got["spec"] == {"role": "report", "name": "Y.md"}


def test_a_plain_command_is_still_a_command():
    """The fix must not turn every check into an artifact check."""
    from kernel.worker.contracts import normalise

    got = normalise([{"run": "pytest tests", "label": "guard"}])[0]
    assert got["kind"] == "command"
    assert got["spec"] == {}


def test_a_malformed_artifact_check_is_refused_not_shelled_out():
    """`parse` returns None for "not an artifact check" and an EMPTY DICT
    for a badly written one — the distinction exists so a broken artifact
    check is refused rather than executed as a shell command, which is how
    `artifact[]` would otherwise reach a shell."""
    from kernel.worker.contracts import normalise

    got = normalise([{"run": "artifact[]", "label": "goal"}])[0]
    assert got["kind"] == "artifact"
    assert got["spec"] == {}
