"""
Task handler registry for the generic task runner.

Each async task the runner can perform is a module in this package exposing a
`run_once() -> bool` callable:
    True  — claimed and processed one unit of pending work.
    False — no pending work for this task type this execution.

To add a new async task:
  1. Create `task_runner/tasks/<your_task>.py` with a `run_once()` function that
     polls its own queue/table for pending work (see document_render.py).
  2. Append a TaskHandler entry to HANDLERS below.
That is the whole change — the dispatcher (task_runner/main.py) polls every
registered handler automatically.
"""

from dataclasses import dataclass
from typing import Callable

from task_runner.tasks import document_render


@dataclass(frozen=True)
class TaskHandler:
    """A named unit of async work the runner can execute."""

    name: str                    # stable label used in dispatcher logs
    run_once: Callable[[], bool]  # process one pending item; True if work was done


# Ordered list of everything the runner will poll on each execution. Order is the
# priority order the dispatcher tries them in.
HANDLERS: list[TaskHandler] = [
    TaskHandler(name="document_render", run_once=document_render.run_once),
]
