"""Where a replayed dependency lives — one declaration, both managers.

THE HOLE THIS CLOSES. `env_install(npm)` was recorded `reproducible =
false` and the model was told the checks would run without it. So a
Node story that installed a test runner ad-hoc and then wrote a check
needing it got `error` in verification, forever, with nothing it could
do about it. pip had a replay and npm did not.

The reason was never npm. It was that `npm install -g` mutates the
image, and an image mutation cannot be carried in a directory — the
same thing that makes `apt` unreplayable. A LOCAL install can be, and
is the only change needed on the executor's side.

WHY NODE NEEDS NO `PYTHONPATH`. Python finds a directory because
something points at it; node finds `node_modules` by walking UP from
the importing file. The tree lands at `/tmp/verify`, so a `node_modules`
at `/tmp` is on that walk from every repo beneath it — for `require`
AND for `import`, which is the part `NODE_PATH` cannot do. NODE_PATH is
consulted by the CommonJS resolver alone; the ESM resolver ignores it,
and ESM is what a current Node project writes. Pointing at
`/tmp/deps/node_modules` and calling it done would have reproduced the
hole for exactly the projects most likely to hit it.

So the ONE captured directory carries both and the archive is re-rooted
into TWO, which is `rebase`'s whole job:

    capture   /deps            pip --target, npm --prefix
              /deps/node_modules
    criterion /tmp/deps        PYTHONPATH
              /tmp/node_modules   node's own resolution, no variable

One capture, one Job, one tar. The alternative — a second capture for
node — is a second Job, a second failure mode, and two archives to keep
in step.

A python distribution shipping a top-level package literally named
`node_modules` would be re-rooted with the node tree and fall off
PYTHONPATH. No such distribution is known; said out loud because the
split is by name and a silent one would be hard to find.
"""

from __future__ import annotations

import io
import shlex
import tarfile

# Inside the CAPTURE container: where both managers install, and the one
# directory the sidecar takes away.
CONTAINER_DEPS = "/deps"

# Inside a CRITERION container: where that archive unpacks. `/tmp`
# because Docker will not create a destination that is not in the image,
# and because it is the parent the tree already uses.
DEPS_PARENT = "/tmp"

# The two roots the archive is re-rooted into, and where they land.
PY_PREFIX = "deps"
NODE_PREFIX = "node_modules"
PY_PATH = "/tmp/deps"
NODE_DIR = "/tmp/node_modules"
NODE_BIN = "/tmp/node_modules/.bin"

# NOT `-g`. A global install writes into the image's own tree, which is
# what made this unreplayable; a prefix install is a directory, and a
# directory travels. The rest keep the prefix clean: a `package.json` or
# lockfile written beside the packages is a file the capture carries and
# `only_new` would diff, and npm's funding and audit chatter is output
# the model has to read past.
NPM_FLAGS = ("--no-save --no-package-lock --no-fund --no-audit "
             "--loglevel=error")


def _join(packages) -> str:
    """Quoted, because a version specifier is shell syntax.

    `fastapi>=0.110.0` unquoted reaches `sh` as a REDIRECT — measured on
    execution 197, which left five files named `=0.110.0` and friends in
    the workspace root. The same is true of `@scope/pkg@^1.2.3`.
    """
    return " ".join(shlex.quote(str(p)) for p in packages)


def pip_install(target: str, packages) -> str:
    return (f"python3 -m pip install --no-input --quiet "
            f"--target {target} {_join(packages)}")


def npm_install(prefix: str, packages) -> str:
    """`--prefix` EXPLICIT, never npm's default.

    npm's default prefix is "the nearest parent folder containing either
    a package.json or a node_modules" — it WALKS UP. In a workspace root
    holding neither it would climb out of the workspace entirely and
    install somewhere nothing reads. The pip call above has no such
    behaviour, which is why only this one has to say it.
    """
    return f"npm install --prefix {prefix} {NPM_FLAGS} {_join(packages)}"


def _reroot(name: str) -> str:
    """Where one member of the captured `/deps` archive belongs.

    The capture returns members rooted at `deps/`. Everything under
    `deps/node_modules` is node's and moves to its own root; the rest is
    pip's and stays.
    """
    rest = name.split("/", 1)[1] if "/" in name else ""
    if rest == NODE_PREFIX or rest.startswith(NODE_PREFIX + "/"):
        return NODE_PREFIX + rest[len(NODE_PREFIX):]
    return PY_PREFIX + ("/" + rest if rest else "")


def rebase(tar_bytes: bytes) -> bytes:
    """Re-root the captured archive so it unpacks where the paths point.

    A base image whose pip target differs, or a future capture path,
    would otherwise unpack somewhere PYTHONPATH does not point at and
    node does not walk through. Normalising here means one place decides
    where dependencies live.
    """
    if not tar_bytes:
        return b""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as src, \
            tarfile.open(fileobj=out, mode="w") as dst:
        for member in src.getmembers():
            member.name = _reroot(member.name.rstrip("/"))
            dst.addfile(member, src.extractfile(member)
                        if member.isfile() else None)
    return out.getvalue()
