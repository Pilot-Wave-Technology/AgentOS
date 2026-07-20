---
agent_type: GO
prompt_kind: consolidation_user_prompt
version: 2
is_active: true
notes: memory_consolidation_prompts_v2
---
MEMORY.md has exceeded its size cap. This is an automated maintenance turn: the user is NOT present and your reply is discarded. The file is over budget by definition, so you MUST shrink it this turn. There is no skip.

Execute these steps in order. Do not stop early.

1. Call the `read` tool on MEMORY.md to load its full current content.
2. Build a SMALLER version. For every entry decide KEEP / DROP / MERGE / COMPRESS using the schema below. The result must come in at or under ~80% of the cap.
3. Call the `write` tool EXACTLY ONCE on MEMORY.md with the FULL new content (replace, not append). Mandatory — the turn is not complete until this write succeeds.
4. ONLY after the write succeeds, output exactly: [NO_REPLY]

GO section schema (soft budgets):

# Goal context          (<=2k chars)   goal objective, scope, deadline
# Team & roles          (<=3k chars)   members, IA agents, ownership
# Story decisions       (<=8k chars)   architecture/tech decisions across stories
# In-flight stories     (<=5k chars)   stories not yet done, status, blockers
# Repo conventions      (<=4k chars)   branch model, push rules, plan file paths
# Completed work        (<=4k chars)   summarized — full detail is in stories table / git
# Misc                  (<=2k chars)

How to cut:
- Completed-and-shipped story details: COMPRESS to a one-liner; the stories table has the rest.
- Resolved blockers: DROP unless they reveal a recurring pattern.
- Duplicate decisions across stories: MERGE.
- Obvious repetition / duplicated blocks: collapse to a single copy.

Strict consolidation only — no new facts. If unsure what to drop, COMPRESS aggressively, but you MUST write a smaller file.