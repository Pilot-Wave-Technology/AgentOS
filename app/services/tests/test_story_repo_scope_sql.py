"""`story_repo_scope`'s SQL, exercised against a REAL PostgreSQL.

Companion to `test_local_multirepo_interlock.py`, which mocks the pool and therefore
cannot verify anything that lives in SQL. Three things do:

  * `count_story_repos` counts DISTINCT `owner/name` pairs, case-folded — so one repo
    granted as both a `repo` and a `branch` resource is ONE repo, not two. Get this
    wrong and every ordinary single-repo local checkout is wrongly refused.
  * `granted_repo_scope` is what the credential broker falls back to when a story has
    no recorded workspace layout — which is EVERY story a kernel assistant runs, since
    its workspace clones nothing. Get this wrong and `git clone` 404s in the executor's
    sandbox and the run stalls for ever, retrying something that was never transient.
  * `has_live_local_checkout` compares `checkout_expires_at` to `NOW()` on the DB
    clock — so an abandoned lock stops blocking repo grants once it expires.

**How to run.** Start the local stack (`local-development/scripts/stack.ps1`); these
tests then run against the local Postgres. If no database is reachable they SKIP
rather than fail, so the suite stays green wherever there is no stack.
Override the target with `DATABASE_URL`.

**Why `asyncio.run` and not `@pytest.mark.asyncio`.** `pytest-asyncio` is not
installed here, and an async test without it does not skip — it ERRORS, which is how
all twelve of these sat dead while the gate they belong to read green. The sync
wrapper needs no plugin, and is what `test_stage_k_invariants.py` already uses.

**Safety.** Every test runs inside one transaction that is ALWAYS rolled back, and
fixture ids are namespaced `zz-interlock-test%`. Nothing is left behind in the dev
database. The rollback is asserted, not assumed.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from app.services.story_repo_scope import (
    count_story_repos,
    granted_repo_scope,
    has_live_local_checkout,
)

asyncpg = pytest.importorskip("asyncpg")

# The local-development stack runs Postgres on 5433 (see local-development/README.md).
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-interlock-test"


def _candidate_dsns() -> list[str]:
    """DSNs to try, in priority order.

    `DATABASE_URL` cannot be trusted here: `app/services/model_registry/tests/
    conftest.py` does `os.environ.setdefault("DATABASE_URL", "postgresql://x:x@
    localhost:5432/x")` at import time, so during a full-suite run this module would
    otherwise inherit a dummy DSN and skip itself while passing when run alone.
    Trying candidates in order — explicit override, then whatever `DATABASE_URL`
    holds, then the local-dev stack — is robust without hardcoding the dummy value.
    """
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"),
                LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


_SKIP_REASON = (
    "no local database with the expected schema — start the local stack "
    "(local-development/scripts/stack.ps1) or set DATABASE_URL"
)


async def _connect_or_skip():
    """Connect to the first candidate DSN that has the schema, or skip.

    Resolved at TEST time, not import time, for two reasons that both caused these
    tests to silently skip while passing in isolation: a module-level
    `pytest.mark.skipif` had to call `asyncio.run` during collection, and the
    `DATABASE_URL` it read was by then a dummy set by a sibling conftest. A guard
    that hides itself is worse than no guard, so both are decided here.
    """
    attempts: list[str] = []
    for dsn in _candidate_dsns():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                  # noqa: BLE001 — any failure means "not this one"
            attempts.append(f"{_redact(dsn)}: {type(exc).__name__}")
            continue
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('stories','goals','goal_resources','story_resources')")
        if tables == 4:
            return conn
        await conn.close()
        attempts.append(f"{_redact(dsn)}: only {tables}/4 required tables")
    pytest.skip(f"{_SKIP_REASON}. Tried: {'; '.join(attempts)}")


def _redact(dsn: str) -> str:
    """Host/port/db only — credentials must never reach test output or CI logs."""
    tail = dsn.rsplit("@", 1)[-1]
    return tail or "<malformed dsn>"


@contextlib.asynccontextmanager
async def rolled_back_conn():
    """A connection whose work is always discarded, with the rollback verified."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = sum([
            await conn.fetchval("SELECT count(*) FROM stories WHERE id LIKE $1", f"{PREFIX}%"),
            await conn.fetchval("SELECT count(*) FROM goals WHERE id LIKE $1", f"{PREFIX}%"),
            await conn.fetchval("SELECT count(*) FROM goal_resources WHERE id LIKE $1", f"{PREFIX}%"),
        ])
        await conn.close()
        assert leaked == 0, f"fixture rows survived the rollback ({leaked}) — dev DB dirtied"


