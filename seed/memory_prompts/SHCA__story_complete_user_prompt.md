---
agent_type: SHCA
prompt_kind: story_complete_user_prompt
version: 2
is_active: true
notes: v2 imperative, no USER_MEMORY (078)
---
Story {story_id} "{story_title}" just transitioned to {final_state}.

This is a story-complete memory checkpoint. This turn is invisible to your user — your output never reaches them, and you will not see this turn again. Your ONLY job is to write two memory files capturing this story arc, then end.

You MUST emit exactly TWO `write` tool calls before [NO_REPLY]:

  1. write(file_path="memory/{today}.md", ...)   — story narrative
  2. write(file_path="MEMORY.md", ...)           — distilled durable facts

Both files are append-only this turn. Do NOT write to any other file. Do NOT reply [NO_REPLY] until BOTH writes are done.

────────────────────────────────────────────────────────────────────
CALL 1 — memory/{today}.md   (story narrative)
────────────────────────────────────────────────────────────────────
Append a short narrative of this story under a `## {today} — {story_title}` heading:
  - Original ask
  - Key decisions
  - Final outcome ({final_state})
  - One sentence on what you'd do differently
If the story was rejected/cancelled, state explicitly what went wrong so future stories don't repeat it. Prose or a tight bulleted list is fine — this file is search-only.

────────────────────────────────────────────────────────────────────
CALL 2 — MEMORY.md   (distilled durable facts)
────────────────────────────────────────────────────────────────────
Append <=500 chars of distilled insight worth keeping BEYOND this story, under the appropriate existing section:

  ## User preferences     — preferences this story revealed about how your user works
  ## Project conventions  — decisions/conventions that should outlive this story (branching, tooling, etc.)
  ## Working context      — open threads that span beyond this story
  ## Recent               — anything else, plus failed approaches AND why

If nothing fits the top three sections, write at least one `- {today}:` line under ## Recent. Keep it terse.

Do NOT save: tool-call output or raw file contents, anything reproducible from git / the ledger / the stories table, or conversational tone.

After BOTH writes are done, reply with exactly: [NO_REPLY]