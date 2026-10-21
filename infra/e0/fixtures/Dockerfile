# syntax=docker/dockerfile:1
#
# G0 · the image the Verifier runs criteria in — and the image the
# sandbox is built from too. `cloudbuild-next.yaml` builds THIS FILE as
# the production sandbox base; E0's preflight builds it locally. One
# file, so the two rooms cannot drift.
#
# THIS FILE DID NOT EXIST UNTIL STAGE G. `agentos-kernel/fixture:dev`
# was built by hand on 19 August 2026 and lived only in one laptop's
# Docker cache. Six suites depend on it — F4's reward-hacking traps,
# F7's real-repository run, the default contract `amend` gives a story
# that never had one, and the E0 suites around them — so losing the
# cache meant losing the ability to run them AND the ability to rebuild
# what they ran against. Recovered by reading `docker history`.
#
# WHY IT IS NOT python:3.x-slim. It nearly is: slim plus pytest. But the
# criteria run HERE, and slim has no test runner — which is how F4's
# first pass produced six false greens, every attack "failing closed"
# having checked nothing at all. An image that cannot run the check is
# indistinguishable from a check that passes.
#
# ── PINNED BY DIGEST ────────────────────────────────────────────────────
#
# Two verifications of the same contract must see the same thing, and a
# `python:` or `node:` tag is a moving target — during an earlier
# increment the local copy and the one the fixture was built from were
# already different builds of the same version. Contracts carry
# `base_digest` for exactly this reason; this is the same argument one
# level down.
#
# BOTH STAGES ARE THE SAME DEBIAN RELEASE, which is the half of the pin
# that used to be silently missing: the unsuffixed `python:3.X-slim` tag
# has historically defaulted to bookworm, so a silent move would leave
# `COPY --from=node` bringing a binary linked against a newer glibc than
# the base provides. The digests below are trixie on both sides, and the
# tag each came from is in the comment so the next person knows what to
# pull.
#
# THE DIGEST IS THE MANIFEST LIST's, not one architecture's -- which is
# what `docker image inspect --format '{{index .RepoDigests 0}}'`
# returns after a pull. Multi-arch keeps working and the content cannot
# move underneath it.
#
# To move either one: pull the tag, take its RepoDigest, paste it here,
# rebuild with AGENTOS_REBUILD_FIXTURE=1 (see preflight.sh -- without it
# you test the OLD image and go green), and RUN THE SUITES. A check that
# behaves differently under a new base is a finding, not a nuisance, and
# F4 is the suite to watch on a base or pytest move.

# node:24-trixie-slim, resolved 2026-09-21 (tag last pushed 2026-09-19)
FROM node@sha256:8ec5d7557396cfe32d21c3f9c13072355ceab22b584578ca4bb28af31120cffe AS node

#
# 3.12 AND NOT 3.14, REVERSED ON 2026-09-22 AFTER MEASURING. The move to
# 3.14 was argued from "3.12 is security-only", which is a good reason for
# a project we control and the wrong one for THE IMAGE AN ARBITRARY REPO IS
# HANDED. What matters here is the breadth of the wheel ecosystem, not
# whether the interpreter still takes bugfixes.
#
# MEASURED against PyPI, resolving each pin for cp312/cp313/cp314:
#
#     pgserver        cp312 only   (no sdist -- embedded Postgres for tests)
#     pomdp-py        cp312 only   (last release March 2024)
#     numpy <2.3      cp313 max    (3.14 wheels start at 2.3)
#     pyarrow <19     cp313 max
#     torch, asyncpg, duckdb, matplotlib   all three
#
# The long tail is the problem, not the flagships: torch -- historically
# the laggard -- ships 3.14 wheels, while small and unmaintained packages
# do not, and those are exactly what an arbitrary repo drags in.
#
# AND THE FAILURE IS NOT CLEAN. A missing wheel does not refuse, it builds
# from sdist, spends the setup budget and surfaces as "the checks fail for
# a reason that is not the work" -- the misdiagnosis `setups.py` exists to
# prevent. A goal that wants a newer interpreter names one in
# `sandbox_base_image`; that is what per-goal images are for.
#
# python:3.12-slim-trixie, resolved 2026-09-22 (tag last pushed 2026-09-19)
FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# ── pytest ──────────────────────────────────────────────────────────────
#
# Pinned to all three components, because the recipe check requires
# three and because a criterion that passes under one pytest and fails
# under the next has told you about pytest, not about the work.
#
# F4 IS THE SUITE TO RUN ON A BUMP, not merely the red-test risk. Its
# reward-hacking traps use `addopts` manipulation AS AN ATTACK, so a
# change in ini-file precedence can flip a security finding rather than
# a colour. A green F4 after a pytest bump is the evidence; the version
# number is not.
RUN pip install --no-cache-dir pytest==8.4.1

# The node runtime, npm and corepack, copied from the pinned stage.
# `corepack enable` writes the pnpm/yarn shims INTO THIS IMAGE so the
# offline criteria containers have them on disk; the actual package-
# manager fetch happens in the networked prepare step. git because
# `npm ci` reads git-hosted dependencies and setup scripts run here.
#
# NO TRAILING `|| true`. It was left-associative over the whole RUN
# chain, so a failed `apt-get install git` still exited 0 and the image
# shipped without git. That matters more now, because `pnpm --version`
# is the only evidence the corepack step worked at all.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && corepack enable \
 && apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version && pnpm --version

