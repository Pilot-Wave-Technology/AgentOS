"""One call per entrypoint. Replaces four divergent configs and three
bare basicConfig calls.

WHAT IT WIRES. A filter that stamps the context and process fields onto
every record, and a renderer that turns the result into a line. The
filter is the reason no call site changes: `logger.info("...")` anywhere
in the tree comes out carrying the ids, because the filter runs on the
record after it is made and before it is formatted.

WHY A FILTER AND NOT A FORMATTER. A formatter is the last step and only
one can be attached; a filter can be attached to the root handler and
still leaves the renderer swappable. It also means the fields exist as
structured data on the record, so a future OTLP exporter reads the same
attributes rather than re-parsing a string.
"""

from __future__ import annotations

import logging
import os
import sys

from kernel.obs import context
from kernel.obs import fields as F
from kernel.obs.render import renderer


class ContextFilter(logging.Filter):
    """Stamps the ids. Never rejects a record -- always returns True."""

    def __init__(self, process: dict):
        super().__init__()
        self._process = process

    def filter(self, record: logging.LogRecord) -> bool:
        merged = dict(self._process)
        merged.update(context.current())
        # An `event=` passed at the call site wins over the context, so
        # `logger.info("done", extra={"event": "leg_end"})` works without
        # a second mechanism.
        extra = getattr(record, "agentos", None)
        if extra:
            merged.update(extra)
        # `extra={"event": "leg_end"}` is the ordinary way to name one,
        # so lift it out of the record rather than making callers know
        # about the `agentos` key. The vocabulary in fields.EVENTS is
        # advisory -- a typo should log, not raise.
        if hasattr(record, "event"):
            merged["event"] = record.event
        record.agentos = merged
        return True


_configured = False


def configure(component: str | None = None, level: str | None = None) -> None:
    """Install the filter and renderer on the root logger.

    Idempotent: a second call from a module that also wants to be an
    entrypoint is a no-op rather than a second handler printing every
    line twice -- which is the failure this project has hit with
    basicConfig being called in three kernel entrypoints.
    """
    global _configured
    if _configured:
        return

    if component:
        os.environ["AGENTOS_COMPONENT"] = component

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(renderer())
    handler.addFilter(ContextFilter(F.process_fields()))

    root = logging.getLogger()
    # Replace rather than add. Three kernel entrypoints call basicConfig,
    # and a stack that has run one of them already has a plain handler
    # that would print every line a second time, unstamped.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)

    want = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, want, logging.INFO))

    # uvicorn installs its own handlers and would otherwise bypass all of
    # this -- which is how unhandled-500 tracebacks came to be the only
    # thing reaching Cloud Logging in a usable shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    _configured = True


def reset_for_tests() -> None:
    global _configured
    _configured = False
