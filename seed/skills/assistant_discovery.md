---
name: assistant_discovery
title: Assistant Discovery
description: Phase=discovery for any story type. Establish scope, acceptance criteria, story type with the human.
applicable_to: SHCA
status: active
version: 4
---
# Discovery

Phase = `discovery`. Establish a clear, shared scope for the story before planning starts.

## Goal

Get to a clear shared understanding of:
- What problem is being solved
- What "done" looks like (acceptance criteria)
- Which repo + branch the work targets — read RESOURCES.md; the GO has already attached the repo/branch for this story

## How to converse

- One or two focused questions per turn — never a wall
- If the request is already clear enough, say so and propose moving to planning
- If the request is vague, dig deeper
- Summarize your understanding before transitioning to planning so the human can correct you

## Transitioning to planning

When you and the human are aligned on scope:

1. Call `story_phase(story_id=<from state snapshot>, phase='planning')`
2. End your message with a one-liner like *"Moving to planning. I'll bring in Claude Code in a moment to help draft the plan."*
3. On your next turn, the new state snapshot will show `phase: planning` and AGENTS.md routes you to the code-planning skill.

## Don't

- Don't propose tasks or specs during discovery — that's the planning phase
- Don't write a plan document during discovery — that's the planning phase
- Don't move to planning without explicit agreement on scope
- Don't ignore the human's "you decide" — make a recommendation and confirm

## If the human wants to close the story instead

If during discovery the human decides the story isn't needed, point them to the **✓ Mark story done** button in the phase banner (available during discovery / planning too). Don't call `story_state` yourself — marking done is a human button action.
