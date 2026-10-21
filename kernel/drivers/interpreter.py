"""What runs the shim, in an image that may have no Python at all.

THE FAILURE THIS REMOVES. The sandbox's command is `python
<bridge>/shim.py`. Name an image without a `python` on PATH -- a Go
image, a Rust image, anything built `FROM scratch` plus a toolchain --
and the pod starts, the bridge never answers, and the story reads as a
dead pod rather than as a missing interpreter.

THERE IS NOTHING TO REFUSE ON, which is why this is not a check. No
route exists to inspect an image's contents before running it: the
Docker socket proxy runs without `IMAGES=1` and `preflight.sh` says in
as many words not to add it, and on Kubernetes there is no registry
client. All that exists is the image's NAME. A refusal could only be a
name allowlist, and it would be wrong for exactly the case `base_image`
promises works -- "a goal that works on a Rust repository names a Rust
image here once."

SO THE PROBLEM IS MADE NOT TO EXIST. An initContainer already copies the
bridge into the sandbox from the kernel's own image. A self-contained
interpreter travels the same way, and the invariant holds by
construction for any image, with nothing to refuse.

AND IT SETTLES A LIVE INCONSISTENCY. The Docker path invoked `python3`
and the Kubernetes path invoked `python`. An image carrying one and not
the other worked locally and failed in the cluster, which is the worst
shape a difference can take. Bare `python` is gone from both: where the
shipped interpreter is not available the fallback is `python3`, the name
Debian, Ubuntu, Alpine and RHEL all agree on.

WHAT IS STILL OUT OF REACH, said plainly. The shipped build is
relocatable and carries its own libssl and libffi, but it is linked
against glibc. A musl image -- Alpine without `gcompat` -- still cannot
run it, and the fallback is what answers there. That is a narrower gap
than "any image without python" and it is not nothing.
"""

from __future__ import annotations

import os

# WHERE THE KERNEL IMAGE KEEPS IT. Read from the conductor's own
# filesystem, which is the same image the initContainer runs -- so
# "do I have one to ship" and "will the copy find one" are the same
# question, answered once, in the process building the manifest.
SOURCE_DIR = "/opt/agentos-python"
SOURCE_ENV = "AGENTOS_SHIPPED_PYTHON"

# Where it lands in the sandbox, and what runs. Relative to the bridge
# directory so the two travel as one volume: a sandbox that has the shim
# has the thing that runs it.
SHIPPED_SUBDIR = "python"
SHIPPED_BIN = "python/bin/python3"

# `python3`, NEVER bare `python`. On Debian and Ubuntu `python` has not
# existed by default since the 2to3 split; on the official `python:`
# images both exist. Choosing the name that is present in more places is
# free, and it is the difference the two substrates used to disagree on.
FALLBACK = "python3"


def source_dir() -> str:
    """The directory in the kernel image holding the shipped build."""
    if SOURCE_ENV in os.environ and os.environ[SOURCE_ENV].strip():
        return os.environ[SOURCE_ENV].strip()
    return SOURCE_DIR


def available() -> bool:
    """Is there an interpreter to ship?

    STATTED, NOT ASSUMED, so this lands safely before the image that
    carries one. Until then every sandbox falls back exactly as it does
    today; the day the image ships one, every sandbox picks it up with
    no further change.
    """
    return os.path.isdir(os.path.join(source_dir(), "bin"))


def copy_command(bridge_dir: str) -> str:
    """The shell the initContainer runs to deliver the interpreter.

    `cp -r` and not `-a`, for the reason `k8s_bridge.init_container`
    records at length: `-a` implies `-p`, only root may preserve another
    owner or an arbitrary mtime, and the container is deliberately not
    root -- so a complete and perfectly usable copy exited non-zero and
    the pod reported Init:Error.

    The executable bits ARE preserved: `cp -r` keeps the mode of the
    files it creates, and it is ownership and timestamps that `-p`
    adds. An interpreter copied without its `+x` would be a subtler
    version of having no interpreter at all.
    """
    target = f"{bridge_dir}/{SHIPPED_SUBDIR}"
    return f"mkdir -p {target} && cp -r {source_dir()}/. {target}/"


def shim_command(bridge_dir: str, *, shipped: bool,
                 bind: str | None = None) -> list[str]:
    """Argv for the sandbox: the shim, and the thing that runs it.

    An ABSOLUTE path when shipped. A relative one would resolve against
    the contract image's working directory, which is the contract's
    business and not ours.
    """
    runner = (f"{bridge_dir}/{SHIPPED_BIN}" if shipped else FALLBACK)
    argv = [runner, f"{bridge_dir}/shim.py"]
    if bind is not None:
        argv += ["--bind", bind]
    return argv
