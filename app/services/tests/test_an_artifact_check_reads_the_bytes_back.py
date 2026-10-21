"""An `artifact` check passes on a deliverable, not on a row about one.

WHAT IT USED TO DO. `artifact_kind.evaluate` answered entirely from
`kernel.artifact_manifest`: a matching row was a pass. A row says
something was EMITTED. It says nothing about whether it can be READ.

Between 2026-09-18T17:26 and 2026-09-20 that difference was the whole
story. The leg runs in the execution pod, the pod's artifact store was
its own PVC, and every put succeeded and died with the pod. Five
artifacts across four stories were catalogued as durable,
content-addressed and mountable; three stories reported "satisfied by 1
artifact(s)" for objects nobody could open. Every check was green. It
surfaced two days later when a later story was granted one and its
workspace build failed.

WHAT THIS PINS:

  * a matched row whose bytes are gone FAILS, and says which
  * a store that cannot be reached ERRORS -- never a pass, never a fail
  * the pod asks the control panel, because its own disk is the wrong
    place to look now that emit stores through the panel
  * E0, with no broker, still answers from its own store
"""
from __future__ import annotations

import pytest

from kernel.verifier import artifact_kind, artifact_reachable


class _Conn:
    """Just enough asyncpg to answer the one query `evaluate` makes."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args):
        return self._rows


def _row(artifact_id="a" * 64, role="report", name="report.md", size=120):
    return {"role": role, "name": name, "bytes": size,
            "artifact_id": artifact_id, "summary": ""}


CRITERION = {"id": "c1", "kind": "artifact", "spec": {"role": "report"}}


@pytest.mark.asyncio
async def test_a_row_whose_bytes_are_gone_is_not_a_pass(monkeypatch):
    """The exact shape of the two-day outage, as a test."""
    async def all_gone(ids, execution_id):
        return list(ids)

    monkeypatch.setattr(artifact_reachable, "missing", all_gone)
    out = await artifact_kind.evaluate(_Conn([_row()]), 42, CRITERION)

    assert out.passes == "fail", out.detail
    assert "cannot produce" in out.detail["reason"]
    # It must name the artifact, not just the count: the executor has to
    # know its emit did not survive, which is a different thing to do
    # next than emitting a differently named file.
    assert "report:report.md" in out.detail["reason"]


@pytest.mark.asyncio
async def test_an_unreachable_store_is_an_error_not_a_failure(monkeypatch):
    """Refusing delivered work over an outage is the worse bug.

    `artifact_kind`'s own docstring reserves `error` for a check that
    could not say anything, and a store nobody can reach is exactly
    that.
    """
    async def cannot_say(ids, execution_id):
        raise artifact_reachable.Unanswerable("the panel is down")

    monkeypatch.setattr(artifact_reachable, "missing", cannot_say)
    out = await artifact_kind.evaluate(_Conn([_row()]), 42, CRITERION)

    assert out.passes not in ("pass", "fail"), (
        f"an unreachable store produced {out.passes!r}; a pass would "
        f"restore the original bug and a fail would refuse real work")
    assert "the panel is down" in out.detail["reason"]


@pytest.mark.asyncio
async def test_the_readable_ones_still_satisfy_the_check(monkeypatch):
    """One lost artifact does not condemn a criterion another satisfies."""
    good, bad = "b" * 64, "c" * 64

    async def one_gone(ids, execution_id):
        return [bad]

    monkeypatch.setattr(artifact_reachable, "missing", one_gone)
    rows = [_row(artifact_id=bad, name="lost.md"),
            _row(artifact_id=good, name="kept.md")]
    out = await artifact_kind.evaluate(_Conn(rows), 42, CRITERION)

    assert out.passes == "pass"
    assert [a["id"] for a in out.detail["artifacts"]] == [good]


@pytest.mark.asyncio
async def test_a_pod_does_not_answer_this_from_its_own_disk(monkeypatch):
    """The trap that would have failed every story on the cluster.

    The check runs on the WORKER, whose binding is filesystem. Since
    emit stores through the panel, the bytes are NOT on that disk -- a
    local `exists()` there answers False for every correctly stored
    artifact. Refusing to guess is the only safe answer.
    """
    from kernel.workspace import podfs_auth

    monkeypatch.delenv("CONTROL_PANEL_URL", raising=False)
    monkeypatch.delenv(podfs_auth.ENV_VAR, raising=False)
    monkeypatch.setenv("AGENTOS_EXECUTION_ID", "42")
    monkeypatch.setattr("kernel.artifacts.store.BINDING", "filesystem")

    with pytest.raises(artifact_reachable.Unanswerable) as caught:
        await artifact_reachable.missing(["d" * 64], 42)
    assert "pod's own disk" in str(caught.value)


@pytest.mark.asyncio
async def test_e0_answers_from_its_own_store(monkeypatch):
    """No broker and no pod marker is E0, where local IS durable."""
    from kernel.workspace import podfs_auth

    asked = []

    async def fake_exists(artifact_id):
        asked.append(artifact_id)
        return artifact_id.startswith("e")

    monkeypatch.delenv("CONTROL_PANEL_URL", raising=False)
    monkeypatch.delenv(podfs_auth.ENV_VAR, raising=False)
    monkeypatch.delenv("AGENTOS_EXECUTION_ID", raising=False)
    monkeypatch.setattr("kernel.artifacts.aio.aexists", fake_exists)

    gone = await artifact_reachable.missing(["e" * 64, "f" * 64], 7)
    assert gone == ["f" * 64]
    assert len(asked) == 2


def test_the_endpoint_refuses_rather_than_reporting_nothing_missing():
    """A 502 must not read as "everything is present".

    That is the precise shape of the bug being closed: a question nobody
    could answer, recorded as a satisfied one.
    """
    import inspect

    from app.routers import artifact_exists

    src = inspect.getsource(artifact_exists.artifact_exists)
    raise_at = src.index("status_code=502")
    return_at = src.index('return {"missing"')
    assert raise_at < return_at, (
        "the store error path falls through to a successful answer")


def test_the_probe_does_not_read_a_missing_field_as_success():
    """An answer of the wrong shape is not an answer."""
    import inspect

    src = inspect.getsource(artifact_reachable._ask_panel)
    assert '"missing" not in answer' in src, (
        "a response with no `missing` key would be read as an empty "
        "list, which is the original bug wearing a different hat")
