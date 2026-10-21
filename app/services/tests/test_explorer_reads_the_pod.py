"""The file explorer reads the pod that holds the workspace.

WHY THE PANEL WAS EMPTY AND NOTHING SAID SO. `kernel_workspace.py`
fetches the tree over HTTP from `KERNEL_WORKSPACE_URL`, documented as
"the container that holds the workspace volume". On Docker that is one
container holding one volume for every story. On Kubernetes each
execution has its own pod, its own ReadWriteOncePod claim, and its own
address -- there is no such container to name.

The variable is also read in one place and written in one:
`local-development/env/local.values`. Nothing sets it on `next`. So
`enabled()` returned False, the endpoint fell through to the gateway
RPC that walks the ASSISTANT's directory, and the assistant holds no
files under the kernel. No error, no log line, an empty panel -- which
reads as "the workspace did not build" while the workspace was
complete, committed and 20 files deep.

Same defect as `GIT_CREDENTIAL_URL`, in a different file, found the
same way: ask who WRITES the variable, not who reads it.

VERIFIED AGAINST A LIVE POD before this was written, which is why the
shape below is asserted rather than guessed:

    host_for('f8b7487d') -> 10.48.129.202
    tree: 20 entries, WORKSPACE.md 570, summary_report.md 7271
    read_file: 7271 bytes
"""
from __future__ import annotations

import pytest

from app.services import kernel_workspace_pod as kwp


class _Conn:
    # `eid` beside `host`: `host_for` returns both, because podfs
    # authenticates per execution and the address alone opens nothing.
    def __init__(self, host, eid=41):
        self.host, self.eid, self.sql = host, eid, ""

    async def fetchrow(self, sql, *args):
        self.sql = sql
        # No pod is NO ROW, not a row with an empty address -- that is
        # what the query's `coalesce(workspace_host,'') <> ''` means.
        if not self.host:
            return None
        return {"id": self.eid, "workspace_host": self.host}


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()


@pytest.fixture()
def wired(monkeypatch):
    def _use(host, explore=None):
        conn = _Conn(host)
        monkeypatch.setattr("app.database.get_pool", lambda: _Pool(conn),
                            raising=False)
        import sys
        import types
        mod = types.ModuleType("app.database")
        mod.get_pool = lambda: _Pool(conn)
        monkeypatch.setitem(sys.modules, "app.database", mod)
        if explore is not None:
            # THE ATTRIBUTE, NOT sys.modules. `_call` does
            # `from kernel.workspace import podfs_client`, which reads
            # the attribute off the already-imported package -- so a
            # sys.modules entry is ignored the moment any other test in
            # the run has imported the real thing. Passed alone and
            # failed in the suite, which is the tell.
            monkeypatch.setattr(
                "kernel.workspace.podfs_client.explore", explore)
        return conn
    return _use


# ── which pod, and whether there is one ──────────────────────────────

@pytest.mark.asyncio
async def test_no_pod_means_fall_through_not_an_empty_panel(wired):
    """None, not {}. Docker and E0 still serve this over HTTP, and
    returning an empty tree here would replace a working path with a
    blank one."""
    wired("")
    assert await kwp.tree("s1") is None
    assert await kwp.read_file("s1", "x.md") is None


@pytest.mark.asyncio
async def test_it_asks_for_the_newest_execution_that_still_has_a_pod(wired):
    conn = wired("10.0.0.5")
    assert await kwp.host_for("s1") == ("10.0.0.5", 41)
    # A story is attempted more than once; the live attempt is the one
    # worth showing, and a destroyed pod has no address to call.
    assert "ORDER BY id DESC" in conn.sql
    assert "container_id IS NOT NULL" in conn.sql


# ── the shape the page renders ───────────────────────────────────────

@pytest.mark.asyncio
async def test_the_tree_comes_back_in_the_pages_shape(wired):
    def explore(host, what, root, *, execution_id, **args):
        assert (host, what, root) == ("10.0.0.5", "tree", "/workspace")
        # THE ID THE ROW CARRIED, not None: podfs derives this
        # execution's token from it and a 401 reads as an unreachable
        # pod, which is the one thing this panel must not misreport.
        assert execution_id == 41
        return {"at": "", "writable": True,
                "entries": [{"path": "WORKSPACE.md", "size": 570},
                            {"path": "repos/acme__api/main.py", "size": 20,
                             "status": "M"}]}
    wired("10.0.0.5", explore)
    out = await kwp.tree("s1")
    assert [e["path"] for e in out["entries"]] == [
        "WORKSPACE.md", "repos/acme__api/main.py"]
    # ALREADY FLATTENED by the pod's explorer, so the mount prefix is
    # part of the path and nothing here stitches one on -- doing so gave
    # `docs/NAME.md/NAME.md` the last time somebody joined these.
    assert out["changed"] == 1
    assert out["entries"][0]["type"] == "file"


@pytest.mark.asyncio
async def test_a_file_is_read_by_its_flattened_path(wired):
    def explore(host, what, root, *, execution_id, **args):
        assert what == "read" and args["rel"] == "summary_report.md"
        assert execution_id == 41
        return {"path": "summary_report.md", "size": 7271,
                "text": "# Technical Summary Report"}
    wired("10.0.0.5", explore)
    out = await kwp.read_file("s1", "summary_report.md")
    assert out["size"] == 7271
    assert out["content"].startswith("# Technical Summary")


# ── and it says so when the pod will not answer ──────────────────────

@pytest.mark.asyncio
async def test_an_unreachable_pod_is_reported_not_rendered_as_empty(wired):
    """The whole point. An empty tree and a pod that did not answer look
    identical on the page, and only one of them is about the work."""
    def explore(*a, **k):
        raise OSError("connection refused")
    wired("10.0.0.5", explore)
    out = await kwp.tree("s1")
    assert out["entries"] == []
    assert "did not answer" in out["detail"]


@pytest.mark.asyncio
async def test_the_read_only_port_is_the_one_used():
    """9101 is the worker's workspace surface; 9100 is the sandbox's
    bridge, which runs arbitrary bash. A viewer uses the viewer's port."""
    from kernel.workspace.podfs import WORKER_PORT
    assert WORKER_PORT == 9101


def test_the_transport_does_not_block_the_event_loop():
    """`podfs_client` is urllib. The module this supplements carries a
    paragraph about a synchronous fetch on the loop turning into the
    server waiting on its own socket; this must not repeat it."""
    import inspect
    assert "to_thread" in inspect.getsource(kwp._call)
