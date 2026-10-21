"""Connect to the suite's database, or skip — the repo's convention.

~97 test files carry their own `_connect_or_skip()`. This is the same
contract in one place for the SQL guards, which all need two
connections rather than one.

WHY SKIP RATHER THAN FAIL, given these guards exist to be loud. The
DSN can be a real database, or a placeholder some other conftest set
at import time, and a guard cannot tell the difference from here.
`app/services/model_registry/tests/conftest.py` sets
`DATABASE_URL=postgresql://x:x@localhost:5432/x` when it is loaded as
an INITIAL conftest -- which happens whenever that directory is named
on the command line, because pytest loads those before the root
conftest's `pytest_configure` can start the embedded server. So
`pytest tests app/services/model_registry` hands every test a DSN
pointing at nothing.

Failing there would punish a legitimate command for an unrelated
module's import-time side effect. Skipping matches every other
database test in the repo, and the case this would otherwise catch --
a CI run whose database never came up -- is already covered, and
covered better, by the mass-skip ceiling in `scripts/ci/run_suite.sh`:
it fails the build when skips pass 120, where the measured floor with
a healthy database is under 60.
"""
from __future__ import annotations

import os

import pytest

KERNEL_SEARCH_PATH = "kernel,public"


def dsn() -> str | None:
    return (os.environ.get("INTERLOCK_TEST_DSN")
            or os.environ.get("DATABASE_URL") or None)


async def connect_pair(asyncpg):
    """A plain connection and a kernel-search_path one, or skip.

    The control plane's SQL names kernel tables with the schema and the
    kernel's own SQL does not, so the guards need both to judge each
    query the way it will actually run.
    """
    target = dsn()
    if not target:
        pytest.skip("no INTERLOCK_TEST_DSN / DATABASE_URL")
    try:
        plain = await asyncpg.connect(target, timeout=10)
    except Exception as exc:                                # noqa: BLE001
        pytest.skip(f"database unreachable ({type(exc).__name__})")
    try:
        kern = await asyncpg.connect(
            target, timeout=10,
            server_settings={"search_path": KERNEL_SEARCH_PATH})
    except Exception as exc:                                # noqa: BLE001
        await plain.close()
        pytest.skip(f"database unreachable ({type(exc).__name__})")
    return plain, kern
