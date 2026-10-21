"""The split changed no number. Proved against the module it replaced.

S0 MOVED 956 LINES. The claim "pure refactor" is worth exactly as much
as the evidence for it, and reading a diff that large proves nothing --
the failure mode is a function that lost a filter or a rounding step in
the move, which looks identical and scores differently.

SO BOTH VERSIONS RUN. `git show HEAD:...scoring.py` is the pre-split
module; it is loaded under its own name, pointed at the same database
connection as the new one, and the two outputs are compared field by
field. When the two agree on real SQL over real rows, the refactor is
what it says it is.

AFTER S0 LANDS THIS TEST RETIRES ITSELF: once the old module is no
longer the parent of HEAD it has nothing to compare against, and it
skips rather than failing. It exists to get the split across, not to
pin the engine for ever -- `test_taximeter_normalise.py` does that.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

_ROOT = Path(__file__).resolve().parents[3]
_REL = "app/services/taximeter/scoring.py"


def _pre_split_module():
    """The scoring module as it was before the split, or None."""
    try:
        src = subprocess.run(
            ["git", "show", f"HEAD:{_REL}"], cwd=_ROOT,
            capture_output=True, text=True, timeout=30)
    except Exception:                                       # noqa: BLE001
        return None
    if src.returncode != 0 or "_population_relative_scores" not in src.stdout:
        # HEAD already carries the split: there is nothing to compare.
        return None
    tmp = Path(tempfile.mkdtemp()) / "scoring_pre_split.py"
    tmp.write_text(src.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("taximeter_pre_split", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["taximeter_pre_split"] = mod
    spec.loader.exec_module(mod)
    return mod


def _point_at(conn):
    """Make every taximeter module use this one connection as its pool.

    `get_pool()` is imported INTO each module at import time, so patching
    `app.database.get_pool` would miss them all. Each module's own
    attribute is replaced instead -- including the pre-split module's.
    """
    import app.services.taximeter as pkg

    class _Pool:
        """A pool whose `acquire` hands back the one open transaction."""
        async def fetch(self, *a, **k):
            return await conn.fetch(*a, **k)

        async def fetchval(self, *a, **k):
            return await conn.fetchval(*a, **k)

        async def fetchrow(self, *a, **k):
            return await conn.fetchrow(*a, **k)

        async def execute(self, *a, **k):
            return await conn.execute(*a, **k)

    pool = _Pool()
    names = ["compute", "drilldown", "goals", "reads", "window",
             "metric_scores", "category_scores"]
    touched = []
    for n in names:
        mod = getattr(pkg, n, None) or __import__(
            f"app.services.taximeter.{n}", fromlist=[n])
        if hasattr(mod, "get_pool"):
            touched.append((mod, mod.get_pool))
            mod.get_pool = lambda _p=pool: _p
    return pool, touched


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        old = _pre_split_module()
        if old is None:
            pytest.skip("HEAD has no pre-split scoring module to compare against")
        tx = conn.transaction()
        await tx.start()
        try:
            pool, touched = _point_at(conn)
            old.get_pool = lambda _p=pool: _p
            await body(conn, old)
        finally:
            for mod, fn in touched:
                mod.get_pool = fn
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_compute_activity_is_unchanged():
    async def body(conn, old):
        from app.services.taximeter import scoring as new

        for kwargs in ({}, {"window_weeks": 4}, {"sort": "name", "direction": "asc"}):
            a = await old.compute_activity(**kwargs)
            b = await new.compute_activity(**kwargs)
            assert a == b, f"compute_activity differs for {kwargs}"
    _run(body)


def test_the_goal_lists_are_unchanged():
    async def body(conn, old):
        from app.services.taximeter import scoring as new

        assert await old.fetch_taximeter_goals() == await new.fetch_taximeter_goals()
        assert (await old.fetch_goal_list_with_contributors()
                == await new.fetch_goal_list_with_contributors())
    _run(body)


def test_the_drilldown_is_unchanged():
    async def body(conn, old):
        from app.services.taximeter import scoring as new

        uid = await conn.fetchval("SELECT uid FROM users LIMIT 1") or "nobody"
        assert (await old.compute_goal_drilldown(uid)
                == await new.compute_goal_drilldown(uid))
    _run(body)


def test_the_names_dashboard_metrics_imports_still_resolve():
    """The facade is the point of keeping `scoring.py` at all: every
    consumer imports from it, and a split that renamed one of these
    would break the dashboard at request time rather than here."""
    from app.services.taximeter import scoring

    for name in ("compute_activity", "fetch_taximeter_goals",
                 "fetch_goal_list_with_contributors", "compute_goal_drilldown",
                 "SORTABLE_COLUMNS"):
        assert hasattr(scoring, name), f"scoring.{name} is gone"

    src = (_ROOT / "app/services/dashboard_metrics.py").read_text(encoding="utf-8")
    for name in ("compute_activity", "SORTABLE_COLUMNS"):
        assert name in src, f"dashboard_metrics no longer uses {name} — check the facade"
