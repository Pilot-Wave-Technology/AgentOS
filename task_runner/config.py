"""
Shared configuration for the task-runner Cloud Run Job.

The task runner is a generic async executor: a single Cloud Run Job that is
fired (with no overrides) whenever background work may be pending, and then
polls the database for work to do. Individual task types live under
`task_runner/tasks/` — see `tasks/__init__.py` for the handler registry.

All configuration arrives via environment variables injected by the Cloud Run
job config (Secret Manager for secrets):
  DATABASE_URL    PostgreSQL DSN — shared by every task handler.
                  since other handlers (document_render) don't need it;
                  that handler raises its own clear error if it's missing
                  when actually called.
"""

import logging
import os
import sys

# Configure root logging once, for every handler in the process. Handlers use
# their own named loggers (e.g. "document_render") so log lines say which task
# produced them.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)

# Shared across all task handlers — every current task reads/writes the same DB.
DATABASE_URL = os.environ["DATABASE_URL"]

# NOTHING HERE NEEDS GEMINI ANY MORE. `goal_weightage_recompute` was the
# only handler that called a model, and it is gone: a goal's weight is now
# `1 + items completed in it this period`, computed from the taximeter view
# at score time rather than guessed by an LLM. The secret can come off the
# task-runner deploy.
