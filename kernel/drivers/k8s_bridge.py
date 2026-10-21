"""Getting the bridge shim into a sandbox that has never heard of it.

THE SANDBOX RUNS THE CONTRACT'S IMAGE, not the kernel's. That is the
whole point of a contract: the executor works in the environment the
work was written against, and F6's parity check exists to prove the
Verifier used the same one. So the sandbox image has no `kernel/`, no
`kernel/bridge/`, and no shim to run.

Docker solves this with a bind mount — `<kernel>/bridge:/app/bridge:ro`
— and the `:ro` is load-bearing, because `docker.py` says plainly that
"a sandbox able to edit the shim could rewrite" it. A pod cannot bind
mount from the conductor: there is no shared filesystem, which is the
same wall P3/D1 hits for the workspace.

THE POD EQUIVALENT IS AN initContainer AND AN emptyDir. The init
container runs the KERNEL's image, copies `kernel/bridge` into a scratch
volume, and exits. The sandbox mounts that volume READ-ONLY and runs the
shim out of it. Same bytes, same read-only guarantee, no shared
filesystem needed — and because it is an initContainer, a pod whose
bridge could not be delivered never becomes Ready, rather than starting
a sandbox that answers nothing.

WHY NOT BAKE THE SHIM INTO EVERY EXECUTION IMAGE. It would make every
contract's image our problem: a user-supplied image would have to carry
our bridge at our path and our Python, and a mismatch would surface as
a sandbox that starts and never answers. Delivering it at create time
keeps the contract's image the contract's business.
"""

from __future__ import annotations

import os

from kernel.drivers import interpreter

# Where the bridge lands inside the pod. Not under /workspace: that is
# the executor's own tree, it is writable by the sandbox, and a shim
# living in it would be exactly the "sandbox able to edit the shim"
# Docker's :ro flag exists to prevent.
BRIDGE_DIR = "/opt/agentos-bridge"
BRIDGE_VOLUME = "bridge"

# The initContainer's own allocation. Small and fixed: it copies a
# directory of stdlib Python and exits. In ratio (1:4) because Autopilot
# rewrites anything that is not, init containers included.
INIT_CPUS = "0.25"
INIT_MEMORY_MB = 1024


class BridgeImageMissing(RuntimeError):
    """No image carries the shim, so no sandbox could answer a tool call."""


def kernel_image() -> str:
    """The image that HAS `kernel/bridge` — the conductor's own.

    READ BY THE DRIVER, not by `pod_manifest`. Which image is deployed
    is a deployment fact and the manifest builder is a pure function of
    its arguments; reaching into the environment from inside it made
    seven existing tests fail on a variable that has nothing to do with
    what they assert, which is the tell.


    Refused by name rather than defaulted. A pod built without this
    starts a sandbox whose command is a path that does not exist, and
    the failure arrives later as a tool call that times out, which
    reads as a hung model rather than as a deployment gap.
    """
    image = os.environ.get("AGENTOS_EXECUTION_IMAGE")
    if not image:
        raise BridgeImageMissing(
            "AGENTOS_EXECUTION_IMAGE is not set, so there is no image "
            "that can deliver the bridge shim into a sandbox. The "
            "conductor's own image is the one to name — see "
            "deploy/k8s/conductor.yaml.")
    return image


def volume() -> dict:
    """The scratch volume the shim is copied into."""
    return {"name": BRIDGE_VOLUME, "emptyDir": {}}


def init_mount() -> dict:
    """Writable, for the container doing the copying."""
    return {"name": BRIDGE_VOLUME, "mountPath": BRIDGE_DIR}


def sandbox_mount() -> dict:
    """READ-ONLY. See the module docstring: this is Docker's `:ro`."""
    return {"name": BRIDGE_VOLUME, "mountPath": BRIDGE_DIR,
            "readOnly": True}


def init_container(image: str, uid: int | None = None) -> dict:
    """Copy `kernel/bridge` out of the kernel image into the volume.

    A copy rather than a rebuild: the bytes shipped in the image are the
    bytes that run, so there is nothing to get out of step.

    `-r` AND NOT `-a`, WHICH COST A POD. `cp -a` implies `-p`: preserve
    mode, ownership AND timestamps. That is right for a root copy and
    impossible for any other, because only root may set another owner or
    an arbitrary mtime on a file it is creating. The moment this
    container stopped running as root -- which it had to, since
    `runAsNonRoot` is set and the kernel image declares no USER -- the
    copy still moved every byte and then exited non-zero on

        cp: preserving times for '/opt/agentos-bridge/.': Operation not
        permitted

    so the pod reported Init:Error with a complete and perfectly usable
    bridge sitting in the volume.

    Nothing here needs preserving. The shim is read by python, not
    executed; its owner must CHANGE (to the uid doing the copy, under
    the pod's fsGroup) for the sandbox to read it at all; and its mtime
    is of no interest to anyone.
    """
    # AND THE INTERPRETER, when the kernel image carries one. B2: the
    # sandbox runs the CONTRACT's image, which may have no python at
    # all -- a Go image, a Rust image -- and there is nothing to refuse
    # on, because no route exists to inspect an image's contents before
    # running it. Shipping the interpreter beside the shim makes the
    # problem not exist. `interpreter.available()` stats the conductor's
    # own filesystem, which IS this container's image, so the answer and
    # the copy cannot disagree.
    steps = [f"cp -r /app/kernel/bridge/. {BRIDGE_DIR}/"]
    if interpreter.available():
        steps.append(interpreter.copy_command(BRIDGE_DIR))
    container: dict = {
        "name": "bridge-deliver",
        "image": image,
        "command": ["sh", "-c", " && ".join(steps)],
        "volumeMounts": [init_mount()],
        "resources": {
            "requests": {"cpu": INIT_CPUS, "memory": f"{INIT_MEMORY_MB}Mi"},
            "limits": {"cpu": INIT_CPUS, "memory": f"{INIT_MEMORY_MB}Mi"},
        },
    }
    if uid is not None:
        # The sandbox reads these files as its own uid. Writing them as
        # root would be fine for reading, but matching the uid keeps the
        # ownership story identical to Docker's bind mount.
        container["securityContext"] = {"runAsUser": uid, "runAsNonRoot": uid != 0}
    return container


def sandbox_command(bind: str = "0.0.0.0") -> list[str]:
    """What the sandbox runs instead of sleeping.

    `--bind 0.0.0.0`, and the reason is in `kernel/bridge/shim.py`: a
    pod shares no network namespace with the conductor, so the shim's
    127.0.0.1 default answers nothing and every tool call fails. The
    reach is restricted by the `execution-bridge-ingress` NetworkPolicy
    instead — the namespace is otherwise open on ingress, so that
    policy is not belt-and-braces, it is the only control.

    An argument and not an env var: the sandbox is created with no
    environment at all, deliberately.

    AND THE RUNNER IS THE SHIPPED ONE where there is one. Bare `python`
    used to be spelled here while Docker spelled `python3`, so an image
    carrying one and not the other worked locally and failed in the
    cluster. `interpreter` owns both answers now.
    """
    return interpreter.shim_command(BRIDGE_DIR,
                                    shipped=interpreter.available(),
                                    bind=bind)
