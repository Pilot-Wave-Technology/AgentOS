"""Has a story's kickoff message ever been sent?

Sole owner of this predicate, shared by two enforcement points for the
multi-repo first-execution fix (`research/MULTI_REPO_STORIES_PLAN.md` §12):

  * `app/routers/assistant.py` `dispatch_story_kickoff_to_assistant` — decides
    whether a `workspace_state == 'ready'` story may still be re-provisioned to
    add a write repo whose branch arrived late.
  * `app/services/resource_allocation.py` `grant_resource` — refuses a *new*
    write-repo grant once kickoff has been sent, so the case above can only ever
    fire when nothing has been worked on yet.

**Why kickoff, specifically, is the line.** `provision_story_workspace`'s clone
step is destructive on every call — the gateway's `shca.workspace.setup` nukes
and re-clones every repo subdir to stay idempotent. Before kickoff is sent, that
is harmless: the assistant has not been told to do anything, so nothing exists to
lose. After kickoff, the assistant may have real, uncommitted work in the clone.
Kickoff is therefore the one moment that cleanly separates "safe to redo" from
"unsafe to redo," and both enforcement points key off exactly that moment.

**Why unwindowed.** `dispatch_story_kickoff_to_assistant` already runs a similar
marker check, but scoped to the last 2 minutes — that one exists purely to guard
against a double-fired *concurrent* kickoff, not to answer "did this ever happen."
This predicate answers the latter, so it carries no time window at all: a kickoff
sent hours ago still means real work may exist.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def story_has_had_kickoff(pool, story_id: str, thread_id: str = "") -> bool:
    """Was this story's kickoff marker ever posted, in ANY of its threads?

    STORY-scoped deliberately. This predicate guards a STORY-scoped destructive
    action — `provision_story_workspace`'s clone step nukes and re-clones every repo
    subdir — so scoping the guard itself to ONE thread let it fail open: a story whose
    conversation had fragmented across threads read as "never kicked off" in each new
    one, and every fresh assistant re-cloned a workspace that already held real work.

    Fragmentation is expected, not exotic: `migrations/threads_story_id.sql` exists
    precisely because `stories.active_thread_id` drifts or clears and a fresh thread is
    minted when it does. A guard whose scope is narrower than the damage it prevents is
    not a guard. Confirmed live 2026-08-25 on story e97ea4fb, where the pinned
    incomplete-write-repo-set predicate re-entered this branch on every new assistant.

    No time window — a kickoff from any point in the story's history counts.

    `thread_id` is an optional EXTRA candidate, covering a thread not yet bound through
    `threads.story_id` (that column is newer than some live threads). Supplying it can
    only widen the search, so it can only ever make the answer MORE conservative.
    """
    if not story_id:
        return False
    rows = await pool.fetch("SELECT id FROM threads WHERE story_id = $1", story_id)
    candidates = {r["id"] for r in rows}
    if thread_id:
        candidates.add(thread_id)
    if not candidates:
        return False
    # Both sides are indexed: `idx_threads_story` above, `idx_messages_thread`
    # (thread_id, created_at) here — so this never degrades into a scan of `messages`,
    # which carries no index on `content`.
    row = await pool.fetchval(
        """SELECT 1 FROM messages
            WHERE thread_id = ANY($1::text[])
              AND content LIKE '# Story Mode Active%'
              AND content LIKE '%' || $2 || '%'
            LIMIT 1""",
        list(candidates), story_id,
    )
    return bool(row)


async def thread_has_kickoff(pool, thread_id: str, story_id: str) -> bool:
    """Has THIS thread already been told about this story?

    Deliberately a different question from `story_has_had_kickoff`, and the two must
    not be collapsed:

      * `story_has_had_kickoff` (STORY-scoped) answers "may real work exist anywhere?"
        It guards the DESTRUCTIVE re-clone, so it must see every thread.
      * this one (THREAD-scoped) answers "does this conversation already contain the
        marker?" It guards POSTING A MESSAGE, so it must be scoped to the thread that
        would receive it — a story rotated onto a fresh assistant genuinely needs its
        kickoff there, even though the story has been kicked off before.

    Unwindowed, unlike `dispatch_story_kickoff_to_assistant`'s 2-minute check. That
    window exists only to collapse CONCURRENT double-fires and is useless against a
    periodic caller: `branch_ready_listener` polls every 45s, so two of every three
    passes fall inside the window and the third lands outside it — which is exactly
    the ~135s kickoff cadence observed live on 2026-08-25 (stories 476439fd, 43c40354).
    A time window cannot express "this thread already knows"; only the marker can.
    """
    if not thread_id or not story_id:
        return False
    row = await pool.fetchval(
        """SELECT 1 FROM messages
            WHERE thread_id = $1
              AND content LIKE '# Story Mode Active%'
              AND content LIKE '%' || $2 || '%'
            LIMIT 1""",
        thread_id, story_id,
    )
    return bool(row)
