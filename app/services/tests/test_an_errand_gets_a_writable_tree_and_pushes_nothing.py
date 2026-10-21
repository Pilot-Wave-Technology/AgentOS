"""An errand can write; what it writes never reaches the forge.

WHAT WAS MEASURED, on goal ERR17, 2026-10-07. Errands 409 and 410 held
their only repo read-only. `writable_scope_for` returns [] when mounts
exist and none is writable -- "an empty scope is how the envelope says
writes are refused" -- so the `write` tool refused everywhere. But an
errand's deliverable is an artifact and `emit_artifact` takes a PATH, a
file that must already exist. The model therefore had to find some
other way to put bytes on disk, found that `bash` is not path-scoped
the way the write tool is, and wrote a 38 KB report through
`python3 -c '<markdown>'` -- which `harness/destructive.py` reads as
shell and gates. 409: eight permission parks, 115 model calls, no
artifact, cancelled. 410: three parks, 123 calls, one artifact after
27 minutes and four attempts at the same report.

The same errands on goal GOAL1 hold their repo `modifies` and have all
along: 2 to 9 artifacts each, zero or one park, 37-47 calls. They use
the checkout as scratch, so the `write` tool works and `bash` is never
needed.

SO AN ERRAND IS GRANTED A WRITABLE REPO, AND ITS PUSH IS TURNED OFF.
The first half is what makes it able to work; the second is what keeps
a scratch tree out of the forge. Neither is safe without the other,
which is why one file tests both.
"""

from __future__ import annotations

import pytest

from kernel.go import errand_resources as er
from kernel.workspace.mounts import Mount, writable_scope_for


class _Conn:
    """Enough of the catalogue for `_one`, plus the execution's kind."""

    def __init__(self, kind: str = "repo", status: str = "active",
                 exec_kind: str = "errand"):
        self._kind, self._status, self._exec_kind = kind, status, exec_kind

    async def fetchrow(self, sql, *args):
        if "goal_resources" in sql:
            return {"id": "r1", "kind": self._kind, "name": "acme/app",
                    "status": self._status}
        return None

    async def fetchval(self, sql, *args):
        if "workspace_path" in sql:
            return "/workspace/exec-1"
        if "workspace_source" in sql:
            return None
        if "SELECT kind FROM executions" in sql:
            return self._exec_kind
        return None

    async def fetch(self, sql, *args):
        return []


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    """`resolve_resource_id` is the catalogue lookup, not what is tested."""
    async def _ok(conn, goal_id, rid):
        return rid
    monkeypatch.setattr(er, "resolve_resource_id", _ok)


# ── half one: the errand can write ───────────────────────────────────

@pytest.mark.asyncio
async def test_a_repo_asked_for_read_only_comes_back_writable():
    got = await er.validate(_Conn(kind="repo"), "G",
                            [{"resource_id": "r1", "relation": "needs"}])
    assert got[0]["relation"] == "modifies", (
        "an errand with no writable mount has an empty writable scope "
        "and cannot produce the file emit_artifact needs")


@pytest.mark.asyncio
async def test_a_branch_grant_is_upgraded_too():
    got = await er.validate(_Conn(kind="branch"), "G",
                            [{"resource_id": "r1", "relation": "needs"}])
    assert got[0]["relation"] == "modifies"


@pytest.mark.asyncio
async def test_the_upgrade_says_so_rather_than_happening_quietly():
    """The GO proposes `needs` and the story holds `modifies`. Without a
    reason on the grant an audit sees two answers and nothing joining
    them."""
    got = await er.validate(_Conn(kind="repo"), "G",
                            [{"resource_id": "r1", "relation": "needs"}])
    assert "writable" in got[0]["why"] and "pushed" in got[0]["why"]


@pytest.mark.asyncio
async def test_a_reason_the_caller_gave_is_kept():
    got = await er.validate(
        _Conn(kind="repo"), "G",
        [{"resource_id": "r1", "relation": "needs", "why": "read the API"}])
    assert got[0]["why"].startswith("read the API")


# ── and what the upgrade must NOT touch ──────────────────────────────

@pytest.mark.asyncio
async def test_cloud_reach_stays_read_only():
    """`a run ACTS AS an identity, it never edits one`. Widening that is
    an IAM change a person makes outside this system, and this upgrade
    must not become a way around the refusal above it."""
    got = await er.validate(_Conn(kind="gcp_service_account"), "G",
                            [{"resource_id": "r1", "relation": "needs"}])
    assert got[0]["relation"] == "needs"


@pytest.mark.asyncio
async def test_a_document_is_not_upgraded():
    """A doc is copied in; there is nothing to write to."""
    got = await er.validate(_Conn(kind="document"), "G",
                            [{"resource_id": "r1", "relation": "needs"}])
    assert got[0]["relation"] == "needs"


@pytest.mark.asyncio
async def test_an_explicit_write_without_a_reason_is_still_refused():
    """The upgrade runs AFTER that refusal, so it cannot smuggle an
    unexplained write grant past it."""
    with pytest.raises(er.BadResources) as exc:
        await er.validate(_Conn(kind="repo"), "G",
                          [{"resource_id": "r1", "relation": "modifies"}])
    assert exc.value.code == "E_NO_REASON"


# ── half two: nothing it writes is pushed ────────────────────────────

def test_a_writable_mount_gives_the_errand_a_scope_to_write_in():
    """The point of the upgrade, stated as the property it buys."""
    ro = Mount(at="repos/acme__app", binding="git", writable=False)
    rw = Mount(at="repos/acme__app", binding="git", writable=True)
    assert writable_scope_for("/workspace", [ro]) == [], (
        "this is the state 409 and 410 were in"
    )
    assert writable_scope_for("/workspace", [rw]), (
        "a writable mount must yield somewhere the write tool accepts")


@pytest.mark.asyncio
async def test_an_errand_pushes_nothing(monkeypatch):
    from kernel.workspace import deliver

    reached = []
    monkeypatch.setattr(deliver, "_pod_host",
                        lambda *a, **k: reached.append("host"))

    async def _mounts(conn, eid):
        return (Mount(at="repos/acme__app", binding="git", writable=True),)
    monkeypatch.setattr(deliver, "mounts_for", _mounts)

    conn = _Conn(exec_kind="errand")
    ok, detail = await deliver.publish_execution(conn, 1)

    assert ok is True, "an errand that pushed nothing has still delivered"
    assert "artifact" in detail or "scratch" in detail
    assert not reached, (
        "an errand paid for the lane guard's round-trip to the pod")


def test_the_lane_refusal_is_no_longer_kind_dependent():
    """An errand returns before D1, so the branch that asked which kind
    this was had one answer left. A `kind != errand` test still sitting
    inside D1 would be dead code claiming to handle a case that cannot
    reach it."""
    import inspect

    src = inspect.getsource(
        __import__("kernel.workspace.deliver", fromlist=["x"])
        .publish_execution)
    head, _, tail = src.partition("if on_lane:")
    assert 'kind == "errand"' in head, "the early return is gone"
    assert "errand" not in tail or "no longer reaches" in tail, (
        "D1 still branches on kind for a case that cannot reach it")
