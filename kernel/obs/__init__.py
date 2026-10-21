"""Structured logging: ids on every line, set at boundaries, no call-site changes.

    from kernel.obs import bound, configure

    configure(component="worker")           # once, at the entrypoint
    with bound(execution_id=13616, leg=4):  # at a boundary
        logger.info("claimed", extra={"event": "leg_start"})

Four modules, so the thing you want to change is always one file:

  context.py   the contextvar and `bound`. Knows nothing about logging.
  fields.py    THE FIELD SET, as data. Edit this to change the shape.
  render.py    fields -> a line. JSON for Cloud Logging, text for a
               terminal. Swap without touching the other three.
  setup.py     wires them onto the root logger.

It lives in kernel/ and not app/ because kernel must never import from
app -- the E0 container ships kernel/ and not app/ -- so anything both
halves use has to be here, and app imports it in the allowed direction.
"""

from kernel.obs.context import bind, bound, clear, current, unbind
from kernel.obs.setup import configure, reset_for_tests

__all__ = ["bind", "bound", "clear", "configure", "current",
           "reset_for_tests", "unbind"]
