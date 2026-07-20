---
agent_type: GO
prompt_kind: goal_summary_user_prompt
version: 2
is_active: true
notes: v2: include story ids in Completed/Remaining sections
---
You maintain ONE running summary of everything that has happened in this goal so far. It is refreshed periodically and must always reflect the goal's CURRENT state — not a history log.

=== CURRENT SUMMARY (as of the last checkpoint) ===
{current_summary}

=== NEW ACTIVITY since the last checkpoint (chronological — GO, user, and system messages) ===
{new_activity}

=== CURRENT STORIES (authoritative, from the database — each line: <story_id> | <title> [<state>]) ===
{story_snapshot}

Rewrite the summary by INTEGRATING the new activity into the current summary. This is an UPDATE, not an append:
- Use the CURRENT STORIES list as the source of truth for which stories exist and their state. The Completed and Remaining sections MUST match it. If a story that appeared in the previous summary is no longer in the list, it was removed — drop it.
- ALWAYS include each story's id (from the CURRENT STORIES list) when you mention it in Completed Stories and Remaining Work, so every story is traceable. Start each story line with its id.
- Use the messages to describe WHAT each story is and what was actually done or discussed.
- Carry forward everything from the current summary that is still true; correct or remove anything the new activity or the story list has changed.
- Do NOT write a changelog, and do NOT mention "the checkpoint" or "new activity". Write a standalone, current picture of the goal.

Output the summary under EXACTLY these four sections, in this order. Always keep every heading, even if a section is empty (write "—" under it). Be dense and concrete.

## Current Scope
What this goal is about right now — its boundaries, what is in scope and what is explicitly out of scope.

## What the Users Want to Accomplish
The outcome the user(s) are after, with any stated constraints, preferences, or deadlines. Name the user(s) where it matters.

## Completed Stories
One line per story whose state is done/completed, STARTING WITH THE STORY ID: "<story_id>: <title> — short description of what was delivered".

## Remaining Work
One line per story still open or planned (proposed, accepted, in progress, blocked, …), STARTING WITH THE STORY ID: "<story_id>: <title> [<state>] — short description of what is left".

HARD LIMIT: the entire summary must be under 2000 tokens (~7500 characters). If you approach the limit, compress older or less-relevant detail — never drop the four section headings.

Output ONLY the summary (the four sections). No preamble, no closing remarks.