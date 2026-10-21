---
agent_type: SHCA
prompt_kind: story_complete_user_prompt
version: 4
is_active: true
notes: stage-k-concurrent-threads
---
Story {story_id} "{story_title}" just transitioned to {final_state}.

This is a story-complete memory checkpoint. This turn is invisible to your user — your output never reaches them, and you will not see this turn again. Your ONLY job is to write two memory files capturing this story arc, then end.

You may be holding OTHER stories in other threads right now, and they share this home. That is why the first file is named after the story and why the second must be re-read before it is written.

You MUST emit exactly TWO `write` tool calls before [NO_REPLY]:

  1. write(file_path="memory/{story_id}.md", ...)  — this story's narrative
  2. write(file_path="MEMORY.md", ...)             — distilled durable facts

Do NOT write to any other file. Do NOT reply [NO_REPLY] until BOTH writes are done.

────────────────────────────────────────────────────────────────────
CALL 1 — memory/{story_id}.md   (this story's narrative)
────────────────────────────────────────────────────────────────────
This file belongs to this story and nothing else writes it. Write the whole
narrative under a `## {today} — {story_title}` heading:
  - Original ask
  - Key decisions
  - Final outcome ({final_state})
  - One sentence on what you'd do differently
If the story was rejected/cancelled, state explicitly what went wrong so future stories don't repeat it. Prose or a tight bulleted list is fine — this file is search-only.

If the file already exists (a story run twice), `read` it first and keep what is there — append a second heading below it.

────────────────────────────────────────────────────────────────────
CALL 2 — MEMORY.md   (distilled durable facts)
────────────────────────────────────────────────────────────────────
**`read` MEMORY.md immediately before this write, in this turn.** Not from
memory of it, not from earlier in the conversation — another thread may have
appended to it since. Your write must reproduce every line that read
returned, unchanged, with your addition at the end of its section. `write`
replaces the file: a line you do not carry forward is a line that is gone.

Append <=500 chars of distilled insight worth keeping BEYOND this story, under the appropriate existing section:

  ## User preferences     — preferences this story revealed about how your user works
  ## Project conventions  — decisions/conventions that should outlive this story (branching, tooling, etc.)
  ## Working context      — open threads that span beyond this story
  ## Recent               — anything else, plus failed approaches AND why

Every line you add starts with `- {today} ({story_id}):` so a fact can be traced back to the story that produced it — with several stories running, an undated, unattributed line is one nobody can check.

If nothing fits the top three sections, write at least one such line under ## Recent. Keep it terse.

Do NOT save: tool-call output or raw file contents, anything reproducible from git / the ledger / the stories table, or conversational tone.

After BOTH writes are done, reply with exactly: [NO_REPLY]