"""The second key, and where it looks.

WHAT WAS WRONG, and it was invisible because everything stayed green.

`assess` and `investigate` both took `tree_tar: bytes`. The ONLY thing
that sets `report["_tree_tar"]` is `stage.py:308`, and `verify_claim`
skips `stage` entirely on the warm path:

    staged, early = (None, None) if warm else await stage(...)

So the moment E19's volume path started working, both agents received
nothing and returned at their own first line:

    assess       if not (available() and tree_tar): return None
    investigate  if not (available() and tree_tar): return None

Mechanical checks pass, verdict written, story DONE -- and the SECOND
KEY absent. `assess.py` exists because "the mechanical verdict is only
as good as the checks, and the checks are written by the same model that
did the work". Losing it silently makes the faster path the weaker one,
in the one dimension nothing mechanical can see.

AND THE INVESTIGATOR IS THE OTHER HALF. A refusal without an
explanation is the half of a verdict a person cannot act on.

THE FIX IS NOT "FETCH THE TAR ANYWAY". That would rebuild the
`io.BytesIO` wall plan 5.5 E5 names as what breaks large codebases, on
the substrate that runs the big ones. The tree became an OBJECT with two
backends: the warm pod on Kubernetes, an archive on Docker and E0.

AND THE POD IS BETTER, not merely available. An ephemeral container gets
the tree and nothing else, so the investigator explaining a failed check
could not see what that check left behind -- no `.venv`, no build
output, no partial artefact. In the pod it reads the actual aftermath.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from kernel.verifier import inspect_tree

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── which backend a run gets ───────────────────────────────────────────

def test_a_warm_host_gives_the_pod():
    got = asyncio.run(inspect_tree.for_run(
        None, {"_warm_host": "10.48.128.116"}, {}))

    assert isinstance(got, inspect_tree.PodTree)


def test_no_pod_and_an_archive_gives_the_tar(monkeypatch):
    """Docker and E0. Unchanged behaviour, and it must stay that way --
    holding there would wedge every local and corpus run."""
    monkeypatch.setattr("kernel.verifier.tarslice.names",
                        lambda tar: ["verify/a.py"])

    got = asyncio.run(inspect_tree.for_run(
        object(), {"_tree_tar": b"not-really-a-tar"}, {"base_image": "i:1"}))

    assert isinstance(got, inspect_tree.TarTree)


def test_neither_gives_nothing_and_that_is_an_accept():
    """Both agents already treat None as "do not run", and that is the
    safe direction: an assessor that cannot see the work must not refuse
    it."""
    assert asyncio.run(inspect_tree.for_run(None, {}, {})) is None
    assert asyncio.run(inspect_tree.for_run(None, None, None)) is None


def test_the_pod_wins_when_both_are_present():
    """A run that staged a tar AND has a warm pod reads the pod: it is
    the tree the checks actually ran against, aftermath included."""
    got = asyncio.run(inspect_tree.for_run(
        object(), {"_warm_host": "10.0.0.1", "_tree_tar": b"x"}, {}))

    assert isinstance(got, inspect_tree.PodTree)


# ── what the pod backend actually sends ────────────────────────────────

def _capture(monkeypatch):
    sent = []

    async def fake_run(host, command, timeout_s, port=None):
        sent.append((host, command))
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr("kernel.verifier.shell.run", fake_run)
    return sent


def test_every_tool_runs_in_the_tree_the_criteria_used(monkeypatch):
    """`cwd` is `volume.TREE` -- the same directory `batch.run` gives
    the criteria -- so a path the model saw in a failure message is the
    path it can read here."""
    from kernel.verifier import volume

    sent = _capture(monkeypatch)
    tree = inspect_tree.PodTree("10.0.0.1")

    asyncio.run(tree.list_files("src"))
    asyncio.run(tree.read_file("app.py"))
    asyncio.run(tree.run("pytest -x"))

    assert len(sent) == 3
    for host, command in sent:
        assert host == "10.0.0.1"
        assert command.startswith(f"cd {volume.TREE} &&"), command


def test_the_pod_carries_the_environment_a_criterion_had(monkeypatch):
    """Without the PATH prepend the investigator's `python` is the
    image's and not the built `.venv`'s, so it reports a missing package
    the check itself could import -- and sends the executor after the
    wrong thing."""
    sent = _capture(monkeypatch)
    tree = inspect_tree.PodTree(
        "10.0.0.1", prepend="/verify/env/repos/a/.venv/bin",
        env={"VIRTUAL_ENV": "/verify/env/repos/a"})

    asyncio.run(tree.run("python -c 'import fastapi'"))

    command = sent[0][1]
    assert "VIRTUAL_ENV=" in command
    assert "/verify/env/repos/a/.venv/bin" in command
    assert 'PATH=' in command


def test_the_pod_holds_no_tree_in_memory():
    """`TarTree` keeps a whole archive in `bytes`; `PodTree` keeps an
    address. That is the difference that decides whether a large
    codebase can be assessed at all."""
    tree = inspect_tree.PodTree("10.0.0.1")

    assert not [v for v in vars(tree).values() if isinstance(v, (bytes,
                                                                 bytearray))]


def test_a_bridge_fault_is_not_a_failing_command(monkeypatch):
    """An unreachable pod is not a verdict, and must not read to the
    model as a command that failed."""
    async def faulty(host, command, timeout_s, port=None):
        return {"error": "connection refused", "output": "", "exit_code": None}

    monkeypatch.setattr("kernel.verifier.shell.run", faulty)

    got = asyncio.run(inspect_tree.PodTree("10.0.0.1").run("pytest"))

    assert "could not complete" in got
    assert "exit=" not in got


# ── the gates that hid the whole thing ─────────────────────────────────

@pytest.mark.parametrize("rel,who", [
    ("kernel/verifier/second_key.py", "the assessor"),
    ("kernel/verifier/verdict.py", "the investigator"),
])
def test_neither_gate_tests_for_the_archive_any_more(rel, who):
    """THE ONE-WORD BUG. Both read `_tree_tar` and returned early when
    it was absent -- which on the volume path is always. The gate is the
    TREE now, and `for_run` answers it from a pod or an archive."""
    # Comments stripped, so the prose explaining the fix cannot satisfy
    # the absence check -- that trap has cost this suite four times.
    code = ast.unparse(ast.parse(_src(rel)))

    assert "for_run" in code, f"{who} no longer builds a tree"
    assert "_tree_tar" not in code, (
        f"{who} gates on the archive again, so it does not run at all "
        f"on the warm path -- which is every Kubernetes verification")


def test_verify_publishes_the_host_the_way_stage_publishes_the_tar():
    """The symmetry is the point: one lookup in `for_run` answers both
    paths, and neither agent has to know which ran."""
    code = _src("kernel/verifier/verify.py")

    assert '_warm_host' in code, (
        "verify_claim no longer puts the warm host on the report, so "
        "`for_run` finds neither backend and both agents stay silent")


def test_the_answerer_keeps_the_archive_on_purpose():
    """It is NOT verification. It answers a question about a run from
    the record and a throwaway copy of the tree, in states -- PENDING,
    PARKED -- that have no pod at all, and `deliver.py` is explicit that
    it "never touches the sandbox"."""
    code = _src("kernel/answerer/agent.py")

    assert "TarTree" in code
    assert "PodTree" not in code, (
        "the answerer reaches for a verification pod; it runs for stories "
        "that have none, and touching the sandbox is what it must not do")
