# syntax=docker/dockerfile:1

# ── Stage 0: Base ────────────────────────────────────────────────────────────
# The system packages that BOTH the runtime and the test image need, declared
# once so they cannot drift apart.
#
# git earns its place twice: gitingest shells out to it at runtime for
# repository summarization, and the test suite runs a real `git init` and
# `git clone` to assert that the gateway bearer never lands in a workspace's
# .git/config. That test is the end-to-end half of a credential-leak guard.
#
# It was previously installed only in the runtime stage. The test stage
# inherits the builder, so the suite ran in an image with no git and the
# clone test failed with FileNotFoundError -- a real check reporting a fake
# defect, which is the fastest way to get a check deleted.
FROM python:3.12-slim AS base

WORKDIR /app

# curl earns its place at runtime, not only at build time. The git
# credential helper (`kernel/workspace/bindings.py::_credential_helper`)
# is a shell function that curls the control panel's token broker, and
# git runs it inside the EXECUTION POD -- which runs this image. curl was
# installed in the builder stage only, to fetch Deno, and the final stage
# carried the Deno binary across with "no curl"; so every clone of a
# private repository in a pod died with
#
#     f() { ...curl... }; f get: 1: curl: not found
#     fatal: could not read Username for 'https://github.com'
#
# where the second line is the one everybody reads and describes a
# terminal that was never involved.
#
# Declared HERE, in base, so the runtime and test images cannot disagree
# about it -- the same reason git is here, recorded above.
#
# `git-lfs` beside it, and it belongs to the CLONE rather than to the
# work. `workspace-compose` runs in THIS image and clones the story's
# repositories; a repo using LFS cloned without it gets pointer files --
# a 130-byte text stub where a model expects a binary -- and nothing
# fails. The story reads the stub, concludes the asset is corrupt, and
# is right about the bytes and wrong about why. The sandbox image
# carries it too, for an asset fetched mid-leg.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    git-lfs \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 1: Builder ─────────────────────────────────────────────────────────
# Installs build-only system deps (gcc, libffi-dev) and Deno. None of these
# tools land in the final runtime image — only compiled artifacts are carried
# forward. The pip cache mount persists across builds so unchanged packages are
# never re-downloaded.
FROM base AS builder

WORKDIR /app

# Build-only: C-extension compilers + Deno installer requirements.
# gcc / libffi-dev compile asyncpg, cryptography, and other C-extension wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno for the TypeScript-tier custom tools runtime.
# Only the binary is copied to the runtime stage — the installer tarball stays here.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# ── B2: the interpreter that travels into the sandbox ────────────────────────
#
# THE SANDBOX RUNS THE CONTRACT'S IMAGE, which may have no python at all --
# a Go image, a Rust image -- and there is nothing to refuse on before it
# runs: the Docker socket proxy runs without IMAGES=1 and preflight says in
# as many words not to add it, and on Kubernetes there is no registry client.
# All that exists is the image's NAME. So the interpreter is delivered the
# way the shim already is, by the initContainer, and the invariant holds by
# construction for any image.
#
# A RELOCATABLE BUILD, NOT THIS IMAGE'S OWN PYTHON. /usr/local/bin/python3
# here is dynamically linked against this base's glibc, libssl and libffi;
# copied into an arbitrary image it finds a different set or none, and the
# failure arrives as a sandbox that starts and never answers -- the exact
# shape this step exists to remove. python-build-standalone's `install_only`
# build carries its own libssl and libffi and runs from any prefix.
#
# WHAT IT STILL CANNOT DO, recorded so nobody reads more into it: it is
# linked against glibc, so a musl image (Alpine without gcompat) falls back
# to the image's own `python3`. That is a narrower gap than "any image
# without python", not no gap.
#
# PINNED TO A RELEASE, and the digest is deliberately absent rather than
# invented: this is fetched at build time over TLS from a tag that does not
# move, and a wrong hash pasted from memory fails the build for a reason
# that is not the work.
ARG PYSTANDALONE_TAG=20241219
ARG PYSTANDALONE_VERSION=3.12.8
# The last line is the PROOF, and it is the last statement of the chain so
# a failure fails the build: a relocatable build that is not, in this
# release, for this arch, is a build failure here and a dead sandbox three
# weeks later.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) triple=x86_64-unknown-linux-gnu ;; \
      arm64) triple=aarch64-unknown-linux-gnu ;; \
      *) echo "no standalone python for $arch" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/astral-sh/python-build-standalone/releases/download"; \
    name="cpython-${PYSTANDALONE_VERSION}+${PYSTANDALONE_TAG}-${triple}-install_only.tar.gz"; \
    curl -fsSL "${base}/${PYSTANDALONE_TAG}/${name}" -o /tmp/pystandalone.tar.gz; \
    mkdir -p /opt/agentos-python; \
    tar -xzf /tmp/pystandalone.tar.gz -C /opt; \
    mv /opt/python/* /opt/agentos-python/; \
    rmdir /opt/python; \
    rm -f /tmp/pystandalone.tar.gz; \
    /opt/agentos-python/bin/python3 -c "import ssl, json; print(1)"

