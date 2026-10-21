"""Wake GO about a running autonomous errand — only when there is something.

WHY NOT A TIMER, which is the obvious design and the wrong one. Every
wake is a whole GO turn: `enqueue_system_wake` queues it FCFS and the
model reads its context to answer. At a 500K window that is not free,
and a heartbeat spends it on runs where nothing happened. The cost has
to be proportional to EVENTS, not to how long an errand takes.

So: a closed set of things worth a turn, and an empty digest means no
wake at all. A two-hour errand that simply works produces zero.

WHAT IS WORTH A TURN. Only what GO can act on:

    a question GO may settle       authorization / clarification
    a job that has gone quiet      still running, last byte an hour ago
    a leg that failed              the run needs steering or stopping

and deliberately NOT every step, tool call or journal line. Those are
what `story_progress` is for when GO decides to look.

WHAT IS NOT WORTH A TURN. A park only a person can settle -- an
`acceptance` (done or more work), a `direction`, a `permission` for one
destructive call. GO cannot answer those, the person already has the
card, and waking GO to watch somebody else's decision is the purest
form of the waste this module exists to avoid.

COALESCED BY THE TABLE THAT ALREADY DEDUPES. `go_wake_log` is unique on
(goal_id, kind, ref); a `ref` carrying a time bucket makes at most one
progress wake per execution per window however many events land inside
it. Nothing new to keep in step, and a restart mid-window cannot double
up.

THE DIGEST TRAVELS WITH THE WAKE. A wake that says only "something
happened" costs a turn and then buys three tool calls to find out what
-- more expensive than the wake. What GO needs to decide is in the
message.
"""

from __future__ import annotations

import logging

from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS

log = logging.getLogger(__name__)

# How often we LOOK. Not how often we wake -- see `errand_checkin`.
INTERVAL_S = 60.0

# HOW LONG BETWEEN CHECK-INS is no longer a constant here: it is an
# operator setting, read fresh each sweep, and the gate is elapsed time
# since this run's last wake rather than a slice of the wall clock. Both
# of those live in `app/services/errand_checkin` with the reasoning.
#
# What is left here is what to SAY, which is this module's job.

# A job still marked running whose last byte was this long ago. One hour,
# because that is the number GO's own prompt names: "a job still marked
# running whose last byte was an hour ago is compute that has gone, not
# work in progress". Two definitions of the same signal would disagree.
SILENT_JOB_S = 3600

# A PERSON'S, and so not worth waking GO about. DERIVED from the
# kernel's own `HUMAN_ANSWERED_KINDS` rather than listed again here --
# that set lives in the kernel on the stated grounds that "a fact about
# kernel data that the kernel does not own is a fact that drifts", and a
# second copy in the control panel is exactly that fact, owned twice.
#
# Deriving also fails in the safe direction: a new human-only kind added
# to the kernel set is excluded here automatically, so GO is woken LESS
# rather than woken about something it cannot settle.
#
# `authorization` IS LIFTED OUT, and it is the one deliberate exception.
# A plan approval is human-only for a story and for a hand-started
# errand, and stays so. On an AUTONOMOUS errand GO may now approve it or
# send it back -- that is the authority this build adds, and being woken
# for it is the whole point of watching. The `_LIVE` query is what keeps
# the exception to autonomous errands: nothing else reaches this filter.
HUMAN_ONLY = frozenset(HUMAN_ANSWERED_KINDS) - {"authorization"}

