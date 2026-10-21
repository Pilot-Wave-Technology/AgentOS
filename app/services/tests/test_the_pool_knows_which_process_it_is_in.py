"""D2-prep: one `make_pool`, two processes, and it must tell them apart.

WHAT THIS IS GUARDING, from plan §10a and §11.5a prerequisites 1 and 2.
Phase D moves the leg into its own pod, which connects through a Cloud
SQL proxy as the narrow role `agentos_leg`. Two constants in
`loopbridge` were written when the conductor was the only caller:

  * `SET ROLE agentos_worker` on EVERY pooled connection. `agentos_leg`
    was deliberately never granted membership — granting it would have
    silenced the boundary along with the error — so the pod's very first
    failure would have been `permission denied to set role`, a confusing
    error about the wrong thing, instead of `permission denied for
    table`, which is the boundary working.
  * `POOL_MIN, POOL_MAX = 2, 16`, against `CONNECTION LIMIT 80` on
    `agentos_leg`. Four pods at 16 exhaust the role and the fifth gets a
    connection error that reads as a database outage.

Neither failure is visible from the conductor, which is why they are
asserted here and not left to the first pod.

THE TWO MECHANISMS ARE DIFFERENT ON PURPOSE and the tests below hold
each to its own standard: the role is decided by asking the DATABASE
(the wrong answer is a privilege hole, and a name comparison would
already be wrong — the deploy script spells the pod's user
`agentos_leg_${ENV_NAME}` while the migration creates `agentos_leg`),
and the size is decided by the ENVIRONMENT because `max_size` is fixed
before any connection exists to ask.
"""

from __future__ import annotations

import asyncio

import pytest

from kernel import loopbridge
from kernel.drivers import k8s_sqlproxy


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeConn:
    """Records what `init` did, and answers the membership check."""

    def __init__(self, holds_role: bool):
        self._holds = holds_role
        self.asked: list[tuple] = []
        self.executed: list[str] = []

    async def fetchval(self, sql, *args):
        self.asked.append((sql, args))
        return self._holds

    async def execute(self, sql, *args):
        self.executed.append(sql)


@pytest.fixture
def conductor(monkeypatch):
    """No pod database in the environment: this is the conductor."""
    monkeypatch.delenv(loopbridge.LEG_DB_ENV, raising=False)


@pytest.fixture
def execution_pod(monkeypatch):
    """The variable Kubernetes puts in the WORKER container, and nowhere
    else — see `k8s_sqlproxy.worker_env`."""
    monkeypatch.setenv(loopbridge.LEG_DB_ENV, "agentos_leg_next")


# ── the role ─────────────────────────────────────────────────────────────

def test_a_conductor_connection_still_adopts_the_worker_role(conductor):
    """F1 is unchanged where it was already working.

    `agentos` is a superuser, so `pg_has_role` says yes and the SET ROLE
    happens exactly as it did before D2-prep. If this ever goes red the
    conductor is authorising grants as the superuser again, which is the
    bug `test_errand_grant_privilege_sql.py` is built on top of."""
    conn = _FakeConn(holds_role=True)

    _run(loopbridge._as_worker(conn))

    assert conn.executed == [f"SET ROLE {loopbridge.WORKER_ROLE}"]


def test_a_leg_connection_does_not_try_to_adopt_a_role_it_lacks(
        execution_pod):
    """The pod is `agentos_leg` and is not a member of `agentos_worker`.

    Asserting on `executed` being EMPTY rather than on the absence of one
    string: a future `SET ROLE agentos_leg` would be just as wrong — the
    connection already is that role — and would slip past a narrower
    assertion."""
    conn = _FakeConn(holds_role=False)

    _run(loopbridge._as_worker(conn))

    assert conn.executed == [], (
        "the pod ran SET ROLE and its first failure will be "
        "'permission denied to set role', not the boundary")


def test_the_question_is_put_to_the_database_and_not_to_a_config():
    """The role name is a parameter of a query, not a string compared to
    a DSN. §10a's rename risk is real: the deploy Secret already carries
    `agentos_leg_${ENV_NAME}` while the migration creates `agentos_leg`,
    so anything matching on the username is born broken."""
    conn = _FakeConn(holds_role=True)

    _run(loopbridge._as_worker(conn))

    assert len(conn.asked) == 1, "the pool asked more than once per connection"
    sql, args = conn.asked[0]
    assert args == (loopbridge.WORKER_ROLE,)
    assert "pg_has_role" in sql
    assert "EXISTS" in sql and "pg_roles" in sql, (
        "a bare pg_has_role RAISES when the role does not exist, which "
        "would make the pool unbuildable on a database the seed has not "
        "reached")


# ── the size ─────────────────────────────────────────────────────────────

def test_the_conductor_keeps_the_sizes_every_other_comment_cites(
        conductor):
    """`worker/concurrency.py`, `verifier/adjudicator.py`,
    `answerer/deliver.py` and `worker/service.py` all reason out loud
    about "POOL_MAX is 16". This is what keeps them true."""
    assert loopbridge.pool_sizes() == (2, 16)
    assert (loopbridge.POOL_MIN, loopbridge.POOL_MAX) == (2, 16)


def test_the_pod_sizes_its_pool_for_one_leg(execution_pod):
    """§10a: `CONNECTION LIMIT 80` is 20 pods x 4, and D2 must size the
    pod at 1-2. The ceiling assertion is the one that matters — it is
    what a later change raising this has to argue with."""
    assert loopbridge.pool_sizes() == (1, 2)
    assert 20 * loopbridge.LEG_POOL_MAX <= 80, (
        "20 concurrent pods at this size exhaust agentos_leg's "
        "CONNECTION LIMIT, and the failure reads as a database outage")


def test_the_two_processes_do_not_get_the_same_pool(monkeypatch):
    """Both halves of the prerequisite in one assertion, because the
    regression that matters is 'somebody made it unconditional again'."""
    monkeypatch.delenv(loopbridge.LEG_DB_ENV, raising=False)
    conductor_sizes = loopbridge.pool_sizes()
    monkeypatch.setenv(loopbridge.LEG_DB_ENV, "agentos_leg_next")
    pod_sizes = loopbridge.pool_sizes()

    assert conductor_sizes != pod_sizes
    assert pod_sizes < conductor_sizes


def test_make_pool_hands_the_sizes_to_asyncpg(execution_pod, monkeypatch):
    """Computed is not the same as applied. There is still exactly ONE
    `create_pool` call under `kernel/` and no caller passes it a number,
    so this is where the two numbers meet the driver."""
    seen = {}

    async def _create_pool(dsn, **kw):
        seen.update(kw)
        seen["dsn"] = dsn
        return "pool"

    monkeypatch.setattr(loopbridge.asyncpg, "create_pool", _create_pool)

    assert _run(loopbridge.make_pool("postgresql://unused")) == "pool"
    assert (seen["min_size"], seen["max_size"]) == (1, 2)
    assert seen["init"] is loopbridge._as_worker
    assert seen["server_settings"] == {"search_path": "kernel,public"}


# ── the one fact spelled in two files ────────────────────────────────────

def test_the_signal_is_the_variable_the_pod_actually_gets():
    """`loopbridge` cannot import `k8s_sqlproxy` — that module pulls in
    `kernel.spec`, and a pool must not need a pod manifest to import — so
    the name is spelled twice. This is the test that keeps the copies
    equal, which is cheaper than the import and louder than a comment."""
    written_by_the_deployment = {var for var, _ in k8s_sqlproxy.DB_KEYS}

    assert loopbridge.LEG_DB_ENV in written_by_the_deployment
