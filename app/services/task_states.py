"""Single source of truth for task-state terminal sets (claude-code model).

See ASSISTANT_WORKFLOW_REFACTOR_PLAN.md §3/§8. Historically three different
"terminal" sets were hand-written across sentinel_parser and task_controller, which
let a DAG rollup disagree with itself (a failed sibling could be skipped by one
check and counted by another). Everything now imports these constants.

Note: the DB `tasks_state_check` CHECK constraint is intentionally left permissive.
Migration rules forbid tightening a CHECK, and historical rows / the (being-removed)
IA path may still use legacy states. The claude-code path only ever WRITES the
reduced set (pending, ready, running/in_progress, blocked, done, failed, cancelled);
the legacy states remain valid solely so old rows don't break.
"""
from __future__ import annotations

# Phase-task (claude-code-phase child) states meaning "still in flight" — the DAG
# cannot roll up while any child is in one of these.
ACTIVE_PHASE_STATES = ("pending", "ready", "running", "dispatching", "in_progress", "blocked")

# A phase-task child in any of these will not progress on its own. Used by the DAG
# rollup ("are all children terminal?"). Inclusive of legacy terminals (rejected,
# halted) so a historical or force-killed row can never wedge a rollup.
TERMINAL_PHASE_STATES = ("done", "failed", "cancelled", "rejected", "halted")

# Terminal for any task, including the parent DAG. 'approved' is a legacy-IA terminal.
TERMINAL_TASK_STATES = ("done", "failed", "cancelled", "rejected", "halted", "approved")


def all_terminal(states) -> bool:
    """True if every state in `states` is terminal — the DAG rollup gate."""
    term = set(TERMINAL_PHASE_STATES)
    return all(s in term for s in states)


def any_failed(states) -> bool:
    """True if any child failed — a failed child fails the whole DAG."""
    return any(s == "failed" for s in states)
