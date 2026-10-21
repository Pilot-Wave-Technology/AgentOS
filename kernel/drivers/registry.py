"""Which substrate runs the sandbox? One place that decides.

G7 asks for a per-driver CI axis, and building it turned up that there
was nothing to parameterise: `DockerDriver()` was constructed directly at
every call site. An axis over a constructor is not an axis — Stage I
would have had to edit each site and then discover the ones it missed at
runtime.

THERE ARE TWO NOW, AND ADDING THE SECOND WAS A ROW. That was the claim
this module made when it had one entry — "Stage I adds a row rather than
a search-and-replace" — and it held: `kubernetes` is one line here plus
a module, with no call site changed. What did not come free is inside
the seam rather than at it: the pod manifest, the process model, and
`sandbox_attachments`, which Kubernetes refuses outright because a pod's
container list is fixed at admission.

Selection still happens once and an unknown name is still refused by
name with the list of what exists.

A registry that pretended to be provider-agnostic while never having run
two providers would be the same shape as the model registry before it
was grounded in `models.list()` — plausible, untested, and wrong in a way
nothing could see.
"""

from __future__ import annotations

import os

# name -> "module:attribute". Imported lazily so that selecting docker
# never imports a Kubernetes client, and vice versa: a driver whose
# dependency is absent must not break the process that is not using it.
SUPPORTED = {
    "docker": "kernel.drivers.docker:DockerDriver",
    "kubernetes": "kernel.drivers.kubernetes:KubernetesDriver",
}

ENV = "AGENTOS_DRIVER"
DEFAULT = "docker"


class UnsupportedDriver(Exception):
    """Refused by name, with what does exist — never a silent fallback.

    Falling back to the default on an unknown name is how a CI axis
    reports green for a substrate it never ran.
    """


def driver_name() -> str:
    return os.environ.get(ENV) or DEFAULT


def resolve(name: str | None = None):
    """The driver CLASS for `name`. Constructing it is the caller's job."""
    chosen = name or driver_name()
    target = SUPPORTED.get(chosen)
    if target is None:
        raise UnsupportedDriver(
            f"{chosen!r} is not a substrate driver this build has. "
            f"Available: {sorted(SUPPORTED)}. Set {ENV} to one of them, or "
            f"add a row to kernel/drivers/registry.py. Each driver's "
            f"client is imported lazily, so a name being listed here does "
            f"not mean its dependency is installed — selecting it will say "
            f"so plainly if it is not.")
    module_name, _, attr = target.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def make(name: str | None = None, **kwargs):
    """Construct the selected driver."""
    return resolve(name)(**kwargs)
