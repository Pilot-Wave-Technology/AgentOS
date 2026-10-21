"""Bulk-apply a template's current model to every existing agent cloned from it.

Editing a template's model (`templates.model`) never cascades to agents
already cloned from it — SHCA instances are cloned once per user at
creation time (`assistant_provisioning.py::configure_user_assistant`) and
never re-read the template afterwards, unlike RA/IA which re-read their
template fresh on every task dispatch. This is deliberate (confirmed with
the user): a template edit should not silently change a model out from
under an agent someone might be mid-conversation with. This module is the
explicit, admin-triggered opt-in for "apply this template's model to
every existing instance too."
"""
import logging
from typing import Any

from app.models.agent import AgentUpdateRequest
from .update import update_agent

logger = logging.getLogger(__name__)


async def apply_template_model_to_instances(
    pool, template_id: str, actor_id: str,
) -> dict[str, Any]:
    """Set every active agent cloned from `template_id` to the template's
    current model.

    Returns {"model": str, "updated": [agent_id...], "failed": [{"agent_id", "error"}...]}.
    Soft-fails per-agent — one agent's update erroring must not stop the
    rest of the bulk action.
    """
    template = await pool.fetchrow(
        "SELECT model FROM templates WHERE id = $1", template_id,
    )
    if not template:
        raise ValueError(f"Template '{template_id}' not found")
    model = template["model"]
    if not model:
        raise ValueError(f"Template '{template_id}' has no model set")

    # Same instance-discovery query as the template detail page's instance
    # list: agents rows pointing at this template, excluding the template
    # row itself (a template's own id can coincide with an agent id).
    instance_rows = await pool.fetch(
        """
        SELECT id FROM agents
         WHERE template_id = $1 AND status = 'active'
           AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = agents.id)
        """,
        template_id,
    )

    logger.info(
        "apply_template_model_to_instances: template=%s model=%s instances=%d",
        template_id, model, len(instance_rows),
    )

    updated: list[str] = []
    failed: list[dict[str, str]] = []
    for row in instance_rows:
        agent_id = row["id"]
        try:
            await update_agent(
                pool, agent_id, AgentUpdateRequest(model=model), actor_id,
            )
            updated.append(agent_id)
        except Exception as exc:
            logger.warning(
                "apply_template_model_to_instances: failed for agent=%s: %s",
                agent_id, exc,
            )
            failed.append({"agent_id": agent_id, "error": str(exc)})

    logger.info(
        "apply_template_model_to_instances: template=%s done — %d updated, %d failed",
        template_id, len(updated), len(failed),
    )
    return {"model": model, "updated": updated, "failed": failed}