async def _story(conn, sid: str):
    """Each story gets its OWN goal: `goal_resources` carries
    UNIQUE(goal_id, kind, name), so two cases granting a repo of the same name would
    collide inside a shared goal."""
    await conn.execute("INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", f"fixture {sid}", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by) VALUES ($1,$2,$3,$4)",
        sid, f"{sid}-goal", "interlock fixture", f"{PREFIX}-user")


async def _grant(conn, sid: str, rid: str, kind: str, owner: str, repo: str,
                 status: str = "active", relation: str = "modifies"):
    """`name` is the display name (unique per goal+kind); `repo_name`/`repo_owner`
    are what the predicate actually reads."""
    await conn.execute(
        """INSERT INTO goal_resources (id, goal_id, kind, name, created_by,
                                       status, repo_owner, repo_name)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        rid, f"{sid}-goal", kind, f"{repo}-{rid[-2:]}", f"{PREFIX}-user",
        status, owner, repo)
    await conn.execute(
        "INSERT INTO story_resources (story_id, resource_id, relation) "
        "VALUES ($1,$2,$3)", sid, rid, relation)


def test_same_repo_as_repo_and_branch_counts_once():
    """The case that matters most: granting a repo AND its branch is the normal
    single-repo shape. Counting rows instead of repos would refuse it."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-a"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-a-r1", "repo", "acme", "svc-a")
            await _grant(conn, sid, f"{PREFIX}-a-r2", "branch", "acme", "svc-a")
            assert await count_story_repos(conn, sid) == 1

    asyncio.run(run())


def test_two_distinct_repos_counted_as_two():
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-b"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-b-r1", "repo", "acme", "svc-a")
            await _grant(conn, sid, f"{PREFIX}-b-r2", "repo", "acme", "svc-b")
            assert await count_story_repos(conn, sid) == 2

    asyncio.run(run())


def test_repo_identity_is_case_insensitive():
    """`Acme/Svc-A` and `acme/svc-a` are one GitHub repository."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-c"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-c-r1", "repo", "ACME", "SVC-A")
            await _grant(conn, sid, f"{PREFIX}-c-r2", "branch", "acme", "svc-a")
            assert await count_story_repos(conn, sid) == 1

    asyncio.run(run())


def test_same_repo_name_under_different_owners_is_two_repos():
    """Owner must be part of the identity, or forks/namespaces collapse together."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-d"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-d-r1", "repo", "acme", "shared")
            await _grant(conn, sid, f"{PREFIX}-d-r2", "branch", "other", "shared")
            assert await count_story_repos(conn, sid) == 2

    asyncio.run(run())


def test_revoked_and_non_repo_grants_are_ignored():
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-e"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-e-r1", "repo", "acme", "svc-a")
            await _grant(conn, sid, f"{PREFIX}-e-r2", "repo", "acme", "svc-b",
                         status="revoked")
            await conn.execute(
                """INSERT INTO goal_resources (id, goal_id, kind, name, created_by, status)
                   VALUES ($1,$2,'doc','spec.md',$3,'active')""",
                f"{PREFIX}-e-r3", f"{sid}-goal", f"{PREFIX}-user")
            await conn.execute(
                "INSERT INTO story_resources (story_id, resource_id, relation) "
                "VALUES ($1,$2,'needs')", sid, f"{PREFIX}-e-r3")
            assert await count_story_repos(conn, sid) == 1

    asyncio.run(run())


def test_story_with_no_repo_grants_counts_zero():
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-g"
            await _story(conn, sid)
            assert await count_story_repos(conn, sid) == 0

    asyncio.run(run())


