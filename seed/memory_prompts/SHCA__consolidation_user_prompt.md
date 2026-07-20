---
agent_type: SHCA
prompt_kind: consolidation_user_prompt
version: 2
is_active: true
notes: memory_consolidation_prompts_v2
---
MEMORY.md has exceeded its size cap. This is an automated maintenance turn: your user is NOT present and your chat reply is discarded. The file is over budget by definition — this turn would not have started otherwise — so you MUST shrink it this turn. There is no skip.

Execute these steps in order. Do not stop early.

1. Call the `read` tool on MEMORY.md to load its full current content.
2. Build a SMALLER version. For every entry decide KEEP / DROP / MERGE / COMPRESS using the schema and budgets below. The result must come in at or under ~80% of the cap.
3. Call the `write` tool EXACTLY ONCE on MEMORY.md with the FULL new content. This replaces the file (it is not an append). This step is mandatory — the turn is not complete until this write succeeds.
4. ONLY after the write tool returns success, output exactly: [NO_REPLY]

Section schema (sizes are soft budgets — redistribute as needed):

# User profile           (<=2k chars)   stable preferences, role, tooling
# People & roles         (<=3k chars)   teammates, ownership, who decides what
# Decisions              (<=8k chars)   technical + product decisions still in force
# Open threads           (<=5k chars)   work-in-progress, awaited reviews, blockers
# Conventions & patterns (<=5k chars)   how this codebase works that's non-obvious
# Failed approaches      (<=4k chars)   what we tried that didn't work, and why
# Misc / drafts          (<=3k chars)   scratch space, reviewed each consolidation

How to cut:
- Resolved decisions: KEEP if still load-bearing (people refer back to it); DROP if superseded or no longer relevant.
- Old open threads: DROP if closed/abandoned; MERGE related threads into one entry.
- Duplicates across sections: MERGE; keep the most precise version.
- Verbose entries: COMPRESS to a one-line summary.
- Obvious repetition / duplicated blocks: collapse to a single copy.

Do NOT add new facts in this turn — strict consolidation only. If you are unsure what to drop, COMPRESS aggressively, but you MUST write a smaller file.