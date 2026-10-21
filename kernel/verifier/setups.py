"""One committed definition of a workspace's environment, per mount.

THE CONTRACT. A repository that needs an environment says so IN ITS OWN
TREE: either an explicit `.agentos/setup.sh`, or a manifest this
registry recognises. The kernel runs that definition in two rooms — the
executor's sandbox when work starts, and the Verifier's prepare
container when work is checked — and because both rooms build from the
same committed text, the executor cannot end up with an environment the
Verifier cannot rebuild. That is the whole fix for "the checks fail for
a reason that is not the work".

LANGUAGE-BLIND BY CONSTRUCTION. The kernel never learns what pip or npm
are; it runs a shell command with cwd at the mount and expects the
result to land INSIDE the tree (`.venv/`, `node_modules/`, `vendor/`).
Every ecosystem has an in-tree mode, and the setup script — the repo's
own code — is where that knowledge lives. The registry below is only a
courtesy for repos that ship a bare manifest and no script; adding a
language is adding a row, not touching the mechanism.

MULTI-REPO IS THE SAME CODE PATH. A story's workspace is mounts under
one root; discovery walks each mount, and each runs its own setup with
its own cwd. One repo per story is simply the list of length one.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import tarfile

# ── HOW LONG A SETUP MAY TAKE, and it is ONE number for both rooms ─────
#
# THE CONTRACT ABOVE IS A PROMISE ABOUT TEXT, and it was only half kept:
# both rooms ran the same committed command and were given DIFFERENT
# amounts of time to finish it. The executor's init container had 1860s
# (`conductor/unscheduled.SETUP_BUDGET_S`); the Verifier's prepare Job
# had 900 (`prepare_volume.DEFAULT_TIMEOUT_S`) -- less than half, for the
# same installs. A repository whose dependencies took twenty minutes to
# install therefore WORKED for the executor and TIMED OUT in
# verification, which is precisely "the checks fail for a reason that is
# not the work" that this file says the contract exists to prevent.
#
# PER MOUNT, because that is how the work scales. `prepare_script` emits
# one `sh -c` that runs every mount's setup in sequence under `set -e`,
# so a five-repo story does five installs inside one budget. A single
# number was a per-story budget pretending to be a per-install one, and
# it got smaller the more repositories a story had.
#
# ONE ORIGIN. `limits.JOB_DEFAULT_S` is already "how long an install
# takes when nobody declared a number", chosen because "a cold
# `pnpm install` on a monorepo is minutes rather than seconds", and
# `unscheduled.SETUP_BUDGET_S` already derived from it. Restating 1860
# here would have been a third copy of one fact.
from kernel.bridge.limits import JOB_DEFAULT_S as _INSTALL_S

# The pull and the tree fetch that run ahead of the install, in both
# rooms. `unscheduled` has always added this for the image pull and the
# clone; the prepare Job pays the same before its first command.
OVERHEAD_S = 60.0

BUDGET_S = _INSTALL_S + OVERHEAD_S

# AND A CEILING, because a prepare holds a ReadWriteOncePod claim for its
# whole run and the warm pod cannot attach until it exits. Two hours
# matches `budget.MAX_S`, the longest a declared check may take -- the
# same reasoning applies to both: the contract is authored by the model
# whose work is being checked, so "the repository needs longer" is not
# by itself sufficient authority to hold a disk indefinitely.
CEILING_S = 7200.0


def budget_for(mount_count: int) -> float:
    """Seconds for one prepare Job covering `mount_count` mounts.

    Never below one mount's worth: a story whose discovery found nothing
    still pays for the pod start, and a floor keeps the arithmetic
    honest when the count is zero.
    """
    return min(CEILING_S, BUDGET_S * max(1, int(mount_count or 0)))


SETUP_FILE = ".agentos/setup.sh"
ENV_FILE = ".agentos/env"

# WHAT AN ESCAPE HATCH IS ASSUMED TO BUILD.
#
# A `.agentos/setup.sh` mount used to declare `bins: ()`, so the one
# mechanism for a repository with an unguessable layout was also the one
# that put nothing on PATH -- and the module's own docstring names the
# convention it then did not apply: the result is expected to "land
# INSIDE the tree (`.venv/`, `node_modules/`, `vendor/`). Every
# ecosystem has an in-tree mode."
#
# GUESSING IS SAFE HERE IN A WAY IT USUALLY IS NOT, because a PATH entry
# that does not exist is ignored by every shell. The cost of naming a
# directory the setup did not build is zero; the cost of naming none is
# that `pytest` resolves to the image's interpreter and the checks fail
# on the project's own dependencies. So this lists the conventions
# rather than probing for them, which also keeps it DERIVED, NOT
# DISCOVERED, like every other row.
#
# A repo whose layout is none of these still has `.agentos/env`, which
# the setup owns and this never overwrites.
SETUP_BINS = (".venv/bin", "node_modules/.bin", "vendor/bin",
              "target/debug", "bin")

# The courtesy registry: bare manifest -> generated setup. First match
# per mount wins; an explicit setup.sh beats them all. `bins` are the
# in-tree directories whose executables the checks may call bare
# (`pytest`, not `.venv/bin/pytest`).
# Every generated command starts by excluding what it is about to create
# from git, via .git/info/exclude -- repo-LOCAL, never committed, so it
# works on repos whose .gitignore never anticipated us. Without this,
# story_commit's `git add -A` would commit the .venv the setup just
# built: five hundred vendored files again, this time made by us.
_EXCLUDE = ("{{ [ -d .git ] && mkdir -p .git/info && "
            "echo {name} >> .git/info/exclude; true; }} && ")

# Telling the SANDBOX where the crates went, without touching a file the
# repository owns. `$TREE` is what `mount_env.parse_env` substitutes for
# this mount's root, so the line means the same thing in the sandbox and
# in the criterion container.
_CARGO_ENV = ("{ [ -f .agentos/env ] || { mkdir -p .agentos && "
              "printf 'CARGO_HOME=$TREE/.cargo' > .agentos/env; }; }")

REGISTRY = (
    {"manifest": "requirements.txt",
     "command": (_EXCLUDE.format(name=".venv/")
                 + "python3 -m venv .venv && "
                 ". .venv/bin/activate && "
                 "python3 -m pip install --no-input -q -r requirements.txt"),
     "bins": (".venv/bin",),
     # A LOCKFILE BEATS IT, the same rule the JS side already applies.
     # `_mount` returns EVERY match, so a repo carrying both
     # `requirements.txt` and `poetry.lock` would otherwise run two
     # installs into one tree -- the venv this builds and the one poetry
     # builds -- and whichever finished last would own `.venv/bin`.
     # These three are mutually exclusive in practice; the supersede is
     # what makes that a rule rather than a hope.
     "superseded_by": ("uv.lock", "poetry.lock", "Pipfile.lock")},
    {"manifest": "pnpm-lock.yaml",
     # corepack reads `packageManager` from package.json, so the version
     # the repo pinned is the version that installs -- openclaw pins
     # pnpm@10 and a workspace installed by npm simply breaks
     # (workspace: protocol). Node images ship corepack.
     "command": (_EXCLUDE.format(name="node_modules/")
                 + "corepack enable && "
                 "pnpm install --frozen-lockfile"),
     "bins": ("node_modules/.bin",)},
    {"manifest": "yarn.lock",
     "command": (_EXCLUDE.format(name="node_modules/")
                 + "corepack enable && "
                 "yarn install --immutable"),
     "bins": ("node_modules/.bin",)},
    {"manifest": "package-lock.json",
     "command": (_EXCLUDE.format(name="node_modules/")
                 + "npm ci --no-audit --no-fund"),
     "bins": ("node_modules/.bin",)},
    {"manifest": "package.json",
     "command": (_EXCLUDE.format(name="node_modules/")
                 + "npm install --no-audit --no-fund"),
     "bins": ("node_modules/.bin",),
     # A bare package.json resolves versions AT INSTALL TIME: the
     # sandbox at T0 and the Verifier at T1 can honestly build two
     # different environments. Supported, because refusing would strand
     # every lockless repo -- but said out loud in every report.
     "warn": "package.json without a lockfile: the install is not "
             "reproducible; commit package-lock.json",
     "superseded_by": ("pnpm-lock.yaml", "yarn.lock",
                       "package-lock.json")},

    # ── MODERN PYTHON, and why this block exists ─────────────────
    #
    # A repository whose dependencies live in `pyproject.toml` -- which
    # is most of them now -- matched nothing here. Discovery returned
    # `none`, the tree got no environment, and the first check failed on
    # an import with nothing saying why. `setups.py` already states the
    # intent: "the cheapest fix is a line in this registry", and these
    # are those lines.
    #
    # EACH BOOTSTRAPS ITS OWN TOOL WITH `pip`, deliberately. The sandbox
    # image ships python, pip, pytest, node and git and nothing else --
    # no uv, no poetry, no pipenv. A row naming a binary the image lacks
    # would fail as `command not found`, which reads as a broken repo
    # rather than a missing tool, and it would make this registry
    # silently depend on an image change shipping first. `pip install
    # uv` is a few seconds; the others are slower and still bounded.
    #
    # IN-PROJECT VENVS, set by ENVIRONMENT and never by config file.
    # `poetry config --local` and `pipenv --site-packages` write
    # `poetry.toml`/`Pipfile` changes INTO the tree, which
    # `story_commit`'s `git add -A` would then commit as the story's
    # work. The env vars do the same job and leave nothing behind.
    {"manifest": "uv.lock",
     "command": (_EXCLUDE.format(name=".venv/")
                 + "python3 -m pip install --no-input -q uv && "
                 "uv sync --frozen"),
     "bins": (".venv/bin",)},
    {"manifest": "poetry.lock",
     "command": (_EXCLUDE.format(name=".venv/")
                 + "python3 -m pip install --no-input -q poetry && "
                 "POETRY_VIRTUALENVS_IN_PROJECT=true "
                 "poetry install --no-interaction --no-ansi"),
     "bins": (".venv/bin",)},
    {"manifest": "Pipfile.lock",
     "command": (_EXCLUDE.format(name=".venv/")
                 + "python3 -m pip install --no-input -q pipenv && "
                 "PIPENV_VENV_IN_PROJECT=1 pipenv sync"),
     "bins": (".venv/bin",)},
    {"manifest": "pyproject.toml",
     # EDITABLE, and that is not a style preference. A story edits the
     # tree and then runs its checks against it; a non-editable install
     # copies the package into site-packages, so the checks would
     # exercise the copy made before the work and pass or fail on the
     # wrong code.
     #
     # AND THE DEV EXTRAS, because a story's CHECKS ARE TESTS and tests
     # need test dependencies. `-e .` alone installs the package and
     # not its `dev` group, so a repo whose pyproject sets
     # `asyncio_mode` or `timeout` -- both of which need plugins -- gets
     # an environment where pytest fails at CONFIG LOAD, before
     # collecting anything. Nothing tells the model why: `_line` reports
     # the manifest that matched, `-q` suppresses the output, and the
     # scout only fires when discovery found NOTHING. The person
     # watching concludes the platform is broken.
     #
     # THE NODE ROWS ALREADY DO THIS. `npm ci`, `pnpm install
     # --frozen-lockfile` and `yarn install --immutable` all install
     # devDependencies by default, so a Node story has had its test
     # tooling all along and a Python story has not. This is not a new
     # policy, it is the Python rows catching up to an inconsistency
     # nobody had noticed.
     #
     # THREE NAMES, UNCONDITIONALLY, AND NO MANIFEST PARSING. pip warns
     # and exits 0 for an extra a project does not declare --
     # "WARNING: <pkg> does not provide the extra 'dev'" -- so all three
     # common spellings can be asked for and the absent ones cost a
     # warning. That keeps this file what it is: filename matching and
     # command strings, never learning what pip is.
     "command": (_EXCLUDE.format(name=".venv/")
                 + "python3 -m venv .venv && "
                 ". .venv/bin/activate && "
                 "python3 -m pip install --no-input -q -e "
                 "'.[dev,test,tests]'"),
     "bins": (".venv/bin",),
     # Same honesty the bare `package.json` gets: no lockfile means the
     # resolver runs twice and may answer twice.
     "warn": "pyproject.toml without a lockfile: the install is not "
             "reproducible; commit uv.lock, poetry.lock or "
             "requirements.txt",
     # LAST OF THE PYTHON ROWS. Every other Python manifest is more
     # specific about what to install than a bare `pyproject.toml`, so
     # each of them beats it.
     "superseded_by": ("requirements.txt", "uv.lock", "poetry.lock",
                       "Pipfile.lock")},

    # -- RUST, and why B6b is an allowlist change in disguise --------
    #
    # This registry recognised Python and Node and nothing else, so a
    # Rust repository discovered `none`, got no environment, and the
    # first check failed on a missing crate with nothing saying why.
    # Whoever hits that reaches for the EGRESS ALLOWLIST, because a
    # missing dependency looks like a blocked host -- and crates.io is
    # already permitted. A row here is what stops the allowlist growing
    # for a problem it was never the cause of.
    #
    # THE IMAGE CARRIES cargo NOW, which is what makes these runnable:
    # B3 put `rustc cargo` in the image and it stayed when the image
    # returned to Python 3.12 -- partly FOR these rows, which would
    # otherwise name a binary the image lacks. A row naming a binary the image
    # lacks would fail as `command not found`, which reads as a broken
    # repo -- the reason every Python row above bootstraps with pip.
    #
    # CARGO_HOME IS IN THE TREE, and that is not tidiness. The default
    # is `$HOME/.cargo`, HOME is `/tmp`, and /tmp is the CONTAINER's own
    # layer -- so a cache the setup container fills is a cache the
    # sandbox never sees, and every story would fetch its whole
    # dependency graph twice. The tree is the one thing both containers
    # share, which is why `.venv` works and why this follows it.
    #
    # `.agentos/env` IS ONLY WRITTEN IF THE REPO HAS NONE. It is a
    # repository's file, allowlisted for exactly this
    # ("where do my packages live", and CARGO_HOME is already in
    # ALLOWED_ENV_KEYS) -- but overwriting one a repo committed would be
    # the registry editing the story's work.
    {"manifest": "Cargo.lock",
     "command": (_EXCLUDE.format(name="target/")
                 + _EXCLUDE.format(name=".cargo/")
                 + "export CARGO_HOME=$PWD/.cargo && "
                 "cargo fetch --locked && "
                 + _CARGO_ENV),
     "bins": ("target/debug",)},
    {"manifest": "Cargo.toml",
     "command": (_EXCLUDE.format(name="target/")
                 + _EXCLUDE.format(name=".cargo/")
                 + "export CARGO_HOME=$PWD/.cargo && "
                 "cargo fetch && "
                 + _CARGO_ENV),
     "bins": ("target/debug",),
     # Same honesty the bare `package.json` and `pyproject.toml` get.
     "warn": "Cargo.toml without a lockfile: the resolver runs twice "
             "and may answer twice; commit Cargo.lock",
     "superseded_by": ("Cargo.lock",)},
)

# What one MOUNT's build is allowed to weigh (env-tunable). Beyond this
# the dependencies belong in the contract's base image, and the error
# says so. Per mount, not per tree -- a gateway monorepo's node_modules
# is legitimately heavy and should not budget against its siblings.
import os as _os

MAX_PREPARED_BYTES = int(
    _os.environ["AGENTOS_MAX_PREPARED_MB"]
    if "AGENTOS_MAX_PREPARED_MB" in _os.environ else "1400") * 1024 * 1024


def _mount(at: str, read) -> list[dict]:
    """EVERY setup for one mount. `read(relpath) -> bytes | None`.

    An explicit script wins ALONE -- the repo's own code is the whole
    definition. Otherwise every matching manifest contributes: a
    fullstack repo with requirements.txt AND package.json needs both
    installs, and the first-match-only version quietly skipped the
    frontend. A manifest is skipped when its `superseded_by` is present
    (a lockfile beats the bare package.json it locks).
    """
    explicit = read(posixpath.join(at, SETUP_FILE) if at else SETUP_FILE)
    if explicit is not None:
        return [{"at": at, "source": SETUP_FILE,
                 "command": f"sh {SETUP_FILE}",
                 "bins": SETUP_BINS, "content": explicit}]
    present = {}
    for row in REGISTRY:
        blob = read(posixpath.join(at, row["manifest"]) if at
                    else row["manifest"])
        if blob is not None:
            present[row["manifest"]] = (row, blob)
    out = []
    for manifest, (row, blob) in present.items():
        over = row.get("superseded_by") or ()
        if isinstance(over, str):
            over = (over,)
        if any(o in present for o in over):
            continue
        entry = {"at": at, "source": manifest, "command": row["command"],
                 "bins": row["bins"], "content": blob}
        if row.get("warn"):
            entry["warn"] = row["warn"]
        out.append(entry)
    return out


def _discover(ats, read) -> list[dict]:
    out = []
    for at in sorted(set(ats)):
        out.extend(_mount(at or "", read))
    return out


def from_tar(tar_bytes: bytes, ats, prefix: str = "verify/") -> list[dict]:
    """Discovery against the COMMITTED tree — the tar the Verifier holds.

    From the archive and never the working copy, for the same reason the
    Verifier checks out the commit: an environment the executor did not
    commit is an environment the team does not have.
    """
    members: dict[str, bytes] = {}
    import io
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        wanted = set()
        for at in set(ats):
            base = posixpath.join(prefix + (at or ""), "")
            wanted.add(posixpath.normpath(base + SETUP_FILE))
            for row in REGISTRY:
                wanted.add(posixpath.normpath(base + row["manifest"]))
        for m in tf:
            name = posixpath.normpath(m.name)
            if name in wanted and m.isfile():
                members[name] = tf.extractfile(m).read()

    def read(rel: str):
        return members.get(posixpath.normpath(prefix + rel))

    return _discover(ats, read)


def from_dir(root: str, ats) -> list[dict]:
    """Discovery against a live workspace — the executor's sandbox side."""
    def read(rel: str):
        path = os.path.join(root, *rel.split("/"))
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    return _discover(ats, read)


