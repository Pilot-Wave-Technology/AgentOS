"""E16 — the one symlink that makes a cached `node_modules` findable.

NODE RESOLVES `node_modules` BY WALKING UP FROM THE SOURCE FILE. Not
from `PATH`, not from `NODE_PATH` (deprecated, and ignored by every
bundler), not from the cwd of the process that started. A criterion
runs `TREE/<at>/test/foo.spec.js`; node asks for
`TREE/<at>/test/node_modules`, then `TREE/<at>/node_modules`, then
`TREE/node_modules`, then `/verify/node_modules`, and stops. The
install put its answer at `ENV/<at>/node_modules`, which is on none of
those paths, so `require('vitest')` fails in a tree whose dependencies
are sitting on the same disk.

`mounts_env[at]["prepend"]` -- which carries `ENV/<at>/node_modules/.bin`
-- makes the BINARY runnable and does nothing for the resolution its
first `require` performs. That is the gap this closes, and it is why a
PATH entry is not an alternative.

PYTHON NEEDS NO EQUIVALENT AND MUST NOT GET ONE. A venv bakes its
interpreter path in ABSOLUTELY, the prepare Job and the verification pod
both mount this volume at `volume.MOUNT`, so `ENV/<at>/.venv/bin/python`
is the same string in both -- fault 12 made impossible rather than
worked around. Routing it through a symlink in the tree would put the
TREE path into every shebang the install writes, and the tree is
replaced on every round.

ONE DECLARATION, TWO SEEDING SIDES. The prepare Job seeds `TREE` for
itself (`prepare_script` emits `shell`) and the Deployment pushes a new
tree into the warm pod on every later round (a python seeder calls
`link`). Both must make the same link or the common path -- a refusal,
then generation two on the SAME environment -- loses it, so the pair of
paths is computed here and nowhere else.
"""

from __future__ import annotations

import logging
import os
import shlex

from kernel.verifier import volume

logger = logging.getLogger(__name__)

# The directory name node walks up looking for. Declared rather than
# spelled at each site: `setups.REGISTRY` already names
# `node_modules/.bin` as a bin directory, and a second spelling of the
# first path segment is a second thing to keep in step.
NODE_MODULES = "node_modules"


def _at(base: str, at: str) -> str:
    return f"{base}/{at}" if at else base


def pair(at: str) -> tuple[str, str]:
    """(where the link goes, what it points at) for one mount."""
    return (f"{_at(volume.TREE, at)}/{NODE_MODULES}",
            f"{_at(volume.ENV, at)}/{NODE_MODULES}")


def shell(at: str) -> str:
    """The link as `sh`, for a seeder that is already a shell script.

    CONDITIONAL ON THE TARGET, because most mounts are not node ones and
    a dangling `node_modules` in the tree is worse than none: `ls` shows
    it, an editor follows it, and a tool that stats it reports a broken
    installation rather than an absent one.

    THE LINK IS REPLACED, NOT ADDED TO. `ln -s` onto an existing symlink
    creates the link INSIDE the directory it points at -- so a second
    round would produce `ENV/<at>/node_modules/node_modules` and leave
    the tree's entry pointing at the previous build. `rm -f` first is
    what makes this idempotent, and idempotence is the ordinary case:
    plan 5.4 step 9a re-seeds the tree on every refusal.
    """
    link, target = pair(at)
    return (f"if [ -d {shlex.quote(target)} ]; then "
            f"rm -rf {shlex.quote(link)}; "
            f"ln -s {shlex.quote(target)} {shlex.quote(link)}; fi")


def link(at: str) -> bool:
    """The same link, from python. True when one was made.

    For the OTHER seeding side -- the Deployment pushing a tree into the
    warm pod between generations. It runs no shell there, and a second
    hand-rolled `os.symlink` beside `shell` above is how the two come to
    disagree about which directory node is looking for.
    """
    where, target = pair(at)
    if not os.path.isdir(target):
        return False
    if os.path.islink(where) or os.path.exists(where):
        # See `shell`: symlinking onto a live symlink writes THROUGH it.
        if os.path.islink(where) or os.path.isfile(where):
            os.unlink(where)
        else:
            import shutil                                   # noqa: PLC0415

            shutil.rmtree(where)
    os.symlink(target, where)
    logger.info("linked %s -> %s", where, target)
    return True