# ── the package managers, CACHED and not overridden ─────────────────────
#
# THE REASON IS EGRESS, NOT AVAILABILITY. `corepack enable` only writes
# shims; the fetch happens when `pnpm install` downloads the exact
# version the repo pinned in `packageManager`. Caching the common
# versions here turns that download into a cache hit for most repos.
#
# WHAT IS DELIBERATELY NOT DONE. A baked pnpm still self-manages
# versions unless `manage-package-manager-versions=false`, and setting
# that would OVERRIDE the version the repo pinned — trading a network
# fetch for running the wrong package manager. And there is no single
# baked yarn that is right, because `--immutable` is Berry and Classic
# wants `--frozen-lockfile`. Corepack is what resolves that per repo, so
# corepack stays the mechanism and these are only a warm cache.
RUN corepack prepare pnpm@10.11.0 --activate \
 && corepack prepare yarn@4.9.1 --activate \
 && corepack prepare yarn@1.22.22 --activate \
 && corepack enable

# ── WHAT A REAL REPOSITORY NEEDS, and why each group is here ────────
#
# The image was python + pytest + node + git. That runs a check and
# cannot BUILD one: a dependency with no prebuilt wheel died at
# `gcc: command not found`, and with `apt` refused in the sandbox (it
# needs root, and the container is discarded anyway) a story had no way
# forward but a human round trip. A system package belongs here, in the
# image, where it survives the tree being rebuilt between rounds.
#
# MEASURED, on the earlier base: 190MB -> 900MB. The compiler is 419MB
# of the 710; everything else together is 291MB. That is the trade,
# taken deliberately -- Image Streaming is on, so the cost is registry
# and build time rather than pod start. Rust below adds to it again.
#
# ONE apt LAYER, cleaned in the same RUN, because a `rm` in a later
# layer removes nothing from the image.
#
# NOT BIT-REPRODUCIBLE, said out loud because this file argues hard for
# digest pinning. The FROM lines are pinned; apt is not. Reproducibility
# is preserved where it is consumed -- `AGENTOS_SANDBOX_BASE_DIGEST`
# pins what any one contract sees -- not at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    `# the toolchain: gcc, g++, make, and the thing that finds libs` \
    build-essential pkg-config \
    `# RUST, AND IT STAYS AFTER THE RETURN TO 3.12. It went in for` \
    `# 3.14's Rust-backed wheels -- pydantic-core, cryptography,` \
    `# uuid-utils -- and that reason left with 3.14. Two others did` \
    `# not: setups.REGISTRY now recognises Cargo.lock and Cargo.toml,` \
    `# so a Rust repository gets an environment rather than none,` \
    `# and .crates.io has been on the egress allowlist all along with` \
    `# nothing able to reach it. A row naming a binary the image` \
    `# lacks fails as "command not found", read as a broken repo.` \
    rustc cargo \
    `# headers for the wheels that are not always wheels --` \
    `# psycopg2, cryptography, lxml, Pillow` \
    libpq-dev libssl-dev libffi-dev libxml2-dev libxslt1-dev \
    zlib1g-dev libjpeg-dev \
    `# fetching: git over ssh, and the CA bundle everything needs` \
    curl wget ca-certificates openssh-client \
    `# large files in a repo are a git-lfs pointer without this` \
    git-lfs \
    `# reading what a command returned, and what is in an archive` \
    unzip xz-utils jq less procps file \
    `# inspecting the database a story is working against` \
    postgresql-client sqlite3 \
 && rm -rf /var/lib/apt/lists/* \
 && rustc --version && cargo --version

# `uv` resolves and installs an order of magnitude faster than pip, and
# `setups.REGISTRY` reaches for it whenever a repo commits a `uv.lock`.
# Shipping it here turns that row's `pip install uv` into a no-op rather
# than a download on every single story.
RUN pip install --no-cache-dir uv

# ── an identity for uid 65532 ───────────────────────────────────────────
#
# The pod runs as 65532 and there is no passwd entry for it, so
# `pwd.getpwuid(os.getuid())` raises KeyError and everything built on it
# FAILS rather than degrading: `getpass.getuser()`, git's author
# auto-detection, some npm and ssh paths, and a shell that prints
# `I have no name!`.
#
# THE HOME FIELD IS /tmp, NOT /home/nonroot. That directory would not
# exist in this image and would not be writable by 65532 if it did. The
# passwd home is only consulted when $HOME is unset -- which every
# container avoids by setting HOME=/tmp -- but a fallback reachable only
# in the untested case must give the SAME answer as the explicit value,
# not a second broken one.
#
# It adds an IDENTITY and not a home: no `ENV HOME` below, for the
# reason stated there.
RUN echo 'nonroot:x:65532:65532:nonroot:/tmp:/usr/sbin/nologin' >> /etc/passwd \
 && echo 'nonroot:x:65532:' >> /etc/group \
 && python -c "import pwd; pwd.getpwuid(65532)"

# UTF-8, because the default is POSIX/C and a test whose fixture holds a
# non-ASCII character then fails on an encode rather than on its logic.
# `C.UTF-8` needs no `locales` package and no generation step.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8

# NO `ENV HOME` HERE, deliberately, and this is the second time it has
# been proposed. This image is the sandbox AND the Verifier's prepare
# container; giving it a HOME would give those two different homes than
# the setup container, whose caches `mount_env` places explicitly. HOME
# belongs on the container, set by `k8s_workspace`, not on the image
# every room shares.

# `verify/` is where `archive_committed_tree` unpacks a contract's tree,
# and /repo is where the sandbox's workspace is bound. Both exist so a
# criterion's relative paths resolve the same in either role.
WORKDIR /repo

CMD ["pytest", "-q"]
