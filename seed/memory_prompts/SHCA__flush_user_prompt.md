---
agent_type: SHCA
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
Append a brief narrative of today's work for this user/goal: what was discussed, decided, queued, or blocked. Format: 1–2 short paragraphs OR a tight bulleted list. Prose is fine — this file is search-only. Always start the entry with a `## YYYY-MM-DD HH:MM` heading.

Minimum: one dated line, even if the conversation was thin.

────────────────────────────────────────────────────────────────────
CALL 2 — MEMORY.md
────────────────────────────────────────────────────────────────────
Append ≤500 chars of DISTILLED durable insight under the appropriate section. MEMORY.md sections (already present in the file):

  ## User preferences     — communication style, format preferences, escalation thresholds
  ## Project conventions  — repo conventions, branch strategy, naming
  ## Working context      — active mental models, in-progress decisions
  ## Recent               — fresh insights waiting for consolidation

If nothing new fits the top three sections, write under ## Recent. Always write at least one line under ## Recent with a `- YYYY-MM-DD:` prefix referencing today.

What belongs in MEMORY.md:
- User preferences you've observed
- Open threads that should survive compaction
- Non-obvious decisions made for this goal (with WHY)
- People + roles relevant to this user's work
- Failed approaches AND why

What does NOT belong in MEMORY.md:
- Raw tool output, file dumps, transcript-style detail
- Anything reproducible from git, the ledger, or the database
- Routine state transitions or conversational filler

Keep entries terse. If a section is getting long, move detail to today's daily log.

────────────────────────────────────────────────────────────────────
ORDER OF OPERATIONS
────────────────────────────────────────────────────────────────────
1. Call write for memory/<today>.md
2. Call write for MEMORY.md
3. Reply with [NO_REPLY]

Do NOT reply [NO_REPLY] before both writes succeed. Skipping either write is a bug — at minimum write the dated one-liner described above.