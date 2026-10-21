"""Which of a story's threads does a card belong in?

PREFER THE PERSON'S THREAD; FALL BACK TO THE GO'S. Both halves matter,
and each was learned separately.

THE FIRST HALF. A story briefly had two threads: the assistant's, and
one opened for the Goal Orchestrator half a second later. Six card
posters each spelled out their own resolver:

    (SELECT th.id FROM threads th
      WHERE th.story_id = e.story_id AND th.left_goal_at IS NULL
      ORDER BY th.created_at DESC LIMIT 1)

`ORDER BY created_at DESC` duly started returning the GO's. Every plan
approval, every answer, every node-error card moved out of the thread
the person was reading and into a machine log they had no reason to
open -- in the database, correct, and invisible. Observed on feature
with story 3a1095ea: two `kernel_park` cards in the GO thread, nothing
in the assistant's. Nothing errored and nothing logged.

The per-story GO thread has since been removed -- a second thread per
story meant a second Slack channel per story -- but the preference
stays, because the thing it guards against is any future second thread,
not that one specifically.

THE SECOND HALF, AND WHY THIS IS A PREFERENCE AND NOT A FILTER. An
AUTONOMOUS ERRAND's thread is GO-owned too (`errand_grants.
_open_story_thread` puts the goal's `go_agent_id` on it), and it is the
ONLY thread such a story has -- nobody holds an assistant conversation
about an errand they did not ask for. A resolver that simply excluded
`go-` threads would return NULL for every errand and post its cards
nowhere: the same silent disappearance, from the other direction.

So: order by whether the thread is GO-owned, then by recency. A human
thread wins when there is one; the GO's is taken when it is all there
is.

AND IT LIVES IN ONE PLACE. Six copies is how a resolver gets fixed in
five of them.
"""

from __future__ import annotations

# How a GO-owned thread is spelled, both for the goal's orchestrator and
# for an errand thread.
GO_AGENT_PREFIX = "go-"


def newest_human_thread(story_expr: str) -> str:
    """SQL scalar subquery: this story's thread, the person's preferred.

    `story_expr` is whatever names the story id in the enclosing query --
    `e.story_id`, `s.id`, a bind parameter. The result is parenthesised
    and safe to drop into a COALESCE.

    Callers pass a column reference or a bind parameter, never user
    input: this is string-built SQL and interpolating a value would be
    an injection. Every call site in the tree passes a literal.
    """
    return (
        "(SELECT th.id FROM threads th\n"
        f"           WHERE th.story_id = {story_expr}\n"
        "             AND th.left_goal_at IS NULL\n"
        "           -- A GO-owned thread LAST, never excluded: an\n"
        "           -- errand has no other thread. See module docstring.\n"
        "           ORDER BY (COALESCE(th.agent_id, '') LIKE "
        f"'{GO_AGENT_PREFIX}%') ASC,\n"
        "                    th.created_at DESC\n"
        "           LIMIT 1)"
    )
