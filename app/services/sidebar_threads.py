"""The assistant's story threads, as sidebar rows.

One assistant, several stories, a thread each — so the sidebar needs a
list where it used to need a link. Rendered as a collapsible child list
under the assistant channel, the same convention the goal-thread's
sub-channels already use, and named after the story or errand it runs
rather than after its conversation: the id is what the person says out
loud and what every tool answer is keyed by.

The marker on each row answers the only question worth answering from a
collapsed list: *is anything waiting on me?* A parked run is invisible
otherwise — the story still reads `in_progress`, because it is; it is
the executor that has stopped, holding a question or a plan.
"""
from __future__ import annotations

import html as _html
import logging

_log = logging.getLogger(__name__)

# Beyond this the list stops being a list and starts being a page. The
# ceiling is four live threads, so this is finished ones as well.
MAX_ROWS = 12

_FINISHED = ("done", "cancelled", "rejected", "reviewed")


async def assistant_story_threads(
    pool, goal_id: str, agent_id: str, user_uid: str,
) -> list[dict]:
    """This person's story threads on this assistant, live ones first.

    Each row carries `waiting` — whether an executor on that story has
    parked on a question or a plan approval. That fact lives in the
    kernel schema; a control panel deployed without it must still render
    a sidebar, so the lookup fails soft to "nothing waiting" rather than
    taking the navigation down with it.
    """
    rows = await pool.fetch(
        f"""SELECT t.id, t.story_id, s.title, s.short_title, s.state
              FROM threads t
              JOIN stories s ON s.id = t.story_id
             WHERE t.goal_id = $1
               AND t.agent_id = $2
               AND t.created_by = $3
               AND t.left_goal_at IS NULL
               AND (t.status IS NULL OR t.status = ''
                    OR t.status IN ('active', 'open'))
             ORDER BY (s.state NOT IN {_FINISHED}) DESC, t.created_at DESC
             LIMIT {MAX_ROWS}""",
        goal_id, agent_id, user_uid,
    )
    out = [dict(r) for r in rows]
    if not out:
        return out
    # SHARED WITH THE GO'S LIST, deliberately: two copies of a fail-soft
    # kernel lookup is how one of them quietly stops failing soft.
    return await _mark_waiting(pool, goal_id, out)


def sidebar_label(row: dict) -> str:
    """What the sidebar row SAYS. Never a bare id.

    THIS IS WHERE THE SIX-WORD TITLE WAS MISSING, and the short-title
    work did not touch it. That work set `threads.title` through
    `label_for`, and these two lists do not read `threads.title` -- they
    join the story and rendered a hardcoded `# {story_id}`, with the
    real title only in the hover tooltip. So every story and errand row
    in the sidebar read as eight hex characters however good the label
    stored against it was, which is the exact complaint the feature was
    built to answer.

    READ-TIME, not a stored column, and that is the point: it corrects
    every row that already exists as well as every new one, with no
    backfill and nothing to keep in step.
    """
    from kernel.go.short_title import label_for  # noqa: PLC0415

    return label_for(row.get("short_title"), row.get("title"),
                     row.get("story_id") or "")


def story_thread_row(
    goal_id: str, agent_id: str, row: dict, current_thread: str,
    unread_counts: dict, previews: dict,
) -> str:
    """One sidebar row for one story thread."""
    from app.routers.goals import _unread_badge  # noqa: PLC0415  (cycle)

    tid = row["id"]
    story_id = row["story_id"]
    is_current = tid == current_thread

    if row.get("waiting"):
        dot = "text-amber-400"
        state_word = "waiting on you"
    elif row.get("finished"):
        dot = "text-gray-700"
        state_word = row["state"]
    else:
        dot = "text-violet-400"
        state_word = row["state"]

    if is_current:
        link_cls = "text-violet-400 bg-violet-500/10 font-medium shca-thread-current"
    elif row.get("finished"):
        link_cls = "text-gray-600 hover:text-gray-400"
    else:
        link_cls = "text-gray-500 hover:text-gray-300"

    title = (row.get("title") or "").strip()
    tooltip = _html.escape(f"{story_id} — {title} ({state_word})" if title
                           else f"{story_id} ({state_word})")
    # An amber ring, not just an amber dot: the row has to read as "act on
    # me" from a collapsed list without anyone hovering it.
    ring = (' <span class="text-amber-400 font-bold">!</span>'
            if row.get("waiting") else "")
    return f'''
    <a href="/goals/{goal_id}/chat/{agent_id}?thread_id={tid}"
       data-tooltip-title="{tooltip}"
       data-tooltip-preview="{previews.get(tid, "")}"
       class="sidebar-item text-2xs py-1 {link_cls}">
      <span class="thread-icon-wrap">
        <svg class="w-3 h-3 flex-shrink-0 {dot}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
        </svg>
      </span>
      <span class="sidebar-label truncate"># {_html.escape(sidebar_label(row))}{ring}</span>
      {_unread_badge(tid, unread_counts.get(tid, 0))}
    </a>'''


