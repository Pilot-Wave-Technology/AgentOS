"""Pytest bootstrapping for the agentos test suite.

Adds the repo root to `sys.path` so tests can `from app... import ...`
without needing an editable install. Kept intentionally minimal; no
fixtures live here yet.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import os
os.environ.setdefault("OPENCLAW_AUTH_TOKEN", "test-token")
os.environ.setdefault("ARTIFACT_ROOT", "/tmp/artifacts")
