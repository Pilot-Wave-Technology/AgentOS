"""git-engine worker — the standalone single-instance service that runs ONLY the
GO Git Dependency-Graph Manager engine loop (plan §5.0).

Deployed as its own Cloud Run service (git-engine-*) with `min=max=1`, so the
engine is the single writer for git topology and never shares the web tier's
request budget. It:
  - opens its own DB pool (init_db),
  - starts the GitGraphController, which acquires the single global
    `gitgraph:engine-leader` advisory lock (only the leader runs lanes — this
    survives the brief two-instance window during a revision rollout), LISTENs on
    `git_dispatch`, and (once GITGRAPH_ENABLED + lane work land) drives the
    per-repo merge-queue lanes.

It deliberately does NOT mount the web routers. The web tier keeps the HTTP +
webhook endpoints (they write rows + `NOTIFY git_dispatch`); this worker reads/
writes the same tables. The /health route exists only so Cloud Run has a port to
probe; the real work happens in the lifespan background tasks.

Run: `uvicorn app.git_engine_worker:app` (the Cloud Run deploy overrides the
image's default `app.main:app` command). Mirrors `app/council_worker.py`.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Configure logging BEFORE importing app modules so their INFO logs (DB pool,
# leader election, LISTEN) reach Cloud Run stdout — mirrors app/main.py.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

from app.database import init_db, close_db
from app.services.git_graph.engine import git_graph_controller

_log = logging.getLogger("app.git_engine_worker")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    git_graph_controller.start()
    _log.info("git-engine-worker: started (engine-leader election + git_dispatch listener)")
    try:
        yield
    finally:
        try:
            await git_graph_controller.stop()
        except Exception:
            _log.exception("git-engine-worker: controller stop failed")
        try:
            await close_db()
        except Exception:
            pass


app = FastAPI(title="git-engine-worker", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "service": "git-engine-worker"}
