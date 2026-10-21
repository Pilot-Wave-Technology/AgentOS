"""Every driver accepts what every shared call site actually passes.

THE DEFECT THIS EXISTS FOR, found on the cluster and not by the suite.
`dispatch_admitted` calls `await_ready(container_id, bridge_port=...)`
for whichever driver it was given. `KubernetesDriver.await_ready` took
only `(container_id, timeout)`, so the call raised

    TypeError: await_ready() got an unexpected keyword argument

-- AFTER the pod had been created. Dispatch's cleanup destroyed the pod,
the claim survived (as it must, for preemption), and the next tick built
it again: three pods created and destroyed per minute, orphaned claims
against a quota of 40, and a run stuck in DISPATCHING with nothing in
the UI to say why.

WHY 1,650 TESTS DID NOT SEE IT. Every one of them exercises ONE driver.
Docker's signature was right, Kubernetes' was right on its own terms,
and the mismatch existed only in the relationship between a call site
and an implementation it never runs against locally. A per-driver test
cannot find that by construction, which is why this reads the call
sites instead.

This is the same shape as G7b's provider conformance -- questions asked
of every implementation as DATA, so a third driver is a row rather than
a third suite.
"""
import ast
import inspect
import pathlib

import pytest

from kernel.driver import Driver
from kernel.drivers.docker import DockerDriver
from kernel.drivers.kubernetes import KubernetesDriver

DRIVERS = (DockerDriver, KubernetesDriver)
KERNEL = pathlib.Path(__file__).resolve().parents[3] / "kernel"

# `driver` also names WORKSPACE drivers (CompositeDriver, GitDriver),
# which are a different contract entirely -- `acquire`/`checkpoint`/
# `publish` against a path. Named here rather than pattern-matched,
# because a heuristic that silently skips a real substrate method is a
# check that stops checking.
NOT_SUBSTRATE = {"acquire", "checkpoint", "publish"}


def _driver_calls() -> dict:
    """Every `driver.method(**kwargs)` under kernel/, as {method: kwargs}."""
    found: dict = {}
    for path in KERNEL.rglob("*.py"):
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                      # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            owner = getattr(fn.value, "id", "") or getattr(fn.value, "attr", "")
            if "driver" not in str(owner).lower():
                continue
            if fn.attr.startswith("_") or fn.attr in NOT_SUBSTRATE:
                continue
            found.setdefault(fn.attr, set()).update(
                k.arg for k in node.keywords if k.arg)
    return found


CALLS = _driver_calls()


def test_the_sweep_found_call_sites_at_all():
    # A parser that quietly matches nothing reports every driver perfect.
    assert len(CALLS) >= 8, f"only found {sorted(CALLS)}"
    assert "await_ready" in CALLS
    assert "bridge_port" in CALLS["await_ready"], (
        "the call site that caused this test no longer passes bridge_port; "
        "if that is deliberate, the story below needs rewriting")


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.__name__)
def test_every_driver_implements_what_is_called_on_it(driver):
    missing = [m for m in CALLS if getattr(driver, m, None) is None]
    assert not missing, f"{driver.__name__} is missing {missing}"


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.__name__)
def test_every_driver_accepts_the_kwargs_it_is_passed(driver):
    """The one that would have caught it.

    A driver may IGNORE an argument -- the pod driver ignores
    `bridge_port`, because a pod has one sandbox on a fixed port -- but
    it may not refuse one, because the call site cannot know which
    driver it holds.
    """
    refused = []
    for method, kwargs in sorted(CALLS.items()):
        impl = getattr(driver, method, None)
        if impl is None:
            continue
        params = inspect.signature(impl).parameters
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue
        for kw in sorted(kwargs):
            if kw not in params:
                refused.append(f"{method}({kw}=...)")
    assert not refused, (
        f"{driver.__name__} would raise TypeError on {refused} -- and on "
        f"the substrate that call site runs against, it would raise after "
        f"the container was already created")


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.__name__)
def test_every_driver_answers_every_declared_model(driver):
    """The MODEL attributes are the seams a caller branches on.

    A driver missing one gets the base class's default silently, which
    is "local" or "push" -- i.e. the Docker answer. A Kubernetes driver
    that forgot to declare one would be asked to do the Docker thing and
    would fail somewhere else entirely.
    """
    for name in ("CAPTURE_MODEL", "TREE_MODEL", "WORKSPACE_MODEL",
                 "ARCHIVE_MODEL"):
        assert isinstance(getattr(Driver, name), str), f"base lacks {name}"
        assert isinstance(getattr(driver, name), str)


def test_the_models_actually_differ_between_substrates():
    # If every seam answered the same on both, the seams would be
    # ceremony -- and a real difference would be going unhandled.
    differing = [n for n in ("CAPTURE_MODEL", "TREE_MODEL",
                             "WORKSPACE_MODEL", "ARCHIVE_MODEL")
                 if getattr(DockerDriver, n) != getattr(KubernetesDriver, n)]
    assert len(differing) == 4, f"only {differing} differ"