# Live autonomous errands: an errand, not terminal, with a grant. The
# grant clause is `kernel/go/drivable._HAS_GRANT` verbatim -- two
# definitions of "autonomous" would disagree the first time one moved,
# and the disagreement would be GO woken about work it may not touch.
# EVERY KERNEL TABLE IS QUALIFIED, and `run_state_machine` was not.
# This module runs on the APP's pool, which has no `kernel` in its
# search_path -- the conductor's does, which is why the same unqualified
# name is fine in `kernel/conductor/admit.py` and fatal here. The sweep
# raised `UndefinedTableError` on every tick from the moment it shipped,
# the caller's guard turned that into one warning line, and the wake
# never fired once. Found by running it, not by the suite.
_LIVE = """
    SELECT e.id, e.goal_id, e.story_id, e.state, s.title,
           -- `stall_reason` IS THE STALL. `stall_card_note` is a
           -- BOOKKEEPING MIRROR of what the card poster has already
           -- told somebody about -- it exists so
           -- `stall_reason IS DISTINCT FROM stall_card_note` can detect
           -- a CHANGE -- and it is not cleared when the stall lifts.
           --
           -- Reading the mirror was wrong twice over. Run 310 finished
           -- installing and came up RUNNING with `stall_reason` NULL
           -- and the note still reading "installing: its environment is
           -- still installing" -- so GO would have been told a healthy
           -- run had stalled, for a reason that had already resolved.
           -- Across the table the two disagree on real rows.
           e.stall_reason,
           EXTRACT(EPOCH FROM (now() - e.stalled_at))::int AS stalled_s,
           -- HOW OLD THE RUN IS, for the first check-in. Without it a
           -- run never checked in had no clock at all and was due at
           -- once; see `errand_checkin.is_due`.
           EXTRACT(EPOCH FROM (now() - e.created_at))::int AS age_s
      FROM kernel.executions e
      JOIN stories s ON s.id = e.story_id
      JOIN kernel.run_state_machine m ON m.version = e.machine_version
     WHERE COALESCE(s.work_kind, 'story') = 'errand'
       AND NOT (m.terminal_states @> to_jsonb(e.state))
       AND EXISTS (
             SELECT 1 FROM kernel.errand_schedules g
              WHERE g.goal_id = e.goal_id
                AND g.approved_by IS NOT NULL
                AND g.revoked_at IS NULL
                AND (g.expires_at IS NULL OR g.expires_at > now())
                AND (g.story_id IS NULL OR g.story_id = e.story_id))
"""


async def _open_questions(pool, eid: int) -> list:
    return [dict(r) for r in await pool.fetch(
        "SELECT id, kind, question_text FROM kernel.questions "
        " WHERE execution_id = $1 AND status = 'OPEN' "
        "   AND kind <> ALL($2::text[]) ORDER BY id",
        eid, sorted(HUMAN_ONLY))]


async def _silent_jobs(pool, eid: int) -> list:
    """Running, and nothing out of it for an hour."""
    return [dict(r) for r in await pool.fetch(
        "SELECT job_id, command, "
        "       EXTRACT(EPOCH FROM (now() - last_seen_at))::int AS quiet_s "
        "  FROM kernel.execution_jobs "
        " WHERE execution_id = $1 AND running "
        "   AND last_seen_at < now() - ($2 || ' seconds')::interval "
        " ORDER BY last_seen_at", eid, str(SILENT_JOB_S))]


def _lines(parts: list[str]) -> str:
    """The digest lines, one per line."""
    return chr(10).join(parts)


