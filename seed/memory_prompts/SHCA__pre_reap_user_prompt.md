---
agent_type: SHCA
prompt_kind: pre_reap_user_prompt
version: 1
is_active: true
notes: seed v1 from memory_design/flush_triggers.md
---
You're about to be reaped. The SHCA cleanup watchdog has decided this
instance is idle and its active story is done. You won't get another
turn until your user returns to this goal.

Last chance to write durable memory.

Update `MEMORY.md`:
  - Anything you observed in recent conversations that you'd want to
    know on your next spawn
  - User preferences, technical decisions, open threads

Append to `memory/{today}.md`:
  - One paragraph on what was happening before idle

If you've been writing memory regularly via the compaction and
story-complete flushes, you probably don't need to save much new here.
Reply [NO_REPLY] if so.

Timeout: 30 seconds. If you take longer the reap proceeds anyway.