def from_pod(host: str, root: str, ats, *, execution_id: int) -> list[dict]:
    """Discovery against a workspace that is inside an execution's pod.

    THE CONDUCTOR CANNOT OPEN IT. `from_dir` above reads the tree with
    `open()`, which is right on Docker -- the conductor composed the
    workspace into its own filesystem -- and reads the CONDUCTOR's disk
    on Kubernetes, where the claim is ReadWriteOncePod and belongs to
    another pod entirely. The path it was handed, `/workspace`, does not
    exist there, so every manifest came back missing and every
    execution on a pod silently ran with no environment built: no
    `pip install`, no `npm ci`, and a test command that fails for
    reasons that have nothing to do with the work.

    SAME `_discover`, DIFFERENT READER, which is why this is six lines.
    The discovery rules -- which files count, in what order, per mount
    -- stay in one place; only "how do I get the bytes of a file"
    differs by substrate, and that is exactly the seam podfs exists to
    be.
    """
    from kernel.workspace import podfs_client                # noqa: PLC0415

    def read(rel: str):
        data, problem = podfs_client.read_file(host, root, rel,
                                               execution_id=execution_id)
        return None if problem else data

    return _discover(ats, read)


def fingerprint(image: str, setups: list[dict]) -> str:
    """What the prepared environment depends on, and nothing else.

    The base image, and each mount's setup text and manifest bytes. Two
    claims with the same fingerprint get the same environment, which is
    what makes the prepare cacheable without trusting the cache.
    """
    h = hashlib.sha256()
    h.update(image.encode())
    for s in setups:
        for part in (s["at"], s["source"], s["command"]):
            h.update(b"\x00" + part.encode())
        h.update(b"\x00" + s["content"])
    return h.hexdigest()
