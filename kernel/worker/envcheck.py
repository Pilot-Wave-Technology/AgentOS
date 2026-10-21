"""Did the setup actually produce what the registry said it would?

THE GAP THIS CLOSES. The scout -- the thing that reads an unfamiliar
repository and drafts a setup for it -- runs in two cases: when
discovery found nothing, and when a discovered setup failed. Neither
covers the case that has cost the most, which is a setup that EXITS 0
and builds the wrong environment.

The `pyproject.toml` row installed `-e .` without the dev extras for
months. `pyproject.toml` matched, so the scout never ran; the install
exited 0, so the failure path never ran; and pytest then died at config
load. The report said `[repo:pyproject.toml] exit=0` and the executor
could only conclude its own repository was broken.

ONE SIGNAL, AND IT IS THE REGISTRY'S OWN. Every row declares `bins` --
the in-tree directories whose executables a check may call bare. If the
install worked, they exist; if none of them exists, the row ran a
command that did not do what the row claims. That is checkable without
knowing anything about the ecosystem, which is what makes it general:
a future Go or Ruby row is covered the day it is added.

IT REPORTS, IT DOES NOT REFUSE. A repo may legitimately install to a
place its row does not name -- and `.agentos/env` exists for exactly
that. So this never fails a setup and never blocks a leg; it puts the
discrepancy in the text the model reads, next to the command that was
run, because "the reader knows who to blame" is the whole point of that
report. Six of the defects this platform has paid for were a layer
making a silent choice the model could not see.
"""

from __future__ import annotations

import logging
import shlex

logger = logging.getLogger(__name__)


def probe_command(setups) -> str:
    """One shell line that prints which declared bin directories exist.

    A single job for the whole pass rather than one per mount: the
    answer is three words per directory and the pass has already spent
    minutes installing.
    """
    tests = []
    for setup in setups or ():
        at = (setup.get("at") or "").strip("/")
        for name in setup.get("bins") or ():
            path = f"{at}/{name}" if at else name
            quoted = shlex.quote(path)
            tests.append(f'[ -d {quoted} ] && echo "yes {path}" '
                         f'|| echo "no {path}"')
    return "; ".join(tests)


def read_probe(output: str) -> tuple[list, list]:
    """`(found, missing)` from the probe's output."""
    found, missing = [], []
    for line in (output or "").splitlines():
        head, _, path = line.strip().partition(" ")
        if not path:
            continue
        if head == "yes":
            found.append(path)
        elif head == "no":
            missing.append(path)
    return found, missing


def line(found, missing) -> str:
    """What to add to the setup report, or "" when there is nothing to say.

    SILENT WHEN EVERYTHING IS WHERE IT SHOULD BE. A report that says so
    on every run trains the reader to skip it, and the case worth
    reading is the other one.
    """
    if not missing:
        return ""
    names = ", ".join(missing)
    if found:
        return (f"[env] the setup ran, but {names} was not created. Tools "
                f"that live there will not be found by name; what did get "
                f"built is in {', '.join(found)}.")
    return (f"[env] THE SETUP EXITED 0 AND BUILT NOTHING WHERE IT SAID IT "
            f"WOULD: {names} does not exist. A bare tool name will resolve "
            f"to the image's copy without this project's dependencies, "
            f"which usually reads as the repository being broken. Check "
            f"the command above, or commit an .agentos/setup.sh or an "
            f".agentos/env that says where things really land.")
