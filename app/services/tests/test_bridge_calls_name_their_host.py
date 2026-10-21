"""Every bridge call says WHERE the bridge is.

THE DEFECT, and it is the fourth of its exact shape in one day.

`call_bridge` and `bridge_health` default `host` to "localhost". That is
right on Docker, where the shim shares the worker's network namespace,
and wrong on Kubernetes, where the shim is a container in another pod.
The parameter was added for precisely this, and `bridge_health`'s own
docstring states the failure:

    a readiness probe aimed at the caller's own loopback reports a pod
    sandbox as never ready, which reads as a hung dispatch

Three call sites were not updated. The worst was `ensure_reachable`,
which probes the bridge once per tick to decide whether a run is alive:
it asked its own loopback, found nothing, declared a perfectly healthy
pod dead, abandoned the leg and re-dispatched. Execution 9 reached leg 8
with zero steps, cycling RUNNING to DISPATCHING and back, while the pod
it kept destroying had composed its workspace correctly every time.

WHY A PARAMETER DEFAULT IS THE TRAP. An added argument with no default
breaks every caller loudly at import or at the first call, and gets
fixed in one pass. A default keeps them all compiling and changes what
they MEAN -- and the ones that were right before stay right, so the
diff looks complete. Only the substrate that needed the argument
notices, at run time, in a log nobody is reading yet.

So the rule is asked of the call sites, the same way
`test_driver_contract.py` asks about driver kwargs after an identical
lesson: any code that can run against a pod must name the host.
`kernel/drivers/docker.py` is exempt because it IS the substrate where
loopback is the answer.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
KERNEL = REPO / "kernel"

# Functions that reach a bridge and take a `host`.
REACHING = {"call_bridge", "bridge_health"}

# The one substrate where the default is the right answer: the shim
# shares the worker's netns, so localhost IS the sandbox.
EXEMPT = {"kernel/drivers/docker.py"}


def _calls() -> list:
    """Every call to a reaching function, as (file, line, names)."""
    out = []
    for path in KERNEL.rglob("*.py"):
        if "tests" in path.parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                              # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or \
                getattr(node.func, "attr", "")
            if name not in REACHING:
                continue
            out.append((rel, node.lineno, name,
                        {k.arg for k in node.keywords if k.arg}))
    return out


def test_the_sweep_finds_the_call_sites():
    # A parser matching nothing reports every call site correct.
    found = _calls()
    assert len(found) >= 4, f"only found {found}"
    assert {c[2] for c in found} == REACHING, "a reaching function moved"


def test_every_bridge_call_names_its_host():
    """The one that would have caught it."""
    missing = [f"{f}:{line} {name}()" for f, line, name, kwargs in _calls()
               if f not in EXEMPT and "host" not in kwargs]
    assert not missing, (
        f"{missing} call the bridge without a host, so they reach the "
        f"CALLER's loopback. On Docker that is the sandbox and the code "
        f"is right; on Kubernetes it is the conductor, and the symptom "
        f"is a healthy pod reported dead once per tick -- which is what "
        f"took execution 9 to leg 8 with zero steps.")


def test_the_exemption_is_still_the_docker_driver():
    # If the Docker driver stops calling the bridge, the exemption is a
    # hole waiting for the next file to be added to it.
    docker_calls = [c for c in _calls() if c[0] in EXEMPT]
    assert docker_calls, (
        "nothing in EXEMPT calls the bridge any more; drop the exemption "
        "rather than leaving a named file that can never be checked")


def test_the_reachers_still_take_a_host():
    """A default that quietly disappears would make this whole file
    pass by asking about an argument nobody accepts."""
    import inspect

    from kernel.bridge.client import bridge_health, call_bridge
    for fn in (call_bridge, bridge_health):
        assert "host" in inspect.signature(fn).parameters, fn.__name__
