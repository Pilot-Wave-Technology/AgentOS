"""Does the gateway offer the model exactly the tools the kernel serves?

THE FOURTH DECLARATION, and the one furthest from the code that needs
it. A tool is real only if it exists in all four places: the kernel
package that implements it, the route set that serves it, the template
allowlist that permits it, and the gateway spec that OFFERS it to the
model. Miss the fourth and the model is never shown the tool — its
prompt promises something it cannot call, and there is no error anywhere
because nothing was attempted.

IT HAS DRIFTED EVERY TIME. `start_story` and `list_resources` (template
allowed twelve, gateway registered eight). `read_resource` (in GO's
kernel surface, not on the gateway, for a week — and it was the tool
that reads an uploaded document). `list_artifacts` twice over: Ec2g
added it for the assistant and I3c for GO, both stopped at the template,
so the whole "an errand's deliverable is its artifacts, here is how to
read them" capability was unreachable by either model from the day it
was built.

WHY IT KEEPS DRIFTING is not carelessness. The gateway is a different
repository in a different language, and the check that compares them
could only run inside the E0 container, against a mount that has to be
configured — so in the place where the work actually happens it reported
UNCHECKED, and everybody read the green. That is the same shape as the
`/app/` paths in `test_effects_cover_profiles` and `test_h8b_prompt`:
correct logic behind a path that does not exist where anyone runs it.

So the path is RESOLVED rather than assumed, and this module is the one
implementation every caller uses — the E0 gate and the ordinary test
suite. A second copy of this comparison would be a fifth declaration of
the same fact.

WHY IT LIVES IN kernel/ AND NOT app/. It began in app/services/, and two
E0 suites that need it -- H10 (the tool surface over HTTP) and H13 (a
person can ask) -- could not import it, because E0's container ships
kernel/ and deliberately not app/. Both failed with ModuleNotFoundError
and were quietly left out of the gate, which is the same silence this
module exists to break.

Nothing here was ever app-shaped: `served()` reads kernel.go.tools and
kernel.assistant, and there is no import from app/ anywhere in the file.
app/services/gateway_surface.py remains as a re-export so app-side
callers keep working.
"""

from __future__ import annotations

import os
import pathlib
import re

# `name: "goal_state"` in the SPECS table of each tool file.
_NAME = re.compile(r'name:\s*"([a-z_]+)"')

FILES = {"assistant": "kernel-assistant-tools.ts",
         "go": "kernel-go-tools.ts"}


def specs_dir() -> pathlib.Path | None:
    """Where the gateway's tool specs are, or None if they are not here.

    Three places, in order of how deliberate they are: an explicit
    override, the container mount, and the sibling checkout — which is
    the one that makes this runnable on a laptop, and therefore the one
    that makes it a check rather than a note.
    """
    # No default argument: `kernel/` bans the two-argument form of
    # environ.get outright (B1), because a default in the getter is a
    # second declaration of a fact the caller already owns.
    explicit = (os.environ.get("GATEWAY_TOOLS_DIR") or "").strip()
    candidates = [pathlib.Path(explicit)] if explicit else []
    candidates.append(pathlib.Path("/app/gateway-tools"))
    # THE REPO ROOT, FOUND RATHER THAN COUNTED. This file is
    # kernel/gateway/surface.py, so on a host checkout the root is two
    # parents up -- but inside the E0 container the tree is grafted at
    # /app and counting parents walks off the top of the filesystem. That
    # is the exact defect this module was written to stop (see the
    # docstring), so it must not be the one it commits: walk up until a
    # directory looks like the checkout, and stop.
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "kernel").is_dir() and (parent / "migrations").is_dir():
            candidates.append(
                parent.parent / "agent_os_openclaw/src/agents/tools")
            break
    for path in candidates:
        if path.is_dir() and (path / FILES["go"]).is_file():
            return path
    return None


def declared(path: pathlib.Path, who: str) -> set:
    """The tool names the gateway offers the model on that surface."""
    spec = path / FILES[who]
    if not spec.is_file():
        return set()
    return set(_NAME.findall(spec.read_text(encoding="utf-8")))


def served(who: str) -> set:
    """The tool names the kernel actually serves on that surface."""
    if who == "go":
        from kernel.go.tools import TOOLS

        return set(TOOLS)
    from kernel.assistant import TOOLS

    return set(TOOLS)


def compare(path: pathlib.Path | None = None) -> dict:
    """{surface: (only-in-kernel, only-on-gateway)} for both surfaces.

    Both directions matter and they fail differently. A tool the kernel
    serves and the gateway omits is one the model is never offered — it
    reads about it in its prompt and has no way to call it. A tool the
    gateway declares and the kernel does not serve is one the model IS
    offered and gets a 404 from, which reads to it as a broken system
    rather than a tool that does not exist.
    """
    path = path or specs_dir()
    if path is None:
        raise FileNotFoundError(
            "the gateway's tool specs are not reachable from here. Set "
            "GATEWAY_TOOLS_DIR, mount them at /app/gateway-tools, or "
            "check out agent_os_openclaw beside this repo — an unrunnable "
            "check is how every one of these drifts went unnoticed.")
    return {who: (sorted(served(who) - declared(path, who)),
                  sorted(declared(path, who) - served(who)))
            for who in FILES}