# Dependency layer — only invalidated when requirements.txt changes.
# BuildKit cache mount avoids re-downloading packages on subsequent builds.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# App source + pre-compiled bytecode.
# compileall moves .pyc generation to build time, reducing Cloud Run cold-start
# latency (no filesystem writes needed during startup).
COPY . .
RUN pip install --no-deps -e packages/agentos-ledger-client && python -m compileall -q app/

# ── Stage 1b: Test ───────────────────────────────────────────────────────────
# The suite's dependencies, on top of the builder's. Deliberately placed
# BEFORE the runtime stage: `docker build .` with no --target builds the
# LAST stage in the file, so the plain build keeps producing the runtime
# image and this one is only built when asked for by name.
#
# It inherits `builder`, so the packages the app actually runs with are
# the packages the tests run against. That parity is the point -- the
# defect that motivated this was python-multipart being present in
# requirements.txt and absent from a developer's interpreter, which
# presents as a test-collection error and reads like a test problem.
#
# The stage carries NO tests: .dockerignore excludes `tests/`, `test_*`
# and `*_test.py` from the build context so the production image stays
# clean, and that exclusion applies to every stage. CI mounts the source
# over it at run time instead -- see the `test-suite` step in
# cloudbuild.yaml, and scripts/ci/run_suite.sh for what it runs.
#
# To reproduce CI locally:
#   docker build --target test -t agentos-test .
#   docker run --rm -v "$PWD:/src" -w /src -e INTERLOCK_TEST_DSN=... \
#     agentos-test bash scripts/ci/run_suite.sh
FROM builder AS test

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
# The base (python:3.12-slim + git + curl) with no build tooling. Final
# image contains only what the app needs to run. Last stage in the file, so a plain
# `docker build .` still produces this one and not the test stage above.
FROM base

WORKDIR /app

# Deno binary only — no installer, no tarballs. curl now comes from base
# (see the note there): the git credential helper shells out to it at
# RUN time inside the execution pod, so excluding it here excluded it
# from the one place it was needed.
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

# uvicorn console script — required by Cloud Run services that override CMD
# with --command=uvicorn (council-worker, git-engine, management).
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Compiled Python packages from the builder stage.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages

# App source including pre-compiled .pyc bytecode cache.
COPY --from=builder /app .

# B2 — the interpreter the initContainer ships into the sandbox beside the
# bridge. `kernel/drivers/interpreter.py` stats this exact path to decide
# whether there is one to ship, so the conductor's answer and the copy's
# answer cannot differ: they are the same image.
COPY --from=builder /opt/agentos-python /opt/agentos-python

# Deno cache dir must be writable by the non-root Cloud Run user (uid 1000).
ENV DENO_DIR=/tmp/deno-cache

# Force line-buffered stdout/stderr so Cloud Run's log collector receives log
# lines immediately rather than holding them in Python's block buffer.
ENV PYTHONUNBUFFERED=1

# Cloud Run expects PORT env var.
ENV PORT=8080

# BUILD-OWNED VALUES, BAKED IN. A deploy that sets these with
# `--update-env-vars` loses them the next time terraform applies the
# service, because terraform owns the env list and does not know them.
# In the image they survive that. Empty (the default) reads as unset, and
# a deploy-time env var still overrides them.
ARG PLATFORM_BUILD_SHA=""
ARG AGENTOS_SANDBOX_BASE_IMAGE=""
ENV PLATFORM_BUILD_SHA=${PLATFORM_BUILD_SHA} \
    AGENTOS_SANDBOX_BASE_IMAGE=${AGENTOS_SANDBOX_BASE_IMAGE}

EXPOSE 8080

# python -m uvicorn avoids depending on the /usr/local/bin/uvicorn console
# script, which lives in the builder stage and is not copied here.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
