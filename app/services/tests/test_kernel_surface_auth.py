"""The cloud transport must refuse an unauthenticated surface too.

api.py will not START without ASSISTANT_API_TOKEN, and the reason it
gives is about deployment: this surface carries start_story, steer,
amend, cancel_story and every GO write, addressed by a goal id in the
path with no other scoping. That check was wired only into the
ThreadingHTTPServer -- which runs on a laptop -- and not into asgi.py,
which is the transport that gets a public Cloud Run URL. The `next`
control panel came up with 17 assistant + 20 go tools and no auth.
"""
import pytest

from kernel.assistant import asgi


def _unset(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.delenv("AGENTOS_ASSISTANT_API_OPEN", raising=False)


def test_router_refuses_to_build_with_no_token(monkeypatch):
    _unset(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        asgi.build_router(lambda: None)
    assert "ASSISTANT_API_TOKEN" in str(e.value)


def test_a_token_lets_it_build(monkeypatch):
    _unset(monkeypatch)
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "t")
    assert asgi.build_router(lambda: None).routes


def test_the_local_opt_out_still_works(monkeypatch):
    # A local stack that means it says so out loud.
    _unset(monkeypatch)
    monkeypatch.setenv("AGENTOS_ASSISTANT_API_OPEN", "1")
    assert asgi.build_router(lambda: None).routes


def test_every_declared_tool_is_still_reachable(monkeypatch):
    # The surface asserts set(ROUTES) == set(TOOLS); this is the other
    # half -- the guard must not drop routes on the way in. Compared
    # against route_table(), which is what BOTH transports derive from,
    # so the count stays right when a tool is added (and covers the
    # `view` rows, which are neither ROUTES nor GO_ROUTES).
    from kernel.assistant.api import route_table
    _unset(monkeypatch)
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "t")
    router = asgi.build_router(lambda: None)
    served = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    for method, path, _who, _tool in route_table():
        assert (f"/internal/kernel{path}", (method,)) in served, path


# ── the borrowed-pool search_path ────────────────────────────────────────
#
# The kernel's tables live in a `kernel` schema and its SQL names them
# unqualified. With no KERNEL_DATABASE_URL the router borrows the
# control panel's pool, which resolves `public` -- so every tool
# answered `UndefinedTableError: relation "assistant_requests" does not
# exist` while the schema sat there with 31 tables in it.

class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, *_a):
        self.executed.append(sql)


def _fallback_for(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def acquire():
        yield conn
    return acquire


@pytest.mark.asyncio
async def test_borrowed_pool_gets_the_kernel_search_path(monkeypatch):
    monkeypatch.delenv("KERNEL_DATABASE_URL", raising=False)
    conn = _FakeConn()
    acquire = asgi.kernel_acquire(_fallback_for(conn))
    async with acquire() as c:
        assert c is conn
        assert c.executed == [f"SET search_path = {asgi.KERNEL_SEARCH_PATH}"]


@pytest.mark.asyncio
async def test_the_path_is_put_back_on_release(monkeypatch):
    # The control panel's pool must not keep `kernel` on its path, or an
    # unqualified `executions` resolves differently depending on which
    # pool a query borrowed -- which reads as flakiness, not an error.
    monkeypatch.delenv("KERNEL_DATABASE_URL", raising=False)
    conn = _FakeConn()
    async with asgi.kernel_acquire(_fallback_for(conn))():
        pass
    assert conn.executed[-1] == "RESET search_path"


@pytest.mark.asyncio
async def test_the_path_is_put_back_even_when_the_tool_raises(monkeypatch):
    monkeypatch.delenv("KERNEL_DATABASE_URL", raising=False)
    conn = _FakeConn()
    with pytest.raises(ValueError):
        async with asgi.kernel_acquire(_fallback_for(conn))():
            raise ValueError("tool blew up")
    assert conn.executed[-1] == "RESET search_path"


@pytest.mark.asyncio
async def test_a_failed_reset_does_not_mask_the_real_error(monkeypatch):
    monkeypatch.delenv("KERNEL_DATABASE_URL", raising=False)

    class _Dying(_FakeConn):
        async def execute(self, sql, *_a):
            await super().execute(sql, *_a)
            if sql.startswith("RESET"):
                raise RuntimeError("connection is gone")

    conn = _Dying()
    with pytest.raises(ValueError):
        async with asgi.kernel_acquire(_fallback_for(conn))():
            raise ValueError("the error that matters")
