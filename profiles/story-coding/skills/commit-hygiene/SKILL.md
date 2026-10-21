---
name: commit-hygiene
description: How to size and word commits so a killed leg loses as little as possible
---

# Commit hygiene

A leg can end at any step — budget, wall clock, or a kill you will not see
coming. Only committed work survives it. Everything else is recoverable only
if the workspace happens to outlive the process, which is not a guarantee the
harness makes.

## Size

One commit per coherent change. If you have made an edit that would be worth
keeping on its own, commit it before starting the next one. A commit that
touches four unrelated things cannot be partly kept.

Do not batch commits to the end of the leg. The end of the leg is the moment
you are least likely to reach.

## Wording

State what changed and why, not what file you edited — the diff already says
the file. A reader of the brief on the next leg sees only your message.

Bad:  `update counter.py`
Good: `Counter.decrement mirrors increment, returning the new value`

## Empty diffs

`story_commit` refuses an empty diff with exit code 1. That is not an error to
work around: it means the edit you thought you made did not land. Read the
file before assuming otherwise.