async def digest(pool, row, window: int, quiet: int | None) -> str:
    """What GO should be told, or "" when the answer is nothing.

    EMPTY IS THE COMMON CASE and the point of the whole module. An
    errand mid-suite with no open question and a job that is talking is
    working exactly as it should, and has nothing to say.
    """
    from kernel.go.wake_format import approver

    from app.services import errand_checkin

    eid = row["id"]
    who = await approver(pool, row["goal_id"], row["story_id"])
    parts: list[str] = []

    opens = await _open_questions(pool, eid)
    for q in opens:
        word = ("a plan is waiting for your approval"
                if q["kind"] == "authorization" else "a question is open")
        parts.append(f"- {word} (question {q['id']}): "
                     f"{(q['question_text'] or '').strip()[:300]}")

    for j in await _silent_jobs(pool, eid):
        parts.append(
            f"- job `{j['job_id']}` has produced nothing for "
            f"{j['quiet_s'] // 60} minutes and is still marked running: "
            f"`{(j['command'] or '')[:120]}`. That is the shape of compute "
            f"that has gone, not of work in progress.")

    if row["state"] == "FAILED":
        parts.append("- the run has FAILED")

    # ── stalled, and the digest used to say the opposite ─────────────
    #
    # RUN 308. `stalled_at` was set at 15:35:30 with `stall_card_note`
    # of `provider_unavailable`, and TWELVE consecutive check-ins told
    # GO "it is still running and you have not checked in for 1
    # minute". GO had already found the truth itself through
    # `story_progress` and been reassured eleven times since.
    #
    # A wake that misinforms is worse than no wake: GO stops trusting
    # the ones that are right, and the whole point of the digest is
    # that it is what GO acts on without looking further.
    stalled = bool(row.get("stall_reason"))
    # ── it has not started yet, which is not the same as stalled ─────
    #
    # RUN 345. A pod that cannot be scheduled sets `stall_reason` while
    # the run is still DISPATCHING, and the line below then told GO
    # "that is not work in progress" -- true of a stalled run, false of
    # a queued one. GO asked the right question, got no answer (there is
    # no executor to answer before the pod exists), and cancelled at
    # eighty seconds. `promote_row` allows ninety.
    #
    # WAITING IS REPORTED, NOT HIDDEN: ten minutes unplaced is worth
    # knowing. What changes is that it is described as a queue, and that
    # the one thing GO might reach for is ruled out in the same breath --
    # cancelling frees no capacity, so it cannot help.
    if stalled and row["state"] in errand_checkin.PRE_START:
        mins = max(1, (row.get("stalled_s") or 0) // 60)
        parts.append(
            f"- it has NOT STARTED yet: {mins} minute(s) waiting for the "
            f"cluster to place its pod (`{row['stall_reason']}`). Nothing "
            f"has gone wrong and there is no executor to ask -- the pod "
            f"does not exist. Capacity arrives when a node frees or the "
            f"pool scales. Cancelling would not free any, so it buys "
            f"nothing; say it is queued and leave it.")
    elif stalled:
        mins = max(1, (row.get("stalled_s") or 0) // 60)
        # FACTS ONLY. What to do about it is the imperative's job, and
        # this line used to carry both -- so the stall wake said the
        # same thing twice, once as news and once as an instruction.
        parts.append(
            f"- it was flagged STALLED {mins} minute(s) ago"
            + (f": `{row['stall_reason']}`"
               if row.get("stall_reason") else "")
            + ". That is not work in progress.")

    # ── the check-in, and it is the one TIMED trigger here ───────────
    #
    # EVERY OTHER TRIGGER FIRES ON AN EVENT, which was the whole design:
    # cost proportional to what happens, so a clean errand costs
    # nothing. That left one real gap, named when this was written -- a
    # long errand progressing normally but heading somewhere useless
    # produces no wake, and GO cannot steer what it never looks at.
    #
    # RUNNING ONLY. A PARKED run is waiting on somebody else's decision;
    # there is nothing for GO to ask about, and waking it to watch is
    # the waste this module exists to avoid.
    #
    # WHAT IT COSTS, said plainly because a timer is what the rest of
    # this file argues against: four turns an hour per running errand.
    # That is the price of GO noticing a run going the wrong way.
    #
    # ASK, DO NOT STEER -- and that instruction rides in the wake text
    # rather than living only in the prompt, because this is the moment
    # it applies. An executor forty steps in knows things the journal
    # does not; a steer sent before asking is a guess with authority
    # behind it.
    # NOT WHEN STALLED. "Still running" and "stalled" are contradictory
    # and the run can carry both; printing them together is how GO came
    # to read past the one that mattered.
    if row["state"] == "RUNNING" and not stalled:
        # SHORT, AND THE PROCEDURE IS NOT HERE. The four numbered steps
        # used to live in this string AND in `AGENTS.md`, which is how
        # they came to disagree -- and every check-in wrote all twenty
        # lines into GO's context permanently, four times an hour, to
        # repeat something it had read at the start of the session.
        #
        # What stays is what GO cannot get without a tool call: how long
        # it has been, and the one imperative naming the tool.
        since = ("since this errand started" if quiet is None
                 else f"for {max(1, quiet // 60)} minutes")
        parts.append(f"- nothing has been reported {since}")
    if not parts:
        return ""
    # ONE SHAPE FOR EVERY ERRAND WAKE -- see `kernel/go/wake_format`.
    # The old tail ended "[NO_REPLY]", which is how a check-in came to
    # cost a turn and produce nothing: run 308 approved a plan and said
    # nothing at all. An errand wake always gets a reply, addressed to
    # the person who is not watching.
    from kernel.go.wake_format import wake

    # A STALL IS NOT A CHECK-IN. Headed `[ERRAND CHECK-IN]` and carrying
    # the ask-and-compare imperative, the message told GO in one breath
    # that the run had stopped and that it should test its progress
    # against the plan. The header is the first thing read and it was
    # naming the wrong situation.
    if stalled:
        return wake(
            header="ERRAND STALLED",
            story=row["story_id"], title=row["title"] or "",
            approver=who,
            happened=_lines(parts),
            evidence="",
            imperative=(
                "Find out whether it is recovering or gone -- ask the "
                "executor, and `story_progress` for what it last did. If "
                "the compute has gone, say so and `run_control` it: stop "
                "or restart. Do not wait it out."),
        )

    # AN OPEN QUESTION IS NOT A CHECK-IN EITHER. A run parked on an
    # authorization produces a digest, so it took the check-in header
    # and the check-in imperative -- telling GO to ask the executor how
    # it is going when the run has stopped and is waiting for GO to
    # decide. The header is the first thing read; it has to name the
    # situation the message is about.
    if opens:
        return wake(
            header="ERRAND WAITING ON YOU",
            story=row["story_id"], title=row["title"] or "",
            approver=who,
            happened=_lines(parts),
            evidence="",
            imperative=(
                "Settle what is named above -- `decide_plan` for a plan, "
                "`relay_answer` for a question. The run is stopped until "
                "you do. If you genuinely cannot, say what you need from "
                "a person and name them."),
        )

    return wake(
        header="ERRAND CHECK-IN",
        story=row["story_id"], title=row["title"] or "",
        approver=who,
        happened=_lines(parts),
        evidence="",
        imperative=(
            # "WAIT FOR ITS ANSWER" WAS IMPOSSIBLE. `ask_executor` is
            # asynchronous: it returns the moment it has proposed the
            # question, and the executor replies at its next step
            # boundary -- four and a half minutes, on one measured run.
            # GO cannot wait inside the turn, so the line asked for
            # something it could not do and invited the journal read it
            # was written to prevent.
            "Ask the executor what is happening (`ask_executor`). The "
            "answer does not arrive in this turn -- `[EXECUTOR REPLIED]` "
            "will wake you with it -- so say what you know now and that "
            "you are waiting. The journal (`story_progress`) is an "
            "interim only, and say so when you use it. Test the real "
            "answer against the approved plan when it lands, and `steer` "
            "it back if it has drifted."),
    )


async def sweep(pool) -> int:
    """One pass. Returns how many wakes went out."""
    from app.services.go_wake_events import _deliver, _errand_thread

    from app.services import errand_checkin

    sent = 0
    window = await errand_checkin.window_s()
    for row in await pool.fetch(_LIVE):
        try:
            quiet = await errand_checkin.quiet_s(
                pool, row["goal_id"], row["id"])
            # NOT DUE is the common case and costs one cheap query. The
            # gate is on the WHOLE digest, not the check-in line alone:
            # a FAILED run has the same thing to say every sweep, and
            # saying it every minute is the waste this module exists to
            # avoid.
            if not errand_checkin.is_due(quiet, row["age_s"], window):
                continue
            body = await digest(pool, row, window, quiet)
            if not body:
                continue
            # THE ERRAND'S OWN THREAD, not the goal's main one. An errand
            # parks and asks many times over its life, and `_deliver`'s
            # own docstring records what happened when those all landed
            # in the main session: they spend the window of the one
            # conversation a person actually reads, and which has to
            # last the goal. Falls back to the main thread only when the
            # errand has none -- a wake nobody sees is worse than a wake
            # in the wrong place.
            thread = await _errand_thread(pool, row["id"])
            if await _deliver(pool, row["goal_id"], "errand_progress",
                              errand_checkin.ref(row["id"]), body, thread):
                sent += 1
        except Exception:                                   # noqa: BLE001
            # One errand must not stop the sweep: the others are the
            # reason it runs at all.
            log.exception("errand watch: digest failed for execution %s",
                          row["id"])
    return sent
