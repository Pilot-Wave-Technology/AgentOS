---
agent_type: GO
prompt_kind: flush_user_prompt
version: 3
is_active: true
notes: 071_memory_flush_prompts_v2: mandate dual write
---
You are in a memory-flush turn. This turn is invisible to your user — your output never reaches them. You will not see this turn again either; older context is being compacted right after this.

You MUST emit exactly TWO `write` tool calls before [NO_REPLY]:

  1. write(file_path="memory/<today>.md", ...)   — daily narrative
  2. write(file_path="MEMORY.md", ...)           — distilled durable facts

Both files are append-only this turn. Replace <today> with the current date in YYYY-MM-DD form.

────────────────────────────────────────────────────────────────────
CALL 1 — memory/<today>.md
────────────────────────────────────────────────────────────────────
Append a brief narrative of THIS conversation's orchestration activity: what stories moved, what was approved or rejected, what's queued, what blockers surfaced, what the user emphasized.

Format: 1–2 short paragraphs OR a tight bulleted list. Prose is fine — this file is search-only, never re-injected into context. Always start the entry with a `## YYYY-MM-DD HH:MM` heading.

Minimum: one dated line, even if the conversation was thin.

────────────────────────────────────────────────────────────────────
CALL 2 — MEMORY.md
────────────────────────────────────────────────────────────────────
Append ≤500 chars of DISTILLED orchestration insight under the appropriate section. MEMORY.md sections (already present in the file):

  ## Patterns                    — story sizing / team capacity / dispatch rules
  ## Decisions                   — orchestration decisions and their WHY
  ## Stakeholders & Constraints  — deadlines, compliance gates, sign-offs
  ## Recent                      — fresh insights waiting for consolidation

If nothing new is durable enough for Patterns / Decisions / Stakeholders, write under ## Recent. Always write at least one line under ## Recent with a `- YYYY-MM-DD:` prefix referencing today.

What belongs in MEMORY.md:
- Patterns, rules, meta-decisions that will help orchestrate FUTURE stories
  (e.g. "stories with phased plans ship cleanly")
- Cross-story dependencies discovered during planning
- Stakeholder constraints with concrete impact
- Failed approaches AND why

What does NOT belong in MEMORY.md:
- Per-story status (the stories table is truth)
- Individual task transitions or dispatch ticks
- Anything recoverable by querying `stories` or `tasks`

Keep entries terse. If a section is getting long, move detail to today's daily log instead.

────────────────────────────────────────────────────────────────────
ORDER OF OPERATIONS
────────────────────────────────────────────────────────────────────
1. Call write for memory/<today>.md
2. Call write for MEMORY.md
3. Reply with [NO_REPLY]

Do NOT reply [NO_REPLY] before both writes succeed. Skipping either write is a bug — at minimum write the dated one-liner described above.