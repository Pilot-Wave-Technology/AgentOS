"""Whoever asks for a verdict says which pod holds the tree.

WHAT WAS TRUE BEFORE, AND IT MEANT NO STORY COULD BE VERIFIED AT ALL.

`verify_claim` takes `workspace_host` — "the address of the container
holding the tree, and EMPTY MEANS THIS FILESYSTEM" — and reads it when
`archive_is_remote(driver)`, which is true for the Kubernetes driver and
false for every other. It was added with the pod substrate and one test
caller passed it.

The callers that produce a verdict in production passed NEITHER it nor
anything like it:

    adjudicator.adjudicate_one   the real verdict on a story
    inert.probe                  the second opinion on a failing check
    dryrun.dryrun_one            the preview a person approved against,
                                 retired at E9 (plan 5.6)

So on a pod all of them reached `archive_in_pod("")`, whose own refusal
says "no address for the pod holding this workspace" — raised as
`TreeExportError`, which `verify_claim` turns into `error` for EVERY
criterion, deliberately, because a tree that could not be produced means
nothing was checked. Correct handling of a fact nobody should have
produced.

The result is not a wrong verdict. It is NO verdict, on every story, from
every path that reaches one. And it reads as the Verifier being broken
rather than as two arguments nobody passed.

IT WAS INVISIBLE BECAUSE THE UNIT TESTS PASSED. `verify_claim`'s own
tests hand it a `workspace_host` and prove it uses it — and it does. The
defect lives one level up, in the callers, which no test looked at. A
test that stops at the function boundary cannot see a defect that lives
on the boundary, which is the same shape as the clarification card that
was fixed in `build_card` and thrown away by the browser.

SO THIS TEST ASKS THE CALLERS, NOT THE FUNCTION. It patches
`verify_claim` where each caller resolved it and asserts what that caller
actually passed. Adding a fourth caller that forgets is then a failure
here rather than a story with no verdict.

AND `execution_id` TRAVELS WITH IT, because `podfs` authenticates per
execution (kernel/workspace/podfs_auth.py): the token is derived from the
id, so an address without it is an address that 401s — reported as an
unreachable pod, which sends the reader to the network.
"""
from __future__ import annotations

import pytest

POD_IP = "10.48.129.202"
EID = 193


class _PodDriver:
    """The substrate that makes `workspace_host` load-bearing.

    ALL THREE, as `KubernetesDriver` declares them, because three
    different questions read three different attributes: `is_pod` reads
    WORKSPACE_MODEL, `archive_is_remote` reads ARCHIVE_MODEL, and
    `ensure_reachable` reads SANDBOX_MODEL. A fake that sets one and not
    the others passes through the branch it meant to exercise.
    """

    WORKSPACE_MODEL = "pod"
    ARCHIVE_MODEL = "pod"
    SANDBOX_MODEL = "pod"

    async def resolve_bridge_host(self, cid):
        return POD_IP


class _LocalDriver:
    """Everything else. Absent means local — see `archive_is_remote`."""


# ── the seam: what did the caller actually pass? ─────────────────────

@pytest.fixture()
def captured(monkeypatch):
    """Patch `verify_claim` in each caller's module and record its kwargs."""
    seen = {}

    async def _fake(driver, contract, criteria, repo, commit, **kw):
        seen.update(kw)
        seen["called"] = True
        return []

    monkeypatch.setattr("kernel.verifier.adjudicator.verify_claim",
                        _fake, raising=True)
    # `inert` imports it INSIDE the function, so the module attribute is
    # the one it will resolve.
    monkeypatch.setattr("kernel.verifier.verify.verify_claim", _fake,
                        raising=True)
    return seen


# ── the resolver they share ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pod_substrate_resolves_an_address():
    from kernel.verifier.tree import pod_address

    class _Conn:
        async def fetchval(self, sql, *a):
            # `workspace_host` recorded on the row at dispatch.
            return POD_IP if "workspace_host" in sql else "exec-193"

    assert await pod_address(_Conn(), EID, _PodDriver()) == POD_IP


@pytest.mark.asyncio
async def test_a_local_substrate_resolves_to_empty_and_asks_nothing():
    """Empty is not "unknown", it is "the tree is here" — the convention
    `ExecutionEnvelope.workspace_host` already uses. Docker and E0 must
    not acquire a database round-trip for a directory they can open."""
    from kernel.verifier.tree import pod_address

    class _Conn:
        async def fetchval(self, sql, *a):
            raise AssertionError("a local tree needs no address lookup")

    assert await pod_address(_Conn(), EID, _LocalDriver()) == ""


@pytest.mark.asyncio
async def test_an_unresolvable_pod_refuses_rather_than_returning_empty():
    """Returning "" here would reproduce the whole defect: the caller
    would hand `verify_claim` an empty address and get `error` on every
    criterion with nothing naming the cause. `adjudicate_pass` isolates
    and logs one execution's failure already."""
    from kernel.verifier.tree import pod_address
    from kernel.workspace.podfs_client import WorkspaceUnreachable

    class _Conn:
        async def fetchval(self, sql, *a):
            return None                      # no host, no container

    with pytest.raises(WorkspaceUnreachable):
        await pod_address(_Conn(), EID, _PodDriver())


# ── and each caller passes it ────────────────────────────────────────

@pytest.mark.parametrize("rel", ["kernel/verifier/adjudicator.py",
                                 "kernel/verifier/inert.py"])
def test_every_caller_passes_both(rel):
    """AST, NOT BEHAVIOUR, and deliberately — with the reason stated.

    Driving `adjudicate_one` as far as `verify_claim` needs a dozen
    database fakes (mounts, tree_state, deltas, contract, criteria,
    record), and a test held together by that much scaffolding
    breaks on changes that are not this one and gets deleted. The
    property here is small and exact — THE CALL PASSES THESE TWO
    KEYWORDS — and an AST walk states it without the scaffolding.

    Not a grep: a grep cannot tell a call from a docstring, and both of
    these files discuss `verify_claim` in prose. This resolves the call
    node and reads its actual keywords.

    `inert.probe` is ALSO covered behaviourally below. That one is
    drivable without fakes, so it gets the stronger test, and this one
    keeps every caller stated in one place.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None))
             == "verify_claim"]
    assert calls, f"{rel} no longer calls verify_claim — retire this case"

    for call in calls:
        kw = {k.arg for k in call.keywords}
        missing = {"workspace_host", "execution_id"} - kw
        assert not missing, (
            f"{rel}:{call.lineno} calls verify_claim without {missing}. "
            f"On a pod substrate that reaches archive_in_pod(\"\"), which "
            f"refuses, which is `error` for every criterion — no verdict "
            f"on any story, reported as the Verifier being broken.")


@pytest.mark.asyncio
async def test_the_inert_probe_says_where_the_tree_is(captured):
    """It archives the EMPTY TREE, which looks like it needs no pod — but
    the branch that decides where to archive FROM is the driver, not the
    commit, so it crosses anyway and refused without an address."""
    from kernel.verifier import inert
    from kernel.verifier.outcome import Outcome

    failing = Outcome(passes="fail")
    await inert.probe(_PodDriver(), {}, [{"kind": "command", "id": "c1"}],
                      [failing], "/workspace",
                      workspace_host=POD_IP, execution_id=EID)

    assert captured.get("called"), "verify_claim was never reached"
    assert captured["workspace_host"] == POD_IP
    assert captured["execution_id"] == EID


# ── helpers ──────────────────────────────────────────────────────────

def _always(value):
    async def _fn(conn, execution_id, driver=None):
        return value
    return _fn
