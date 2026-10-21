"""In-goal action registration — discovering and registering new action templates.

Subagents n layers deep can register discovered action templates into
`ledger_events` (`action.template_discovered`) with full Merkle DAG lineage.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _S, _schema

TOOLS: dict[str, dict] = {
    "register_action_template": _schema(
        "register_action_template",
        "Register a discovered action template into the ledger with full Merkle "
        "DAG lineage for the goal and discovering agent.",
        {
            "template": {
                "type": _S,
                "description": "The action template identifier or representation.",
            },
            "params": {
                "type": "object",
                "description": "Parameters and schema definition for the action template.",
            },
            "cohort_scope": {
                "type": _S,
                "description": "Scope of agents or cohort that can use this action template.",
            },
            "description": {
                "type": _S,
                "description": "Explanation of what this action does and when to use it.",
            },
        },
        ["template", "cohort_scope", "description"],
        side="worker",
        effect="reversible",
    ),
}
