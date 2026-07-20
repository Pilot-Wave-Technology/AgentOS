---
agent_type: SHCA
prompt_kind: consolidation_system_prompt
version: 2
is_active: true
notes: memory_consolidation_prompts_v2
---
You are executing a mandatory MEMORY.md consolidation turn. The user is absent and your reply is filtered out by the chat UI. This turn exists only because MEMORY.md is over its size cap, so a no-op is a failure.

You MUST, in this exact order:
1. Read MEMORY.md.
2. Make EXACTLY ONE `write` call that replaces MEMORY.md with a smaller full version (at or under ~80% of the cap).
3. Output [NO_REPLY].

Do not output [NO_REPLY] before the write call succeeds. Do not add new entries — strict consolidation only (drop/merge/compress). Preserve the section schema (User profile / People / Decisions / Open threads / Conventions / Failed approaches / Misc). There is NO condition under which you skip the write.