# ── granted_repo_scope: the credential broker's fallback ────────────────────
#
# The broker used to prove scope ONLY from `story_workspace_nodes`, which
# records the ASSISTANT's clones. A kernel assistant clones nothing there, so
# every code story it ran 404'd at `git clone` and stalled for ever. These pin
# the source it falls back to.


def test_granted_scope_answers_when_no_workspace_layout_exists():
    """The KER15 failure, in one assertion.

    No `story_workspace_nodes` row is created anywhere in this test — that is
    the whole point. A kernel assistant's story never gets one, and the scope
    must still resolve, or the executor cannot authenticate its clone.
    """
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-gs1"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-gs1-r1", "branch", "acme", "svc-a")

            assert await conn.fetchval(
                "SELECT count(*) FROM story_workspace_nodes WHERE story_id=$1", sid) == 0
            scope = await granted_repo_scope(conn, sid)
            assert [(r["repo_owner"], r["repo_name"], r["is_write"]) for r in scope] == \
                [("acme", "svc-a", True)]

    asyncio.run(run())


def test_a_read_only_grant_yields_a_read_only_scope():
    """`needs` is a context clone. It must never resolve to a pushable token."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-gs2"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-gs2-r1", "repo", "acme", "svc-a",
                         relation="needs")
            scope = await granted_repo_scope(conn, sid)
            assert [r["is_write"] for r in scope] == [False]

    asyncio.run(run())


def test_read_and_write_grants_on_one_repo_resolve_to_write():
    """The shape KER15 actually has: GO grants `modifies`, the git engine adds
    `produces` for the branch it cut, and an integration story also holds
    `needs`. First-row-wins would mint a read-only token for a story that must
    push — and it would fail at `git push`, long after anyone was watching."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-gs3"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-gs3-r1", "repo", "acme", "svc-a",
                         relation="needs")
            await _grant(conn, sid, f"{PREFIX}-gs3-r2", "branch", "acme", "svc-a",
                         relation="produces")
            scope = await granted_repo_scope(conn, sid)
            assert len(scope) == 1, scope          # one repository, not two rows
            assert scope[0]["is_write"] is True

    asyncio.run(run())


def test_revoked_grants_do_not_widen_the_scope():
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-gs4"
            await _story(conn, sid)
            await _grant(conn, sid, f"{PREFIX}-gs4-r1", "repo", "acme", "svc-a")
            await _grant(conn, sid, f"{PREFIX}-gs4-r2", "repo", "acme", "svc-b",
                         status="revoked")
            names = [r["repo_name"] for r in await granted_repo_scope(conn, sid)]
            assert names == ["svc-a"]

    asyncio.run(run())


def test_a_story_with_no_grants_still_proves_nothing():
    """The refusal must survive. An empty scope is what makes the broker's 404
    a security property rather than a bug — the fallback widens the SOURCE of
    the proof, never the answer when there is none."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-gs5"
            await _story(conn, sid)
            assert await granted_repo_scope(conn, sid) == []

    asyncio.run(run())


def test_checkout_liveness_follows_expiry_on_the_db_clock():
    """An EXPIRED lock must read as not-live, or an abandoned editor session would
    block repo grants on that story forever."""
    async def run():
        async with rolled_back_conn() as conn:
            sid = f"{PREFIX}-h"
            await _story(conn, sid)

            await conn.execute(
                "UPDATE stories SET checked_out_to_uid=$2, "
                "checkout_expires_at=NOW()+interval '5 min' WHERE id=$1", sid, "uid-1")
            assert await has_live_local_checkout(conn, sid) is True

            await conn.execute(
                "UPDATE stories SET checkout_expires_at=NOW()-interval '5 min' WHERE id=$1", sid)
            assert await has_live_local_checkout(conn, sid) is False

            # No TTL recorded yet, but a holder is set — treat as live.
            await conn.execute("UPDATE stories SET checkout_expires_at=NULL WHERE id=$1", sid)
            assert await has_live_local_checkout(conn, sid) is True

            # Released: no holder means not live, regardless of expiry.
            await conn.execute(
                "UPDATE stories SET checked_out_to_uid=NULL, "
                "checkout_expires_at=NOW()+interval '5 min' WHERE id=$1", sid)
            assert await has_live_local_checkout(conn, sid) is False

    asyncio.run(run())
