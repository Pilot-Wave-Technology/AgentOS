---
name: review_action_planning
title: Review Action Planning
description: REQUIRED on any [REVIEW:story|goal|security] marker. Read the review in that message and reply ONCE on the goal thread — TERSE: a one-sentence acknowledgement by default; only a short numbered plan (max 3-5 one-line items) for genuinely NEW code-fixable issues; non-code issues to the user. DEDUP: never re-propose items already raised or already open as stories. Proposal-only.
applicable_to: GO
status: active
version: 2
---
# Review Action Planning

Woken with a `[REVIEW:story|goal|security]` marker (match the substring `[REVIEW:` anywhere; it may be prefixed when batched). The full review is in **this same** message. Read it, then reply **once, on the goal thread**. Responding to the review is the ONLY thing you do here — don't start other work.

**Be terse. The default is a one-sentence acknowledgement.** Write a plan only when the review raises genuinely new, code-fixable issues.

## Before writing anything — dedup
Scheduled reviews re-run every cycle and re-raise the same findings. Check your recent goal-thread history and your open/pending stories:
- If an issue was already proposed, or is already an open / in-progress story, do **NOT** raise it again.
- Surface only **net-new** items.
- If nothing is new → one line and stop, e.g. `✅ Review complete — items already tracked; nothing new.`

## Pick one response (keep it tight)

**Clean review** → one line: `✅ Review complete — nothing to action.`

**New code-fixable issues** → a numbered list, **max 3-5 items**, each a single line:
`N. [new story | fix story <id> | re-plan] — <what> — <why, ≤10 words>`
No preamble, no restating the review, no essay. Then stop and wait for explicit human approval. Never call `create_story` / `update_story` / `cancel_story` from a review; on approval, execute via the `story_planning` skill and log it with `record_decision`.

**Non-code issues** (secret rotation, cloud IAM, VM/runtime, org policy — anything needing a human with creds or authority) → list under `Needs you (non-code):`, one line each with what must happen. Never a story.

**Mixed** → the numbered code plan first, then `Needs you (non-code):`.

## Rules
- Terse: one sentence when possible; otherwise ≤5 one-line items. Don't blabber, don't repeat the report, don't repeat prior suggestions.
- Only code fixes become plans; everything else goes to the user.
- Proposal-only — human approval gates every story/task. Never self-execute from a review.
- Always reply on the goal thread.
- Security: code-related only if fixable in the repo; credential rotation / cloud IAM / infra outside the repo → user.
