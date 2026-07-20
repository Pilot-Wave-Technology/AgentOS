---
agent_type: GO
prompt_kind: story_complete_user_prompt
version: 1
is_active: true
notes: seed v1 from memory_design/flush_triggers.md
---
Story {story_id} "{story_title}" just transitioned to {final_state}.

This is a story-complete checkpoint. You orchestrate stories across this
entire goal. Capture orchestration learnings from how this story went.

Update `MEMORY.md` with patterns and meta-decisions:
  - How the story was sized and whether the sizing held
    ("Estimated 5 tasks, ran 6; the testing phase was underestimated")
  - Which team member handled it and how their work shape was
    ("Prateek prefers planning-heavy stories; pushed back when discovery
     was thin")
  - Cross-story dependencies discovered
    ("This story exposed that the catalog service can't be modified
     without coordinating with platform team")
  - Stakeholder constraints that surfaced
    ("Legal sign-off required for any auth-related changes — affects
     future stories X, Y, Z")
  - What worked / didn't in the planning → execution → review cycle

Append to `memory/{today}.md` a brief paragraph on the story outcome.

Do NOT save:
- Specific task transitions or progress (ledger has it)
- Story state — that's in the DB
- Routine dispatch chatter

Save PATTERNS, not state.

Reply [NO_REPLY] when done.