"""The image a story's work and its checks run in. One declaration.

WHAT WENT WRONG ON NCAL3, and it was one fact wearing two faces.

`agentos-kernel/fixture:dev` was the DEFAULT for a contract minted here
and in `kernel/console/contract.py`, spelled out as a literal in both.
It is an excellent image -- python 3.12, pytest, node, npm, pnpm, git,
every layer digest-pinned -- and `infra/e0/fixtures/Dockerfile` says
exactly why it is not `python:3.12-slim`: "an image that cannot run the
check is indistinguishable from a check that passes."

It was also built by hand on one laptop on 19 August 2026 and no build
pipeline has ever pushed it anywhere. So in the cloud the name resolves
to `docker.io/agentos-kernel/fixture:dev`, which does not exist:

  exec-190  sandbox ImagePullBackOff -> pod never ready -> dispatch
            times out at 120s -> reconcile recovers -> re-dispatch.
            Seven pods in ninety minutes.

  exec-189  no contract image at all, so it fell through to the
            platform default `python:3.12-slim`, ran, and then parked
            to ask a person why `node --test` was not on PATH.

One story asked for the image and could not have it; the other was not
given it and walked into the exact gap it exists to close.

A FACT ONLY THE LOCAL ENVIRONMENT DECLARES IS NOT A FACT. The name now
comes from the deployment, which is the only thing that knows what its
registry holds; `E0_FIXTURE` stays as the fallback because on E0 the
laptop's Docker cache IS the registry and the literal is correct there.

`os.environ[...] if ... in os.environ` rather than `.get(name, default)`:
B1's rule for `kernel/`, so a default is always visible at the point it
is chosen.
"""

from __future__ import annotations

import os

# E0's value, and only E0's. Built by `infra/e0/preflight.sh` into the
# local Docker cache, which is E0's registry.
E0_FIXTURE = "agentos-kernel/fixture:dev"

ENV_VAR = "AGENTOS_SANDBOX_BASE_IMAGE"


def default_base_image() -> str:
    """The base image a contract gets when nothing else chose one.

    Read per call, not at import: the conductor and the worker are
    separate processes in the cloud and a value frozen at import time in
    one of them is a value the other cannot correct.
    """
    if ENV_VAR in os.environ:
        named = os.environ[ENV_VAR].strip()
        if named:
            return named
    return E0_FIXTURE


async def for_goal(conn, goal_id: str) -> str:
    """The base image THIS GOAL's contracts start from.

    THE THIRD RUNG OF A LADDER THAT ALREADY EXISTED, and the only one
    that had no way up. `kernel/verifier/setups.py` gives a repository
    two ways to declare its own environment, both committed and both run
    identically in the sandbox and in the Verifier's prepare container:

      a recognised manifest  requirements.txt, pnpm-lock.yaml,
                             package-lock.json, package.json, yarn.lock
                             -- installed IN-TREE (.venv/, node_modules/)

      .agentos/setup.sh      arbitrary shell, cwd at the mount, language
                             -blind by construction: "the kernel never
                             learns what pip or npm are"

    Both stop at the same wall, and `setups.py` names it: "Beyond this
    the dependencies belong in the contract's base image." The sandbox
    runs `runAsNonRoot` as uid 65532 with every capability dropped, so
    nothing a repository commits can `apt-get install` a compiler, a
    database client, a JVM or a Go toolchain. That is the right ceiling
    -- and until now the only way past it was to edit a literal in the
    kernel and redeploy, which is not a way past it at all.

    A goal that works on a Rust repository names a Rust image here once,
    and every contract it mints starts there. Contracts still carry
    their own `base_image`/`base_digest` and still win: this is the
    value a new contract INHERITS, not an override of one.
    """
    named = await conn.fetchval(
        "SELECT NULLIF(btrim(coalesce(sandbox_base_image, '')), '') "
        "FROM goals WHERE id = $1", goal_id)
    return named or default_base_image()


# ── WHAT IS IN THAT IMAGE, in the words the model reads ─────────────────
#
# ONE TEXT PER IMAGE, TWO READERS, and it was one constant until B7.
#
# `for_goal` above exists so a goal can name its own image -- "a goal
# that works on a Rust repository names a Rust image here once" -- and a
# single constant means the FIRST goal to do that gets an executor told
# about a machine it is not running on. `kernel/env_text.py` holds the
# texts and picks between them; `ENVIRONMENT` below stays as the name
# for OURS, which is what every existing reference means.
#
# The reason the text exists at all is that there used to be
# one reader and it was wrong. The only description of the environment
# anywhere was a sentence in `worker/scout.py` -- "Available in the
# container: python3+pip+venv, node 22, npm, corepack, git" -- and the
# EXECUTOR was told nothing at all. So a model could not know it had a
# compiler, and avoided a source build it could have done; could not
# know `apt` was refused, and spent a leg discovering it; and the scout
# drafted setup scripts against an image it was describing from memory.
#
# It lives beside `default_base_image` so that whoever changes which
# image is used is already in the file that has to change with it, and
# `test_a_repo_we_have_never_seen_gets_an_environment` pins the claims
# here against `infra/e0/fixtures/Dockerfile` so the two cannot drift.
#
# SHORT ON PURPOSE. This goes in the STABLE PREFIX of every request --
# `profiles/prompt.py` warns that anything time-varying there turns
# every step into a cache miss, and anything merely long is paid for on
# every step of every leg. It names what changes a decision and nothing
# else; it is not an inventory.
# THE TEXTS LIVE IN `env_text`, one per image family, and
# `environment_text()` picks by the image the conductor resolved at
# dispatch. This name is kept for OURS because that is what every
# existing reference to `ENVIRONMENT` means.
from kernel.env_text import (AGENTOS as ENVIRONMENT,  # noqa: E402
                             environment_text, text_for)

__all__ = ["E0_FIXTURE", "ENV_VAR", "ENVIRONMENT", "default_base_image",
           "environment_text", "for_goal", "text_for"]
