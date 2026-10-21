"""Test-suite fixtures + import-time environment.

The test suite runs OFFLINE — no live Postgres, no live gateway. It
does need `app.config.get_settings()` to succeed at import time
because the modules under test import from `app.openclaw` /
`app.database` (which read from settings). Populate the required
env vars with dummy values BEFORE any `app.*` module is imported.
"""
import os

# Populate before any `from app.…` import in test files/collection.
os.environ.setdefault("FIREBASE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_WEB_API_KEY", "test-web-api-key")
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")
os.environ.setdefault("OPENCLAW_AUTH_TOKEN", "test-token")

# Strip environment variables that collide with Settings fields. pydantic-
# settings is case-insensitive, so a shell `DEBUG=...` (common on dev
# machines that export Google SDK debug filters) is parsed as the boolean
# `debug` field and blows up Settings() construction — a test-only
# concern; the production container never sets DEBUG.
for _hostile in ("DEBUG", "debug"):
    os.environ.pop(_hostile, None)

import pytest

@pytest.fixture(autouse=True)
def ensure_openclaw_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", "test-token")
