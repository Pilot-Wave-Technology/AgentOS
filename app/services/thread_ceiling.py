"""How many stories one person may have in flight in one goal.

The rule this replaces was one. Not for any reason to do with the
machinery — the gateway has always keyed sessions per thread, the unique
index on `agent_instances` has always been per thread — but because the
assistant's thread ROTATED between stories, so a second story would have
displaced the first mid-conversation.

Stage K gives every story its own thread, which removes that reason. It
does not remove every reason. N concurrent stories is N executor
sandboxes and N model streams, and the conductor's admission cap is per
GOAL: nothing caps one person. Unlimited would mean the cost of a
misunderstanding ("start all of them") is bounded only by how many
stories exist.

So the guard stays in the same place and stops being a boolean.

FOUR. Chosen by the person who pays for it, not derived. It is the
number of things it is reasonable to be genuinely across at once, and it
leaves room for the ceiling to be raised once the shape has been used in
anger — raising a limit is a one-line change, lowering one strands work.

WHAT COUNTS. A thread whose story has not finished. Errands ride on the
same rows (an errand is a KIND of run against a story, chosen by the
assistant at `start_story`), so they are counted by the same query
without a special case — which is the point of counting threads rather
than enumerating kinds.

The one-story guard applies UNCHANGED to the operator being replaced.
Its thread still rotates, so a second story would still displace the
first; the ceiling would let it happen. See `refusal_for`.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

# Per user, per goal. Not per goal (that is the conductor's admission cap)
# and not per assistant (a person has one assistant, so it would be the
# same number wearing a misleading name).
DEFAULT_MAX_ACTIVE_THREADS = 4

CEILING_ENV = "ASSISTANT_MAX_ACTIVE_THREADS"


def max_active_threads() -> int:
    """The ceiling in force. Read per call, so a run can change it.

    Overridable, with the fallback spelled out above rather than hidden in
    a two-argument `environ.get` — the house rule, whose reason is that a
    default nobody can see is a policy nobody reviewed.

    WHY IT IS OVERRIDABLE AT ALL. Proving the refusal at four means running
    four stories, which is four sandboxes and four model streams to
    exercise one branch. Set it to 2 and the same code path refuses the
    third: the value is a policy, and the mechanism is what a test has to
    exercise. `test_ceiling_is_four` asserts the DEFAULT separately, so a
    value lowered for a test run cannot quietly become the shipped one.
    """
    raw = os.environ.get(CEILING_ENV) or ""
    if raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    if raw.strip():
        _log.warning(
            CEILING_ENV + "=%r is not a positive integer — "
            "using the default of %d", raw, DEFAULT_MAX_ACTIVE_THREADS)
    return DEFAULT_MAX_ACTIVE_THREADS


# Kept as a name because the tests and the prompt both talk about it.
MAX_ACTIVE_THREADS = DEFAULT_MAX_ACTIVE_THREADS

# IN FLIGHT means the story is being worked: in_progress, or blocked
# (which is reached only FROM in_progress and is not terminal, so it still
# occupies the person). Same pair the guard this replaces used.
#
# NOT "anything unfinished". A `proposed` or `accepted` story consumes no
# executor and no model stream, and counting it would put a goal over its
# ceiling before a single story had started — KER15 had three proposed
# stories the moment GO finished planning.
_IN_FLIGHT = ("in_progress", "blocked")


async def active_threads(pool, goal_id: str, user_uid: str,
                         excluding_story: str = "") -> list[dict]:
    """This person's stories in flight in this goal, newest first.

    COUNTED ON THE STORY, NOT THE THREAD, and the distinction is the whole
    correctness of this guard. **The board's Start button never binds a
    thread** — only an import does — so a thread-scoped count sees two
    board-started stories as zero and lets a person start as many as they
    like. The guard this replaced was keyed on `assigned_to` for exactly
    that reason, in a comment that says so; counting threads reintroduced
    the bug it was written about.

    So: assigned to them, OR they hold a thread for it. The second half
    catches a story assigned to somebody else that this person imported
    and is actually working.

    `excluding_story` is the story being started — resuming one must never
    conflict with itself, the same exclusion the old guard made.
    """
    rows = await pool.fetch(
        f"""SELECT DISTINCT ON (s.id)
                   s.id AS story_id, s.title, s.state,
                   t.id AS thread_id, s.updated_at
              FROM stories s
         LEFT JOIN threads t
                ON t.story_id = s.id AND t.goal_id = s.goal_id
               AND t.created_by = $2 AND t.left_goal_at IS NULL
             WHERE s.goal_id = $1
               AND s.id <> $3
               AND s.state IN {_IN_FLIGHT}
               AND (s.assigned_to = $2 OR t.id IS NOT NULL)
             ORDER BY s.id, s.updated_at DESC NULLS LAST""",
        goal_id, user_uid, excluding_story or "",
    )
    return sorted((dict(r) for r in rows),
                  key=lambda r: (r["updated_at"] is not None, r["updated_at"]),
                  reverse=True)


async def refusal_for(pool, goal_id: str, user_uid: str, story_id: str) -> str:
    """The reason to refuse starting `story_id`, or "" to allow it.

    One function for both entry points — the board transition and the
    assistant import — because a limit enforced in one and not the other
    is not a limit. Returns prose: every caller of this puts the string
    straight in front of a person.
    """
    from app.services.assistant_kind import is_kernel_assistant_for_user

    if not await is_kernel_assistant_for_user(pool, user_uid):
        # The operator rotates one thread between stories, so a second
        # story in flight does not get a second conversation — it takes
        # over the first one's. That is the behaviour the original guard
        # existed to prevent and it has not changed for this assistant.
        from app.services.story_transitions import find_conflicting_active_story
        conflict = await find_conflicting_active_story(
            pool, goal_id, user_uid, story_id)
        if not conflict:
            return ""
        return (
            f'You already have a story in progress in this goal: '
            f'"{conflict["title"]}". Finish or cancel it before starting another.'
        )

    ceiling = max_active_threads()
    live = await active_threads(pool, goal_id, user_uid, excluding_story=story_id)
    if len(live) < ceiling:
        return ""

    _log.info(
        "thread ceiling reached: uid=%s goal=%s has %d live thread(s) %s; "
        "refused story=%s (ceiling=%d)",
        user_uid, goal_id, len(live), [r["story_id"] for r in live], story_id,
        ceiling,
    )
    names = ", ".join(f'"{r["title"]}"' for r in live[:ceiling])
    return (
        f"You already have {len(live)} stories in flight in this goal, which is "
        f"the limit ({ceiling}): {names}. Finish or cancel one before "
        f"starting another — every story in flight is its own executor and its "
        f"own model stream."
    )
