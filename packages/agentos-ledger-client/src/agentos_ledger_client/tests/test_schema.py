from __future__ import annotations

from agentos_ledger_client import schema


def test_schema_event_constants():
    assert schema.TASK_CREATED == "task.created"
    assert schema.GOAL_CREATED == "goal.created"
    assert schema.MARL_SIM_TICK == "rlagt.sim.tick"
    assert schema.MARL_OBSERVATION == "rlagt.observation"
    assert schema.MARL_BELIEF_UPDATED == "rlagt.belief.updated"
    assert schema.MARL_ACTION_PROPOSED == "rlagt.action.proposed"
    assert schema.MARL_ACTION_EXECUTED == "rlagt.action.executed"
    assert schema.MARL_OUTCOME_OBSERVED == "rlagt.outcome.observed"
    assert schema.MARL_MODEL_CHECKPOINT == "rlagt.model.checkpoint"
    assert schema.PLAN_PROPOSED == "plan.proposed"
    assert schema.PLAN_APPROVED == "plan.approved"
    assert schema.ACTION_TEMPLATE_DISCOVERED == "action.template_discovered"