# ── the GO's errand threads ───────────────────────────────────────────
#
# WHY THESE ARE NOT ASSISTANT ROWS. An approved autonomous errand opens a
# thread on the GO, not on the approver's assistant, because the GO is
# what drives it. So they cannot come from `assistant_story_threads`,
# which is keyed by the assistant's agent id, and they must not be
# rendered by `story_thread_row`, whose href points at the assistant
# chat page -- a GO conversation opened there would relay the wrong
# agent's history and post the GO's replies into somebody else's thread.
#
# WHERE THEY GO. Under the GO row, beside Agent Mesh, Goal Review Council
# and Security Council. Those three are DIFFERENT IN KIND and stay as
# they are: each is a fixed channel of the goal that folds into the GO's
# MAIN session and context. An errand thread is per-errand, comes and
# goes with the errand, and has a gateway session of its own.
#
# THE SHARED PIECES ARE SHARED ON PURPOSE. Same `_FINISHED` set, same
# waiting lookup, same fail-soft -- so a parked errand reads exactly like
# a parked story and neither drifts from the other.

async def go_errand_threads(pool, goal_id: str, go_agent_id: str) -> list[dict]:
    """Every live errand thread on this goal's GO, live ones first.

    NOT SCOPED TO ONE PERSON, unlike the assistant's list. An assistant
    thread is a private conversation between one member and their own
    agent; the GO belongs to the goal, and an errand it is running is the
    goal's business. Any member who can see the goal thread can see
    these -- and the approver is still the principal on every run.
    """
    if not go_agent_id:
        return []
    rows = await pool.fetch(
        f"""SELECT t.id, t.story_id, s.title, s.short_title, s.state
              FROM threads t
              JOIN stories s ON s.id = t.story_id
             WHERE t.goal_id = $1
               AND lower(t.agent_id) = lower($2)
               AND t.left_goal_at IS NULL
               AND COALESCE(s.work_kind, 'story') = 'errand'
               AND (t.status IS NULL OR t.status = ''
                    OR t.status IN ('active', 'open'))
             ORDER BY (s.state NOT IN {_FINISHED}) DESC, t.created_at DESC
             LIMIT {MAX_ROWS}""",
        goal_id, go_agent_id,
    )
    out = [dict(r) for r in rows]
    if not out:
        return out
    out = await _mark_waiting(pool, goal_id, out)
    return out


async def _mark_waiting(pool, goal_id: str, rows: list[dict]) -> list[dict]:
    """Stamp `waiting` and `finished` on each row.

    Lifted out of `assistant_story_threads` when the GO grew a list of
    its own: two copies of a fail-soft kernel lookup is how one of them
    quietly stops failing soft.
    """
    waiting: set = set()
    try:
        wait_rows = await pool.fetch(
            # `questions`, not `human_requests`. The E6h migration named
            # the latter and the code moved to `questions`; the table
            # does not exist, so this lookup raised every time and the
            # except below swallowed it -- meaning no story has ever
            # been marked `waiting` in the sidebar. `kernel/knowledge/
            # read.py` carries the same warning for the same reason.
            # Status is 'OPEN': the column defaults to upper case.
            """SELECT DISTINCT e.story_id
                 FROM kernel.questions q
                 JOIN kernel.executions e ON e.id = q.execution_id
                WHERE q.status = 'OPEN'
                  AND e.story_id = ANY($1::text[])""",
            [r["story_id"] for r in rows],
        )
        waiting = {r["story_id"] for r in wait_rows}
    except Exception as exc:
        _log.debug("sidebar: parked-run lookup unavailable (goal=%s): %s",
                   goal_id, exc)
    for r in rows:
        r["waiting"] = r["story_id"] in waiting
        r["finished"] = r["state"] in _FINISHED
    return rows


def errand_thread_row(
    goal_id: str, row: dict, current_thread: str,
    unread_counts: dict, previews: dict,
) -> str:
    """One sidebar row for one GO errand thread.

    The href is the GO thread page with the thread named -- `/thread`,
    never `/chat/{agent}`. See `app/routers/go_thread_pick.py` for what
    that route checks before it renders one.
    """
    from app.routers.goals import _unread_badge  # noqa: PLC0415  (cycle)

    tid = row["id"]
    story_id = row["story_id"]
    is_current = tid == current_thread

    if row.get("waiting"):
        dot, state_word = "text-amber-400", "waiting on you"
    elif row.get("finished"):
        dot, state_word = "text-gray-700", row["state"]
    else:
        # Emerald, because this is a GO conversation and the GO row above
        # it is emerald. The assistant's story rows are violet for the
        # same reason, and the colour is the only thing in a collapsed
        # sidebar that says which agent you are about to talk to.
        dot, state_word = "text-emerald-400", row["state"]

    if is_current:
        link_cls = "text-emerald-400 bg-emerald-500/10 font-medium"
    elif row.get("finished"):
        link_cls = "text-gray-600 hover:text-gray-400"
    else:
        link_cls = "text-gray-500 hover:text-gray-300"

    title = (row.get("title") or "").strip()
    tooltip = _html.escape(f"{story_id} — {title} ({state_word})" if title
                           else f"{story_id} ({state_word})")
    ring = (' <span class="text-amber-400 font-bold">!</span>'
            if row.get("waiting") else "")
    return f'''
    <a href="/goals/{goal_id}/thread?thread_id={tid}"
       data-tooltip-title="{tooltip}"
       data-tooltip-preview="{previews.get(tid, "")}"
       class="sidebar-item text-2xs py-1 {link_cls}">
      <span class="thread-icon-wrap">
        <svg class="w-3 h-3 flex-shrink-0 {dot}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13 10V3L4 14h7v7l9-11h-7z"/>
        </svg>
      </span>
      <span class="sidebar-label truncate"># {_html.escape(sidebar_label(row))}{ring}</span>
      {_unread_badge(tid, unread_counts.get(tid, 0))}
    </a>'''
