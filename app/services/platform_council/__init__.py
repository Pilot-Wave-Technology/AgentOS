"""Platform Research Council — a standing admins-only advisory council that
researches platform improvements (models, agent design, coding tooling, infra),
grounded in the repo + template-level inventory + web research.

Design: PLATFORM_RESEARCH_COUNCIL_PLAN.md (working-dir root).

Pieces:
  - inventory.py   — template-level "what exists today" fact sheet (INVENTORY.md)
  - (later phases) provisioning, tools/endpoints, convener loop
"""

PLATFORM_GOAL_ID = "__platform__"

# The v2 main agent: a GO-like steward (type 'Platform') that owns the platform
# goal thread, reads the registry, orchestrates the producers/reviewers, and
# creates stories. (v1's "platform-convener" is retired.)
STEWARD_AGENT_ID = "platform-steward"
