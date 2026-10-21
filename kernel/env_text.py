"""What the model is told about the machine, matched to the machine.

THE FAILURE THIS REMOVES. `ENVIRONMENT` was one constant describing one
image, and `base_image.for_goal` exists precisely so a goal can name a
different one -- "a goal that works on a Rust repository names a Rust
image here once". The FIRST goal to use that gets an executor told about
a machine it is not running on: a compiler it may not have, an `apt`
rule that may not apply, package managers that are not installed. Every
one of those is a decision the text exists to inform.

IT TRAVELS ON THE SPEC, NOT THROUGH A QUERY, and that is forced. Both
readers -- `profiles/prompt.py` and `worker/scout.py` -- run INSIDE the
pod, and neither holds a connection or a goal id, so `for_goal` is not
callable from either. The conductor already resolves the image at
dispatch; it puts the answer in the spec env and the pod reads it.

AND IT MUST NOT BECOME A PER-LEG DATABASE READ. The text sits in the
STABLE PREFIX of every request. `profiles/prompt.py` warns that anything
time-varying there turns every step into a cache miss -- §8.3 prices the
difference at $60 against $314. An environment variable fixed for the
life of the pod keeps the prefix byte-identical across legs; a query
would not.

WHAT AN UNRECOGNISED IMAGE GETS is the honest paragraph, not the
default one. Describing an unknown image as if it were ours is the
failure with the worst shape: the model acts on specifics that are
false. Saying "I do not know what is in this one, find out" costs a few
commands and is true.
"""

from __future__ import annotations

import os

# The conductor writes it at dispatch, after the contract, the goal and
# the deployment default have all had their say. `spec_for_dispatch` is
# the one place that knows the answer.
ENV_VAR = "AGENTOS_IMAGE_IN_USE"

# ── the images we build, and therefore can describe ───────────────────
#
# MATCHED ON A MARKER, NOT A FULL NAME. The same image is
# `agentos-kernel/fixture:dev` on E0 and
# `us-central1-docker.pkg.dev/<project>/agentos-<env>/sandbox-base@sha256:…`
# in the cluster, and a table keyed on full names would answer "unknown"
# for the one image we actually build.
#
# NO ENVIRONMENT NAME APPEARS HERE. A third marker used to read
# `agentos-staging/sandbox`, which recognised our own image in exactly one
# environment and called it unknown everywhere else -- including every
# installation that is not ours. `sandbox-base` is the repository name
# the build always uses (`scripts/infra/names.sh`), whatever the project
# and environment in front of it, so it already matched every case the
# removed marker did.
AGENTOS_MARKERS = ("agentos-kernel/fixture", "sandbox-base")

# The plain upstream Python images -- `SHIM_IMAGE`'s fallback and
# whatever a goal names directly. Real, and much smaller than ours.
PYTHON_MARKERS = ("python:", "python@sha256:")

AGENTOS = """\
# Your environment

Debian 13 (trixie). Python 3.12 (`python3`, `pip`, `venv`, `uv`), Node 24
(`node`, `npm`, `corepack` for pnpm/yarn), `git` and `git-lfs`.

A C AND RUST TOOLCHAIN IS PRESENT -- `gcc`, `g++`, `make`, `pkg-config`,
`rustc`, `cargo` and the common dev headers (postgres, ssl, ffi,
xml/xslt, zlib, jpeg). A dependency with no prebuilt wheel will BUILD
rather than fail, so do not avoid one or pin around it. `psql` and
`sqlite3` are here for looking at a database; `curl`, `wget`, `jq`,
`unzip`, `file`, `ps` and `less` for everything else.

`apt` IS NOT AVAILABLE. This container is discarded and the tree is
rebuilt from its committed manifests between rounds, so a system package
installed here is gone by your next leg -- and the sandbox is non-root,
so it cannot be installed at all. If you genuinely need one, `ask_human`
to have it added to the goal's base image.

Install dependencies with `env_setup`, which applies the tree's own
committed manifest, or `env_install` for a one-off. Both are recorded
and both survive the rebuild. Anything installed through the shell does
not, and is refused for that reason.
"""

PYTHON_SLIM = """\
# Your environment

A plain upstream Python image -- `python3`, `pip` and `venv`, and very
little else. NO compiler, no Node, and not necessarily `git`, `curl` or
a test runner. A dependency without a prebuilt wheel will FAIL rather
than build.

`apt` IS NOT AVAILABLE: the sandbox is non-root and the container is
discarded between rounds. If the work needs a toolchain, `ask_human` to
have the goal's base image changed rather than working around its
absence.

Install dependencies with `env_setup`, which applies the tree's own
committed manifest, or `env_install` for a one-off. Both are recorded
and survive the tree being rebuilt; anything installed through the shell
does not.
"""

UNKNOWN = """\
# Your environment

This goal names its own base image, and I cannot tell you what is in it.
Do not assume a compiler, a test runner, Node, or any particular Python
version -- CHECK, with `which` or `--version`, before you plan around
one. Three commands now beats a leg spent on a tool that is not there.

`apt` IS NOT AVAILABLE whatever the image: the sandbox is non-root and
the container is discarded between rounds, so a system package installed
here is gone by your next leg. If one is genuinely needed, `ask_human`
to have it added to the goal's base image.

Install dependencies with `env_setup`, which applies the tree's own
committed manifest, or `env_install` for a one-off. Both are recorded
and survive the rebuild; anything installed through the shell does not.
"""


def image_in_use() -> str:
    """The image this run is actually in, as the conductor resolved it."""
    if ENV_VAR in os.environ:
        return os.environ[ENV_VAR].strip()
    return ""


def text_for(image: str) -> str:
    """The paragraph that describes `image`. Unknown gets UNKNOWN.

    An empty name is unknown too, and deliberately: a pod dispatched by
    code that has not learned to set the variable should say it does not
    know rather than describe whatever we last built.
    """
    name = (image or "").strip().lower()
    if not name:
        return UNKNOWN
    if any(marker in name for marker in AGENTOS_MARKERS):
        return AGENTOS
    if any(name.startswith(marker) for marker in PYTHON_MARKERS):
        return PYTHON_SLIM
    return UNKNOWN


def environment_text() -> str:
    """What to put in the prompt, for the image this pod is running."""
    return text_for(image_in_use())
