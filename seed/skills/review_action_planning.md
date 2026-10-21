---
name: review_action_planning
title: Review Action Planning
description: REQUIRED on any [REVIEW:story|goal|security] marker. Read the review in that message and reply ONCE on the goal thread — TERSE: one sentence by default, otherwise a numbered list of at most 5 one-line items. SUGGEST ONLY: never call create_story/create_errand/update_story/cancel_story from a review, before or after approval. Two action shapes only (story = lands on trunk, errand = does not); anything else goes to a person under "Needs you:". Cite the finding key each action addresses. Taking no action is a complete answer.
applicable_to: GO
status: active
version: 3
---
# Review Action Planning

Woken with a `[REVIEW:story|goal|security]` marker (match the substring
`[REVIEW:` anywhere; it may be prefixed when batched). The full review is in
**this same** message. Read it, then reply **once, on the goal thread**.
Responding to the review is the ONLY thing you do here — don't start other work.

## You suggest. You do not act.

**Never call `create_story`, `create_errand`, `update_story` or `cancel_story`
from a review** — not before approval, and not after it. Your reply is a
proposal a person reads and acts on. This is true even when the action seems
obvious and even when someone says "go ahead" in the same breath.

(This applies to the review path only. Your ordinary planning work is
unchanged.)

## Be terse

The default is one sentence. Write a list only when the review raises something
new that a person should act on.

**Taking no action is a correct and complete answer.** It needs no
justification. Five invented suggestions are worse than none — they cost
someone the time to read and dismiss them, and they make the next review's
suggestions cheaper to ignore.

## Pick one shape of reply

**Nothing to do** → one line: `✅ Review read — nothing to action.`

**Something to do** → a numbered list, **maximum 5**, worst first:

```
N. [story|errand] (key: <finding-key>) — <what> — <why, ≤10 words>
```

Then stop and wait for a person.

- **story** — work that lands on trunk. A branch, a merge, a change that stays.
- **errand** — work that never lands on trunk. The workspace is thrown away and
  what survives is what it emitted: an investigation, a report, a data pull.

Those are the only two. If the thing you want is neither — a credential to
rotate, a cloud permission, an access grant, a decision only a person can
make — it goes under `Needs you:` instead, one line each. Never a story.

**Both** → the numbered list first, then `Needs you:`.

## Cite the finding key

A goal or security review gives every finding a key in square brackets, like
`[objective-drift-billing]`. **Put the key of the finding your action
addresses in the `(key: …)` slot.** That attaches your answer to the finding,
so the next review can see that you answered it — and whether what you proposed
ever happened.

If an action addresses no specific finding, write `(key: —)`.

## Prefer what has been raised before

A goal review separates `## New this cycle` from `## Escalating`. **Escalating
means it was raised before and nothing has changed.** Those come first: a
problem raised three times and still open matters more than one noticed today.
You do not need to filter repeats yourself — the review has already done it.

## Rules

- One sentence when possible; otherwise ≤5 one-line items. Don't restate the
  review, don't write an essay.
- Only a story or an errand. Everything else goes to the person.
- Proposal-only. You never create the work.
- Always reply on the goal thread.
- Security reviews: same rules. Code-fixable in the repo → a story or errand.
  Credential rotation, cloud IAM, anything outside the repo → `Needs you:`.
