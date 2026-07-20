"""
Task runner — generic async executor (Cloud Run Job entry point).

Fired with `run.jobs.run` (no overrides) whenever background work may be
pending; on each execution it polls every registered task handler in order and
runs those that have pending work. Because it self-discovers work from the DB,
duplicate triggers are harmless (a handler with nothing to do returns False).

Adding a new async task is a registry change only — see task_runner/tasks/__init__.py.

Environment variables (injected by the Cloud Run job config):
  DATABASE_URL  PostgreSQL DSN — see task_runner/config.py
"""

import logging
import sys

from task_runner.config import DATABASE_URL
from task_runner.tasks import HANDLERS

_log = logging.getLogger("task_runner")


def main() -> None:
    _log.info(
        "task_runner start: %d handler(s) registered, DATABASE_URL host=%s",
        len(HANDLERS), DATABASE_URL.split("@")[-1][:40],
    )

    did_any_work = False
    had_failure = False

    for handler in HANDLERS:
        _log.info("task_runner: polling handler=%s", handler.name)
        try:
            worked = handler.run_once()
            if worked:
                did_any_work = True
                _log.info("task_runner: handler=%s processed one item", handler.name)
            else:
                _log.info("task_runner: handler=%s had no pending work", handler.name)
        except Exception:
            # A handler failure must not stop other handlers from running. Each
            # handler is responsible for persisting its own failure state; we log
            # and continue, then exit non-zero so the failure is visible.
            had_failure = True
            _log.exception("task_runner: handler=%s raised — continuing", handler.name)

    if not did_any_work:
        _log.info("task_runner exit: no pending work across any handler (duplicate trigger or race)")

    if had_failure:
        _log.error("task_runner exit: one or more handlers failed")
        sys.exit(1)

    _log.info("task_runner exit: done")


if __name__ == "__main__":
    main()
