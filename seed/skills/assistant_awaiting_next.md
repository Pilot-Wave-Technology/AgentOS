---
name: assistant_awaiting_next
title: Assistant Awaiting Next
description: Triggered when phase=awaiting_next (a DAG just shipped; story is open for the next plan or to be marked done).
applicable_to: SHCA
status: active
version: 5
---
# Assistant Awaiting Next

State snapshot shows `phase: awaiting_next`. A DAG just completed: diff was approved and pushed to git. The story is **still open** — it's not marked done. The human can:

1. **Continue with another feature in this story** — describe it. You + Claude plan it. New plan → human approves → new DAG ingests on top.
2. **Mark the story done** — click the ✓ Mark story done button in the phase banner. That's the explicit close action. You never call `story_state('done')` on your own.

## What to say (only once per awaiting_next transition)

The control panel already wrote a task-controller `AWAITING_NEXT` message saying the diff was pushed. You add a short follow-up in chat:

> *"DAG `<id>` complete and pushed. What's next — another feature/fix, or shall we mark this story done?"*

## When the human responds with new work

**"Add X" / "Now fix Y" / "Let's also do Z"** — this is the START OF A NEW PLANNING CYCLE. Critical sequence — do these in order:

### Step 1 — transition phase to planning EXPLICITLY

Call `story_phase('planning')`. Don't wait for "auto-transition"; do it now. The next state snapshot will show `phase: planning`.

### Step 2 — send Claude a PLANNING-MODE briefing (NOT a raw forward)

Claude's session is full of recent implementation context from the DAG that just shipped. If you forward the human's raw message without context, Claude will assume "implement this" and start editing files. You must explicitly frame the next turn as PLANNING, not implementation.

Send this as the `message` to `claude_code_communication` (substitute `<story_id>` from snapshot, `<human_request>` is what the user just asked):

```
# New planning cycle for this story

The previous DAG has shipped (diff approved + pushed). The story is still open and the human wants to add new work.

Human request: <human_request>

## 🚨 You are in PLANNING phase. DO NOT IMPLEMENT.

The ONLY files you may create or modify until the plan is approved:
- `repo/stories/<story_id>/scope.md` (update only if scope genuinely broadens)
- `repo/stories/<story_id>/plans/cycle-NN-<slug>.md` (NEW file — name it descriptively)

You may NOT:
- Edit any source code (.html, .js, .py, .css, src/, tests/, etc.)
- Run builds or tests
- Stage or commit

Implementation happens AFTER plan approval, when the control panel dispatches tasks. Until then, your output is the new plan file ONLY.

## What to do this turn

1. Re-read the prior plan files under `repo/stories/<story_id>/plans/` to refresh what's already been built and shipped. Each is frozen history — do NOT modify them.
2. Optionally skim sibling stories' plans under `repo/stories/<other_id>/` if they touch related code. Reads are free; writes are scoped to THIS story's subdir.
3. Ask 2-4 SCOPE/BEHAVIOR clarifying questions about the new feature (NOT implementation-flavored questions). Wait for the human's response before drafting the plan.
4. When scope is clear, write `repo/stories/<story_id>/plans/cycle-NN-<slug>.md` with the standard sections (Goal / Scope / Approach / Test strategy / Risks / Task DAG). Update `scope.md` only if the story scope genuinely broadened.
5. After writing the plan, tell the human the file path + a one-paragraph summary. Then STOP — wait for them to ✓ Approve plan or give revision feedback.

DO NOT implement anything until you see a `[DISPATCH] Task T<id>` line. The human's reply to your clarifying questions is NOT an implementation green light — it's planning input.
```

### Step 3 — post Claude's response to chat

Verbatim, with an optional one-line prefix ("Claude has some scope questions before drafting the plan:").

### Step 4 — phase transition is now visible to next turn

The state snapshot on Claude's next turn will show `phase: planning`. AGENTS.md routes you to the `assistant_code_planning` skill from then on. Follow that skill's relay-and-surface flow.

## When the human responds with "mark done"

Point them at the ✓ Mark story done button. Don't call `story_state` yourself; the button is the canonical action.

## When the human responds with an off-topic question

Answer normally; don't push for a decision.

## Don't

- Don't auto-close the story.
- Don't badger the human if they want to take a break — they'll come back.
- Don't accumulate multiple "what's next?" messages — once is enough.
- Don't forward raw user messages to Claude during awaiting_next — always wrap in the planning briefing above. Raw forwards are how Claude ends up implementing without a plan.
- Don't call `story_phase('planning')` until the human has actually asked for new work; the phase change is the signal to Claude that planning is starting.


## If the human starts a new cycle then changes their mind

If the human asks for new work (you moved the phase to planning) and then
decides they don't want it after all, they can close the story immediately:
point them to the **✓ Mark story done** button in the phase banner — it is
available during planning / awaiting_plan_approval, not only here in
awaiting_next. Don't make them approve the new plan first, and don't call
`story_state` yourself. The unused plan file just stays under
`repo/stories/<story_id>/plans/`.
