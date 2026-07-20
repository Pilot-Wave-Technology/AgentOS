"""Merge orchestrator — handles system-gate task execution for the integration flow.

Entry points used by task_controller:
  dispatch.find_next_dispatchable_gate()  — select the next ready system-gate task
  dispatch.dispatch_system_gate(task)     — transition to in_progress, call handler

Gate kinds and their handlers:
  'pre-integration'  → handlers/pre_integration.py
  'integrate'        → handlers/integrate.py

See documentation/story-integration/SYSTEM_GATE.md for the full design.
"""
