"""Memory v2 — OpenClaw-native memory management for SHCA + GO.

Replaces the legacy `app/memory.py` PG-backed memory model with:
  - Markdown files in the agent workspace (MEMORY.md, memory/*.md)
  - Agent-driven curation via flush prompts (pre-compaction, story-complete, pre-reap)
  - Control-panel event bridge for deterministic memory writes
  - PG audit table for forensic visibility
  - Workspace snapshot/restore so memory survives SHCA reaps
  - Cross-goal user memory via shared USER_MEMORY.md

See c:/Project collaborator all/memory_design/ for the full design.

Module layout (none over the size-watcher's 400-line warn threshold):
  prompts.py       — load/save flush + compaction prompts from PG
  event_bridge.py  — ledger event → memory file append
  flush.py         — story-complete + pre-reap flush trigger helpers
  snapshot.py      — workspace files snapshot/restore on reap
  audit.py         — memory_writes table inserts
  user_memory.py   — cross-goal USER_MEMORY.md helpers
  routers.py       — admin UI endpoints + read-only views

This package will be renamed to `app.memory` once the legacy `app/memory.py`
is retired (separate cleanup PR after this rollout is stable).
"""
