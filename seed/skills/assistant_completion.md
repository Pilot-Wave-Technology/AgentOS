---
name: assistant_completion
title: Assistant Completion
description: Phase=done. Short completion summary in chat; save to memory; notify GO; stop.
applicable_to: SHCA
status: active
version: 3
---
# Assistant Completion

Phase = `done`. The human has marked the story closed. Your job: send a
structured story-completion message to the GO (Goal Orchestrator) so the
GO has the canonical record of what shipped, then post a short chat
summary, then stop.

## Required actions, in order

### Step 1 (REQUIRED) — message the GO with a structured summary

This is the canonical record the GO uses for goal-level reporting. It
MUST cover every cycle that shipped, not just the last state. Use
`message_go(type='update')` with this exact structure:

```
## Story complete: <story title>

**Story id:** `<story_id>`
**Branch:** `<branch from RESOURCES.md>`
**Final state:** done

### Cycles shipped

#### Cycle 1 — <feature name from cycle-NN-<slug>.md>
- **Plan:** `repo/stories/<story_id>/plans/cycle-01-<slug>.md`
- **Commit:** `<sha>` (use `custom_git op=log -1 --grep='plan: ...'` if you need to find the right one)
- **Tasks:** <N> completed across phases: <comma-separated phase list>
- **Summary:** <2-3 sentences synthesized from task DONE summaries — what was actually built>

#### Cycle 2 — <next feature, if any>
- **Plan:** `repo/stories/<story_id>/plans/cycle-02-<slug>.md`
- **Commit:** `<sha>`
- **Tasks:** <N> across <phases>
- **Summary:** <2-3 sentences>

(repeat per cycle)

### Story-level scope

`repo/stories/<story_id>/scope.md` — the living scope file, edited
across cycles. Read this for the overall story context.

### Notable decisions / risks resolved

- <one-liner per significant decision the team made during the story,
  including replans, rejected diffs, scope changes>

### What's still open

- Nothing (story marked done by the human).
- (Or note any deferred work the human chose not to pursue.)
```

How to populate each field:

- **plan files**: from `recent_progress` snapshots + chat history. Each cycle has its own `cycle-NN-<slug>.md`.
- **commit SHAs**: `custom_git op=log -10 --oneline` gives recent commits; the "plan:" prefix + the push commit per cycle are what you want. The state snapshots after each diff approval also surface commit SHAs.
- **task counts + phases**: from `recent_progress` array + the `phase_task_progress` cards in chat history.
- **summary per cycle**: synthesize from the `PHASE_TASK_DONE` sentinel summaries Claude emitted for that cycle's tasks. Don't enumerate every task — give 2-3 sentences capturing what was built.
- **decisions**: scan chat history for replans, diff rejections, scope-broadening moments. Skip mundane Q&A.

### Step 2 (REQUIRED) — short chat summary for the human

```
## Story complete — <title>

Shipped <N> cycle(s): <cycle 1 name>, <cycle 2 name>, ...
Total: <total tasks> tasks across <unique phases>.
Latest commit: `<sha>` on branch `<branch>`.

Sent a full ledger to the GO. Tell the GO when you want the next story.
```

Keep this tight. The full record is in the GO message.

### Step 3 — save key outcomes to memory

For each significant decision or pattern the team established, save a
memory at scope=`goal` so future stories on this goal inherit the
context. Don't dump everything — only what's load-bearing for future work
(e.g. "we chose Redis over in-memory for session storage because of
horizontal-scaling requirement").

### Step 4 — STOP

Do not:
- Propose what story to do next (that's the human + GO's call)
- Congratulate Claude, the human, or yourself
- Write a long retrospective
- Continue chatting unless the human asks something

## Why this matters

The GO orchestrates the whole goal. Without your completion message, the
GO doesn't know what's actually in the repo or what the team decided.
Goal-level reports, dependency tracking, and future story planning all
depend on accurate per-story records. Don't skip step 1.

## Don't

- Don't skip step 1. The GO message is the canonical record.
- Don't paraphrase the GO message structure — use the section headings
  exactly as shown so the GO can parse it consistently.
- Don't speculate about commits or task counts — pull from
  `custom_git op=log` and `recent_progress` / chat history.
- Don't propose follow-up work; the human asks the GO when they want
  the next story.
