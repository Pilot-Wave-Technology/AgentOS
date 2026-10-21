"""A database that is a second late is not a reason to exit 1.

WHAT HAPPENED ON EVERY DEPLOY. Cloud SQL reaches the conductor through
`cloud-sql-proxy`, a NATIVE SIDECAR -- an initContainer with
`restart: Always` -- so Kubernetes starts the conductor once the sidecar
has STARTED. Started is not listening. The gap is under a second, and it
was enough:

    ConnectionRefusedError: [Errno 111] Connect call failed
    ('127.0.0.1', 5432)

The process exited 1 and Kubernetes restarted it. It recovered every
time, which is why this looked free. It was not:

  every pod of every deploy carried RESTARTS=1;

  and `smoke`, which reads pod readiness immediately after the rollout,
  caught a pod mid-restart and FAILED A DEPLOY whose cluster was fine --

      ready flags: true
      false
      a conductor pod is not ready

  That is build 08728e37. Both conductors were serving correctly by the
  time anyone looked, which is the worst shape of flake: it fails, then
  disproves itself.

WHY RETRY IN THE PROCESS RATHER THAN ADD A startupProbe TO THE SIDECAR.
A probe covers the first second and nothing else. The proxy can restart
at any point in a pod's life, and a process that waits for its database
is correct at every one of those moments rather than just the first.

AND IT IS A WAIT, NOT A LOOP AROUND THE SERVICE. Past the budget it
raises: a database still absent after a minute is a deployment fault,
and a pod that stays up pretending otherwise is worse than one that
fails the rollout.
"""
from __future__ import annotations

import asyncio

import asyncpg
import pytest

# PATCHED WHERE IT IS READ. `wait_for_pool` left `service.py`
# for `service_pool.py` (D2a), so `make_pool` is a name in THAT
# module now -- patching the facade would have bound nothing and
# this file would have gone out to a real database.
from kernel.worker import service
from kernel.worker import service_pool as pool_mod


@pytest.mark.asyncio
async def test_it_waits_out_a_proxy_that_is_not_listening_yet(monkeypatch):
    """THE CASE. Refused twice, then accepted -- the real sequence."""
    calls = {"n": 0}

    async def _flaky(dsn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError(
                111, "Connect call failed ('127.0.0.1', 5432)")
        return "the pool"

    monkeypatch.setattr(pool_mod, "make_pool", _flaky)
    monkeypatch.setattr(service, "DB_RETRY_S", 0.01)

    assert await service.wait_for_pool("dsn", budget=5) == "the pool"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_a_database_that_is_really_gone_still_fails(monkeypatch):
    """The pod must not come up healthy against nothing. A deployment
    fault has to fail the rollout, not hide behind a retry."""
    async def _never(dsn):
        raise ConnectionRefusedError(111, "nope")

    monkeypatch.setattr(pool_mod, "make_pool", _never)
    monkeypatch.setattr(service, "DB_RETRY_S", 0.01)

    with pytest.raises(ConnectionRefusedError):
        await service.wait_for_pool("dsn", budget=0.05)


@pytest.mark.asyncio
async def test_a_healthy_database_is_not_delayed(monkeypatch):
    """The ordinary start must cost one call and no sleep."""
    calls = {"n": 0}

    async def _fine(dsn):
        calls["n"] += 1
        return "the pool"

    monkeypatch.setattr(pool_mod, "make_pool", _fine)
    started = asyncio.get_running_loop().time()
    assert await service.wait_for_pool("dsn", budget=60) == "the pool"
    assert calls["n"] == 1
    assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.asyncio
async def test_a_postgres_error_is_also_waited_out(monkeypatch):
    """"the database system is starting up" is a PostgresError, not an
    OSError, and it is the other half of the same second."""
    calls = {"n": 0}

    async def _starting(dsn):
        calls["n"] += 1
        if calls["n"] < 2:
            raise asyncpg.CannotConnectNowError(
                "the database system is starting up")
        return "the pool"

    monkeypatch.setattr(pool_mod, "make_pool", _starting)
    monkeypatch.setattr(service, "DB_RETRY_S", 0.01)
    assert await service.wait_for_pool("dsn", budget=5) == "the pool"


@pytest.mark.asyncio
async def test_a_bad_dsn_is_not_retried_for_a_minute(monkeypatch):
    """A programming error must surface at once. Only connection
    failures are worth waiting on; a TypeError is not the proxy being
    slow."""
    async def _broken(dsn):
        raise TypeError("dsn must be a string")

    monkeypatch.setattr(pool_mod, "make_pool", _broken)
    with pytest.raises(TypeError):
        await service.wait_for_pool("dsn", budget=60)


def test_main_uses_the_wait(monkeypatch):
    """The guard: `make_pool` called directly from `main` is the old
    crash-and-restart behaviour back again."""
    import inspect

    src = inspect.getsource(service.main)
    assert "wait_for_pool(" in src
    assert "await make_pool(" not in src
