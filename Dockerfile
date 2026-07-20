# syntax=docker/dockerfile:1

# ── Stage 1: Builder ─────────────────────────────────────────────────────────
# Installs build-only system deps (gcc, libffi-dev) and Deno. None of these
# tools land in the final runtime image — only compiled artifacts are carried
# forward. The pip cache mount persists across builds so unchanged packages are
# never re-downloaded.
FROM python:3.12-slim AS builder

WORKDIR /app

# Build-only: C-extension compilers + Deno installer requirements.
# gcc / libffi-dev compile asyncpg, cryptography, and other C-extension wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno for the TypeScript-tier custom tools runtime.
# Only the binary is copied to the runtime stage — the installer tarball stays here.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Dependency layer — only invalidated when requirements.txt changes.
# BuildKit cache mount avoids re-downloading packages on subsequent builds.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# App source + pre-compiled bytecode.
# compileall moves .pyc generation to build time, reducing Cloud Run cold-start
# latency (no filesystem writes needed during startup).
COPY . .
RUN python -m compileall -q app/

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
# Clean python:3.12-slim with no build tooling. Final image contains only what
# the app needs to run.
FROM python:3.12-slim

WORKDIR /app

# git is required at runtime by gitingest for repository summarization.
# All other build-time system deps are intentionally excluded here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Deno binary only — no installer, no tarballs, no curl.
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

# uvicorn console script — required by Cloud Run services that override CMD
# with --command=uvicorn (council-worker, git-engine, management).
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Compiled Python packages from the builder stage.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages

# App source including pre-compiled .pyc bytecode cache.
COPY --from=builder /app .

# Deno cache dir must be writable by the non-root Cloud Run user (uid 1000).
ENV DENO_DIR=/tmp/deno-cache

# Force line-buffered stdout/stderr so Cloud Run's log collector receives log
# lines immediately rather than holding them in Python's block buffer.
ENV PYTHONUNBUFFERED=1

# Cloud Run expects PORT env var.
ENV PORT=8080

EXPOSE 8080

# python -m uvicorn avoids depending on the /usr/local/bin/uvicorn console
# script, which lives in the builder stage and is not copied here.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
