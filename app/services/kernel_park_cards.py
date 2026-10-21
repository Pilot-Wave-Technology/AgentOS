"""A parked executor announces itself in the story thread.

THE PROBLEM THIS SOLVES. When the executor parks for plan approval it
writes a `kernel.questions` row and stops. Nothing carried that into the
place the person actually is. The assistant could see the question
through its `inbox` tool and would faithfully report *"parked on
question 197 -- approve it in the dashboard"*, and the dashboard it
meant was the E0 console, which exists to drive tests. So the person
was told to leave the conversation for a page that was never meant to
be the product, to approve a plan whose text nobody had shown them.

The thread is the surface. A parked run posts an approval card into the
story's own thread, with the plan in it and buttons on it, and the
person approves where they were already reading.

WHY A POLLER AND NOT THE NOTIFY. `kernel.questions` does NOTIFY on
insert and the kernel's own notifier listens. This does not reuse it:
that listener lives in the E0 container next to the worker, and the
control panel is a separate process that may restart, deploy, or simply
not have been running when the park happened. A poll over
`card_posted_at IS NULL` catches every question whenever it starts,
which a missed NOTIFY does not. Same discipline as the git engine's
poll backstop.

AND IT POSTS A QUESTION ALREADY ANSWERED, which is not a contradiction
of that. The filter was `status = 'OPEN'`, so a question settled before
the poller's next pass never got a card AT ALL -- and GO settles a plan
in about thirteen seconds, well inside one pass. Measured on ERR17:
questions 411, 412, 414 and 415 all have card rows; 413 and 403, both
`authorization`, have none and never will, because `card_posted_at IS
NULL` is a one-shot claim and the status had already moved.

WHAT THAT LOOKED LIKE, and it was reported as a persistence bug three
times: the plan-approval card appeared live -- the broadcaster pushes
at park time, before anybody answers -- and was gone after a reload,
because there was no row to render. The one card GO reliably answers
within seconds was the one card that always vanished.

A CARD IS ALSO A RECORD. What the executor asked and what was decided
belongs in the transcript whether or not anybody still has to act, and
a thread that silently omits the questions that were answered quickly
tells a reader the opposite of what happened. `_post` stamps `decided`
on those, the same key the errand proposal uses, so they render in
their settled state with no live buttons.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from app.database import get_pool

log = logging.getLogger(__name__)

INTERVAL_S = 5.0

# Approval is a HUMAN act (H8a). The card says so on its face rather
# than letting a person discover it by clicking a button the kernel
# then refuses -- and the assistant is told the same thing by `inbox`,
# so the two agree.
# Ec3b/I5a — ONE DECLARATION, in the kernel beside the question kinds.
#
# `permission` was missed here when the kind was added, which made every
# destructive-command park unanswerable: `needs_human` came back False,
# `options` came back empty, and the client renders no buttons at all for
# a card that needs none. Five places carried this fact and three were
# wrong; it is `kernel.mailbox.schema.HUMAN_ANSWERED_KINDS` now.
from kernel.mailbox.schema import HUMAN_ANSWERED_KINDS as HUMAN_ONLY_KINDS
from app.services.story_card_thread import newest_human_thread


async def _pending(conn) -> list:
    # `q.dryrun` IS ABSENT FROM THIS SELECT, ON PURPOSE (R1). Plan 5.6
    # retired the pre-approval dry run and nothing has written that
    # column since, so every authorization card carried a rendered
    # "still running — you can approve now" line about a pass that no
    # longer exists and was never going to finish. Adding the column
    # back here brings that sentence back with it; the measurement
    # returns with E19's warm verification pod, not with this query.
    #
    # Said HERE rather than inside the SQL: a `--` comment in the string
    # is indistinguishable from SQL to the guard in
    # `test_no_unbacked_regression_claim`, which reads string literals
    # because a column name can only come back inside one.
    return await conn.fetch(
        """
        -- A PAUSE A PERSON PRESSED NEEDS NO CARD. They are looking at the
        -- thread, the Executor bar on it already says "paused — waiting on
        -- you", and Resume is right there with its own message box. A card
        -- restating it is a second control for one act, and the one people
        -- will reach for is whichever is nearer — so the two would have to
        -- stay in step for ever.
        --
        -- Only the HUMAN-pressed ones are skipped. A `direction` opened
        -- because an answerer found something still needs its card: nobody
        -- was necessarily watching that thread when it happened, and the card
        -- carries what was asked and what they were told, which the bar
        -- cannot.
        SELECT q.id, q.kind, q.question_text, q.payload,
               q.execution_id,
               e.goal_id, e.story_id, s.title,
               -- THE THREAD THE STORY ACTUALLY USES. `stories.thread_id`
               -- is the legacy per-goal conversation that stories were
               -- rotated through; `threads.story_id` is the resolver the
               -- assistant router settled on ("a thread of its OWN for
               -- this story"). Posting to the first put the approval
               -- card in a thread nobody was looking at -- it was in the
               -- database, correct and invisible. `active_thread_id` is
               -- the same trap one indirection along.
               COALESCE(
                 -- I5b: the DRIVING thread first. Without it this
                 -- takes the story's NEWEST thread, so with two
                 -- people in a goal the card follows whoever
                 -- imported last rather than whoever is
                 -- responsible — and an autonomous errand belongs
                 -- in the thread of the person who approved it.
                 e.driving_thread_id,
                 """ + newest_human_thread("e.story_id") + """,
                 s.active_thread_id, s.thread_id) AS thread_id,
               -- ANSWERED BEFORE THE POLL IS STILL A CARD. See the
               -- WHERE clause; `_post` stamps `decided` so the buttons
               -- do not come back on a question already settled.
               q.status AS q_status
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
          LEFT JOIN public.stories s ON s.id = e.story_id
          JOIN kernel.run_state_machine m ON m.version = e.machine_version
         WHERE q.status IN ('OPEN', 'ANSWERED')
          AND NOT (q.kind = 'direction'
                   AND q.payload->>'from' LIKE 'human:%')
           AND q.card_posted_at IS NULL
           -- A question on a finished run is waiting on nobody; the
           -- inbox learned this the same way, from an inbox that was
           -- seven-tenths dead cards.
           AND NOT (m.terminal_states @> to_jsonb(e.state))
         ORDER BY q.id
         LIMIT 20
        """)


def build_card(row, plan, plan_text: str) -> dict:
    """The card the browser renders. Shape follows app/cards.py."""
    needs_human = row["kind"] in HUMAN_ONLY_KINDS
    options = []
    if row["kind"] == "direction":
        # A HELD RUN IS STOPPED, NOT BROKEN, and the buttons have to say
        # so. Approve/decline would be wrong in both directions here:
        # there is no plan to approve, and declining is not what "keep
        # going as planned" means.
        options = [
            {"id": "approve", "label": "▶ Carry on as planned",
             "value": "approve", "style": "primary"},
            {"id": "revise", "label": "✎ Change the plan…",
             "value": "revise", "style": "secondary"},
        ]
    elif row["kind"] == "acceptance":
        # A STORY-LEVEL DECISION, and the words have to say so. This is
        # the only card in the set that is not about a plan: the plan is
        # spent, verification passed, the work is pushed. What is being
        # asked is whether the STORY is finished -- so "Approve" and
        # "Decline" are both category errors, and "Mark done" alone left
        # it ambiguous what was being marked.
        options = [
            {"id": "approve", "label": "✓ Mark story done",
             "value": "approve", "style": "primary"},
            {"id": "revise", "label": "↺ Ask for more work",
             "value": "revise", "style": "secondary"},
        ]
    elif row["kind"] == "landing":
        # THE MERGE CARD. Its primary action is on the forge, not here:
        # merging the PR resolves this park on its own, which is why
        # there is no "I merged it" button for somebody to get wrong.
        #
        # WHAT THE CARD IS FOR is the other answer. A person reading the
        # PR may decide it is not what they wanted, and without a
        # control here they would have a story they can neither merge
        # nor redirect.
        options = [
            {"id": "revise", "label": "↺ Ask for more work",
             "value": "revise", "style": "secondary"},
        ]
    elif row["kind"] == "closeout":
        # THE SAME WORDS AN ERRAND GETS, deliberately. "Mark story done"
        # means one thing on this platform and it has to keep meaning
        # it for both kinds; a coding story simply reaches this card
        # LATER, once its work is actually on trunk. Two labels for one
        # decision would teach people that finishing means different
        # things by kind, which it does not.
        # ONE OPTION, BECAUSE THERE IS ONE DECISION LEFT.
        #
        # This card used to offer "Ask for more work" beside it, and the
        # body sold it: "the tree that did this work is still here, so
        # if anything was missed, asking for it now continues THIS
        # story." Abhishek, 2026-10-14, ruled the other way, and it is
        # the better rule: "more work should always be done before
        # merging -- that is how work happens everywhere." The merge is
        # the commitment; asking after it reopens work somebody has
        # already accepted onto trunk.
        #
        # It was also the only thing that needed a merged branch brought
        # back to life, which is where it broke: the node is reset, the
        # create pass re-stamps `base_sha` with the live trunk tip, and
        # the branch -- never actually re-cut -- sits behind by its own
        # merge commit while the bookkeeping calls it level. No pull
        # request opens and no catch-up is offered.
        #
        # The `landing` card above still offers it, which is where it
        # belongs: before the merge, on a branch that is still live.
        options = [
            {"id": "approve", "label": "✓ Mark story done",
             "value": "approve", "style": "primary"},
        ]
    elif row["kind"] == "pr_closed":
        # A PERSON DID THIS ON PURPOSE. GitHub distinguishes closed
        # from merged, so nothing failed -- the question is what they
        # meant, and both answers are legitimate. "Approve" and
        # "decline" would be category errors again: there is nothing
        # to approve and refusing is not what cancelling means.
        options = [
            {"id": "approve", "label": "↺ Keep working on it",
             "value": "approve", "style": "primary"},
            {"id": "declined", "label": "✕ Cancel the story",
             "value": "declined", "style": "danger"},
        ]
    elif row["kind"] == "clarification":
        # THE ASSISTANT MAY ANSWER ONE -- and when it does not, nobody
        # could. `clarification` is deliberately outside
        # HUMAN_ANSWERED_KINDS ("an assistant may answer one, which is
        # what makes ask_human cheap"), and the card builder read that
        # as "no human control needed": no options branch, and
        # `allow_custom` keyed on `needs_human`, so the card rendered
        # with no buttons AND no text box.
        #
        # Watched on story dc11d7f2: the executor asked a question, the
        # assistant answered it with a `steer` -- which can only apply
        # at a step boundary a parked run does not have, so it sat
        # PENDING for ever -- then told the person to click the card.
        # There was nothing on the card to click. The run was cancelled
        # to get out of it.
        #
        # "May" is not "must". A question a person can see and cannot
        # answer is a park with no exit but cancellation.
        options = [
            {"id": "approve", "label": "↩ Send answer",
             "value": "approve", "style": "primary"},
        ]
    elif row["kind"] == "permission":
        # ONE CALL, NOT A PLAN. "Approve plan" is the wrong question
        # here: there is no plan on this card, there is a command. The
        # body carries it verbatim — `permission.describe` puts the
        # shell line and what makes it irreversible into the text — so
        # the buttons have to answer THAT.
        #
        # `revise` is kept because "not like that, use --dry-run first"
        # is the common answer and is neither an approval nor a refusal.
        # It relays from anybody; only `approve` demands a human uid.
        options = [
            {"id": "approve", "label": "✓ Allow this call",
             "value": "approve", "style": "primary"},
            {"id": "revise", "label": "✎ Not like that…",
             "value": "revise", "style": "secondary"},
            {"id": "declined", "label": "✕ Refuse",
             "value": "declined", "style": "danger"},
        ]
    elif needs_human:
        options = [
            {"id": "approve", "label": "✓ Approve plan",
             "value": "approve", "style": "primary"},
            {"id": "revise", "label": "✎ Request changes",
             "value": "revise", "style": "secondary"},
            {"id": "declined", "label": "✕ Decline",
             "value": "declined", "style": "danger"},
        ]
    return {
        "type": "agent_card",
        "card_type": "kernel_park",
        "question_id": row["id"],
        "execution_id": row["execution_id"],
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "kind": row["kind"],
        "agent": "executor",
        "agent_name": "Executor",
        # ONE TITLE PER KIND. `needs_human` covers three kinds and this
        # called them all "Plan approval" -- so an ESCALATION, which
        # asks whether the bar itself is wrong after repeated refusals,
        # arrived looking like a routine plan to approve. A person
        # reading the title reads the wrong question.
        "title": {"acceptance": "Verified — done, or more work?",
                  "closeout": "Merged — done, or more work?",
                  "landing": "Ready to merge — or more work?",
                  "pr_closed": "PR closed without merging — now what?",
                  "authorization": "Plan approval",
                  "direction": "Paused — your question needs a decision",
                  "escalation": "Stuck — is this check wrong, or the "
                                "work?",
                  "permission": "One command needs your approval"}.get(
                      row["kind"], row["question_text"] or "Question"),
        "body": plan_text or (row["question_text"] or ""),
        "plan": plan,
        "needs_human": needs_human,
        "options": options,
        # Free text rides with a revise: "nearly right, change step 3"
        # is the common answer and two buttons cannot carry it.
        #
        # AND A CLARIFICATION IS NOTHING BUT FREE TEXT. It is not in
        # HUMAN_ANSWERED_KINDS because the ASSISTANT may answer it, not
        # because a person may not -- and keying the text box on
        # `needs_human` turned "the assistant is allowed to help" into
        # "you are not allowed to reply".
        "allow_custom": needs_human or row["kind"] == "clarification",
    }


async def _landing_prs(conn, story_id: str):
    """EVERY open PR this story is waiting on, as (url, branch, repo).

    ALL OF THEM, NOT THE NEWEST. A story may write several repos, and
    they land as one group: the person has to merge each PR, and a card
    showing one of two is a card that looks complete and is not. They
    would merge it, nothing would happen, and nothing would say why.

    AN EMPTY LIST IS A REASON TO WAIT, not to post a worse card -- see
    the caller. And a story that has merged ONE of two is still waiting
    on the other, so merged nodes drop out and the rest remain.
    """
    rows = await conn.fetch(
        "SELECT gn.pr_number, gn.branch_name, r.owner, r.name "
        "  FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        "  JOIN git_repos r ON r.id = gn.git_repo_id "
        " WHERE gns.story_id = $1 AND gn.pr_number IS NOT NULL "
        "   AND gn.state NOT IN ('MERGED', 'ABANDONED') "
        " ORDER BY r.name, gn.cycle DESC", story_id)
    return [(f"https://github.com/{r['owner']}/{r['name']}/pull/"
             f"{r['pr_number']}", r["branch_name"], r["name"]) for r in rows]


def _landing_body(payload, prs) -> str:
    """The merge card: what was built, and the PR that lands it.

    THE LINK IS THE POINT. Every other card in this set is answered by
    pressing something on it; this one is answered on the forge, so if
    the person cannot get to the PR from here the card has failed at
    the only job it has.
    """
    p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    out = ["**The work is finished and pushed.** It is not on trunk yet — "
           "merging the pull request below is what lands it.", ""]
    if p.get("summary"):
        out += [str(p["summary"]).strip(), ""]
    if len(prs) == 1:
        url, branch, _ = prs[0]
        out += [f"**[Review and merge the pull request]({url})**", "",
                f"`{branch}`", ""]
    elif prs:
        # EVERY ONE OF THEM HAS TO BE MERGED. A multi-repo story lands
        # as one group, and the story keeps waiting until the last of
        # these is in -- so listing one would look complete and be a
        # trap.
        out += [f"**This story lands in {len(prs)} repositories, and all "
                f"of them have to be merged:**", ""]
        out += [f"- [{repo}]({url}) — `{branch}`" for url, branch, repo in prs]
        out += [""]
    out += ["---", "",
            "**Merging is the whole of it** — there is nothing to come back "
            "and click. The story notices the merge on its own and then "
            "asks whether it is done." + (
                " It keeps waiting until the LAST of them is merged."
                if len(prs) > 1 else ""), "",
            "**If the PR is not what you wanted**, ask for more work "
            "instead. The story carries on in the workspace it still has, "
            "on the same branch, so your next look is an updated PR rather "
            "than a second one."]
    return chr(10).join(out).strip()


def _closeout_body(payload) -> str:
    """The work is on trunk. Written for somebody deciding whether
    the STORY is finished, which is a different question from
    whether the work is good -- they already said that.

    THE ORDER IS THE POINT, as with `_direction_body`. What landed,
    then the one thing that is true now and will not be in a
    moment: the workspace is still up. A person who closes the
    story without knowing that has not been told what they are
    spending.
    """
    p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    out = ["**This story's work is on trunk.**", ""]
    if p.get("summary"):
        out += [str(p["summary"]).strip(), ""]
    if p.get("branches"):
        merged = ", ".join("`" + str(b) + "`" for b in p["branches"])
        out += ["Merged: " + merged, ""]
    out += ["---", "",
            "**Closing it releases the workspace.** The work is on "
            "trunk, so this story is finished — anything further is a "
            "new story.", "",
            "More work had to be asked for BEFORE the merge, while the "
            "branch was still live and its pull request still open. "
            "Once it has landed, reopening it would mean rewriting work "
            "somebody has already accepted."]
    return "\n".join(out).strip()


def _direction_body(payload) -> str:
    """A held run, written for the person whose question stopped it.

    THE ORDER IS THE POINT. What they asked, what they were told, then
    what is now in front of them -- and the reassurance early, because
    "your story has stopped" is what they read first and it sounds like
    a failure. It is not: the run is parked exactly where it was.
    """
    p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    out = []
    if p.get("asked"):
        out += ["**You asked:** " + str(p["asked"]).strip(), ""]
    if p.get("answer"):
        out += [str(p["answer"]).strip(), ""]
    out += ["---", "",
            "**The work has stopped here and is waiting on you.** Nothing "
            "has failed and nothing is lost — the story is parked exactly "
            "where it was and carries on the moment you answer.", ""]
    if p.get("decision"):
        out += ["**What to decide:** " + str(p["decision"]).strip(), ""]
    if p.get("options"):
        out += [str(p["options"]).strip(), ""]
    if p.get("why_now"):
        out += ["_Why this could not wait: " + str(p["why_now"]).strip()
                + "_", ""]
    out += ["Choosing **carry on** resumes the existing plan unchanged. "
            "Anything that changes what gets built starts a new cycle: the "
            "executor will write a plan and come back for your approval "
            "before it touches a file."]
    return "\n".join(out).strip()


async def _post(conn, row) -> bool:
    from kernel.assistant.cards import as_text, plan_of

    # AN ESCALATION PAYLOAD IS NOT A PLAN. It carries `checks` too --
    # meaning "the checks that keep refusing", not "the checks that
    # define done" -- so parsing it as a plan produced a card headed
    # "Plan approval" listing two checks under "Verified by" and no
    # steps. Every word of that was wrong about what was being asked.
    plan = (plan_of(row["payload"])
            if row["kind"] in HUMAN_ONLY_KINDS else None)
    plan_text = as_text(plan)
    if row["kind"] == "direction":
        # Not a plan either, and `question_text` alone is just the
        # decision -- which would show the fork with neither the
        # question that produced it nor the answer that justified it.
        plan_text = _direction_body(row["payload"])
    elif row["kind"] == "landing":
        # A WAIT THAT IS OVER NEEDS NO CARD. `_pending` offers ANSWERED
        # questions too, so a card can show what was decided -- right
        # for a plan or an acceptance, wrong for this one. A landing is
        # answered by the MERGE or by the platform retiring it to
        # integrate; either way there is nothing left for the person to
        # do, and the card says "merge the pull request below".
        #
        # Seen on a4098b67: two merge cards, the first for a park
        # retired three minutes earlier. Its claim had been released by
        # the deferral below, so when the PR finally appeared the
        # poller offered the dead row again and posted it.
        if row["q_status"] != "OPEN":
            return False
        prs = await _landing_prs(conn, row["story_id"])
        if not prs:
            # RELEASE THE CLAIM, or this card is lost for good. The
            # poller stamps `card_posted_at` BEFORE calling here --
            # deliberately, so a post that fails leaves a card nobody
            # sees rather than a question announced twice. That policy
            # is right for a FAILURE and wrong for a DEFERRAL: this one
            # is "not yet", and without putting the claim back the row
            # is never offered again and the merge card never appears.
            #
            # Seen on BMIA1 run 97: landing question 230 stamped at
            # 12:57:37, PR #21 opened seconds later, and no card ever.
            await conn.execute(
                "UPDATE kernel.questions SET card_posted_at = NULL "
                " WHERE id = $1", row["id"])
            # NOT AN ERROR, AND DELIBERATELY NOT POSTED. The PR does not
            # exist yet -- it is opened once the node reaches READY, and
            # the node reaches READY because this very park moved the
            # story to `awaiting_merge`. `card_posted_at` stays NULL, so
            # the poller offers this row again next pass and the card
            # appears the moment there is a PR to put on it.
            log.info("kernel park card: landing question %s has no PR yet "
                     "— holding the card", row["id"])
            return False
        plan_text = _landing_body(row["payload"], prs)
    elif row["kind"] == "closeout":
        # `plan_of` finds nothing here -- there are no steps to approve,
        # the plan was spent two parks ago -- so without this the card
        # falls back to `question_text` and says only that something
        # merged. What a person needs to decide is what landed and what
        # closing it costs them.
        plan_text = _closeout_body(row["payload"])
    card = build_card(row, plan, plan_text)
    # NO `card["dryrun"]`. An authorization card used to carry one; see
    # the note in `_pending`'s SELECT and in `kernel/assistant/cards.py`
    # where `dryrun_of` was deleted. The card now shows the plan and the
    # checks, which is everything that is actually known at approval.

    if not row["thread_id"]:
        # No thread to post into. Mark it announced anyway: the poller
        # would otherwise retry this row for ever, and the question is
        # still perfectly answerable through the assistant and the API.
        log.warning("kernel park card: execution %s (story %s) has no "
                    "thread; not posting", row["execution_id"],
                    row["story_id"])
        return False

    # ALREADY SETTLED, SO NO LIVE BUTTONS. `decided` is the same key
    # the errand proposal stamps on decide, and the renderer reads it:
    # `const open = card.needs_human && !card.decided`. Without it a
    # question answered before the poll would render its buttons on
    # every reload, the next click would 409, and a correct refusal
    # would read as a broken page.
    if (row.get("q_status") or "OPEN") != "OPEN":
        card["decided"] = "answered"

    body = card["body"] or "The executor is waiting on you."
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','executor',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], body, json.dumps(card))

    # The DURABLE fan-out, not a direct socket write: every web instance
    # re-broadcasts to its own subscribers, so the card reaches a viewer
    # whose socket is on a different instance -- and this poller has no
    # browser session of its own. Same path diff-review and dag-recovery
    # cards take.
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                       # noqa: BLE001
        # The row is in the thread; a refresh shows it. A failed push is
        # not a failed delivery.
        log.exception("kernel park card: fan-out notify failed (story=%s)",
                      row["story_id"])
    return True


# `VERDICT_THREAD_SQL` WAS DEFINED TWICE, and Python kept the
# second. The first was twenty-five lines of near-identical SQL that
# had not run since whenever the two diverged -- so editing it fixed
# nothing, silently, and reading it told you about a query the
# process never issued. Deleted rather than merged: the surviving
# definition is the one that has been in production.
VERDICT_THREAD_SQL = """
    SELECT v.id, v.passed, v.refusal, v.claim_seq,
           e.goal_id, e.story_id, e.id AS execution_id,
           COALESCE(
             -- I5b: the DRIVING thread first. Without it this
             -- takes the story's NEWEST thread, so with two
             -- people in a goal the card follows whoever
             -- imported last rather than whoever is
             -- responsible — and an autonomous errand belongs
             -- in the thread of the person who approved it.
             e.driving_thread_id,
             """ + newest_human_thread("e.story_id") + """,
             s.active_thread_id, s.thread_id) AS thread_id,
           s.title
      FROM kernel.verdicts v
      JOIN kernel.executions e ON e.id = v.execution_id
      LEFT JOIN public.stories s ON s.id = e.story_id
     WHERE v.card_posted_at IS NULL
     ORDER BY v.id
     LIMIT 20
"""


def _refusal_body(row, note: str) -> str:
    """Counts and commands and output, then the model's reading."""
    try:
        from app.services.verdict_summary import summarise
        body = summarise(row["refusal"])
    except Exception:                                       # noqa: BLE001
        # A summariser that raises must not cost the person the note
        # they would otherwise have had.
        return note
    if note and "The verifier's reading" not in body:
        nl = chr(10)
        body = body + nl + nl + "---" + nl + nl + note
    return body


def _verdict_card(row, note: str) -> dict:
    """A verdict card: informational, no buttons. The story id leads --
    the run number is internal and rides along for provenance only.

    THIS HEADER WAS DELETED, not the function. 58a0c7b5 renamed the old
    `_verdict_card` to `_refusal_body` and gave it a new, smaller body --
    but the ORIGINAL body was left behind, indented, after
    `_refusal_body`'s `return`. Python accepted it happily as
    unreachable code, so nothing complained at import; the only symptom
    was `_post_verdict` raising NameError on every verdict, which meant
    no verdict card has reached a thread since. `check_unbound_names`
    reported it the whole time.
    """
    passed = bool(row["passed"])
    return {
        "type": "agent_card",
        "card_type": "kernel_verdict",
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "execution_id": row["execution_id"],
        "verdict": "passed" if passed else "refused",
        "agent": "verifier",
        "agent_name": "Verifier",
        "title": (f"Verification passed — {row['title'] or row['story_id']}"
                  if passed else
                  f"Verification failed — {row['title'] or row['story_id']}"),
        # THE EVIDENCE, NOT JUST THE READING. `note` is the explainer's
        # paragraph, and on its own it told a person which rule a model
        # THOUGHT was wrong -- with no count, no commands, and none of
        # the output it inferred from. The one refusal we read closely
        # was caused by a missing input file rather than the rule the
        # explainer named, and the card offered no way to notice.
        #
        # And the old fallback said the executor "will try again",
        # which is no longer true: a verdict spends the plan approval,
        # so the next cycle starts with a new plan and a person
        # approving it.
        "body": (note if passed else _refusal_body(row, note)) or
                ("All checks green." if passed else
                 "A check said no. The executor must now register a new "
                 "plan for the next cycle, and you approve it."),
        "options": [],
        "allow_custom": False,
    }


async def _post_verdict(conn, row) -> bool:
    if not row["thread_id"]:
        return False
    # The explainer's note travels IN the refusal jsonb (first entry,
    # key "explain") -- written by the adjudicator at verdict time so
    # the card and the executor read the same sentence.
    note = ""
    if not row["passed"]:
        try:
            entries = json.loads(row["refusal"] or "[]")
            note = next((e.get("explain") for e in entries
                         if isinstance(e, dict) and e.get("explain")), "")
            if not note and entries:
                first = entries[0] if isinstance(entries[0], dict) else {}
                note = (f"{first.get('code', 'refused')}: "
                        f"{(first.get('output') or '')[-300:]}")
        except (ValueError, TypeError):
            note = ""
    card = _verdict_card(row, note)
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','verifier',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], card["body"], json.dumps(card))
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                       # noqa: BLE001
        log.exception("verdict card: fan-out notify failed (story=%s)",
                      row["story_id"])
    return True


PR_THREAD_SQL = """
    SELECT n.id AS node_id, n.pr_number, n.branch_name, n.trunk_branch,
           r.owner, r.name AS repo_name,
           s.id AS story_id, s.title, n.goal_id,
           -- `s.id`, NOT `e.story_id`: this query has no execution in
           -- scope, so there is no driving thread to prefer either.
           COALESCE(
             """ + newest_human_thread("s.id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM git_nodes n
      JOIN git_repos r ON r.id = n.git_repo_id
      JOIN git_node_stories gns ON gns.git_node_id = n.id
      JOIN stories s ON s.id = gns.story_id
     WHERE n.pr_number IS NOT NULL
       AND n.pr_card_posted_at IS NULL
       AND n.state <> 'ABANDONED'
     ORDER BY n.id
     LIMIT 20
"""


async def _post_pr(conn, row) -> bool:
    """The story's PR, in the story's own thread. Informational; the
    merge itself stays on GitHub where branch protection lives."""
    if not row["thread_id"]:
        return False
    url = (f"https://github.com/{row['owner']}/{row['repo_name']}"
           f"/pull/{row['pr_number']}")
    body = (f"Pull request **#{row['pr_number']}** is open for this "
            f"story: [{row['branch_name']} -> "
            f"{row['trunk_branch'] or 'trunk'}]({url}). "
            f"A human reviews and merges it on GitHub; once it lands, "
            f"the branch is deleted and the story is delivered.")
    card = {
        "type": "agent_card",
        "card_type": "kernel_pr",
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "pr_number": row["pr_number"],
        "url": url,
        "agent": "git-engine",
        "agent_name": "Git Engine",
        "title": f"PR #{row['pr_number']} open — "
                 f"{row['title'] or row['story_id']}",
        "body": body,
        "options": [],
        "allow_custom": False,
    }
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','git-engine',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], body, json.dumps(card))
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                       # noqa: BLE001
        log.exception("pr card: fan-out notify failed (story=%s)",
                      row["story_id"])
    return True


# Only these stall reasons become cards: each is a state a PERSON may
# need to act on. Everything else on stall_reason (investigator notes on
# refusals, transient retry chatter) is already carded elsewhere or is
# noise at card volume.
STALL_PREFIXES = ("workspace setup failed", "publish failed",
                  "verification errored", "verification could not run")

STALL_THREAD_SQL = """
    SELECT e.id AS execution_id, e.goal_id, e.story_id, e.stall_reason,
           s.title,
           COALESCE(
             -- I5b: the DRIVING thread first. Without it this
             -- takes the story's NEWEST thread, so with two
             -- people in a goal the card follows whoever
             -- imported last rather than whoever is
             -- responsible — and an autonomous errand belongs
             -- in the thread of the person who approved it.
             e.driving_thread_id,
             """ + newest_human_thread("e.story_id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM kernel.executions e
      LEFT JOIN public.stories s ON s.id = e.story_id
     WHERE e.stall_reason IS NOT NULL
       AND e.stall_reason IS DISTINCT FROM e.stall_card_note
     ORDER BY e.updated_at
     LIMIT 20
"""


# A stall that RECOVERED. `stall_reason` goes NULL the moment the run
# gets past whatever stopped it; `stall_card_note` keeps the text of
# the card that was posted, so it can only be posted once. Nothing
# joined those two facts, so the card stayed on the board saying "work
# is verified but not delivered" over work that had since been
# delivered -- observed on dev execution 3, whose push succeeded on
# retry while the card kept offering a Retry button for it.
#
# The card is not deleted. Its stated job is that "a persistent
# failure is never a silent one", and a failure that happened is still
# a fact worth keeping; what it must stop doing is describing the
# PRESENT. So the resolution is a second, short message under it.
STALL_CLEARED_SQL = """
    SELECT e.id AS execution_id, e.goal_id, e.story_id, e.stall_card_note,
           e.state, s.title,
           COALESCE(
             e.driving_thread_id,
             """ + newest_human_thread("e.story_id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM kernel.executions e
      LEFT JOIN public.stories s ON s.id = e.story_id
     WHERE e.stall_reason IS NULL
       AND e.stall_card_note IS NOT NULL
     ORDER BY e.updated_at
     LIMIT 20
"""


# States where "resolved" would be a lie. A cancelled or failed run did
# not get past its stall; the note is released so the row stops being
# re-read, and nothing is announced.
_NOT_RESOLVED = ("CANCELLED", "FAILED", "ABANDONED")


async def _post_stall_cleared(conn, row) -> bool:
    """Say the stall is over, in the thread that was told it began.

    THREE REASONS TO CLEAR WITHOUT SAYING ANYTHING, and each was a real
    row on dev when this was written:

      * no thread -- nothing was ever posted, so there is nothing to
        correct;
      * a note that is not one of STALL_PREFIXES -- `_post_stall` only
        cards those, so "installing: its environment is still
        installing" never produced a card and a resolution for it would
        announce the end of a failure nobody was told about;
      * a run that is CANCELLED or FAILED -- it did not get past the
        stall, so "resolved" would be false.

    In all three the note is still released by the caller's claim, or
    the row is re-read every tick for ever.
    """
    note = row["stall_card_note"] or ""
    if not row["thread_id"]:
        return False
    if not any(note.startswith(p) for p in STALL_PREFIXES):
        return False
    if (row["state"] or "").upper() in _NOT_RESOLVED:
        return False
    was = (row["stall_card_note"] or "").split(":")[0].strip() or "the stall"
    card = {
        "type": "agent_card",
        "card_type": "kernel_stall_cleared",
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "execution_id": row["execution_id"],
        "agent": "kernel",
        "agent_name": "Kernel",
        "title": f"Resolved — {row['title'] or row['story_id']}",
        "body": f"The earlier failure ({was}) is no longer blocking this "
                f"run: it was retried and got through. The run is "
                f"{(row['state'] or 'running').lower()}. No action needed — "
                f"the card above is kept as a record.",
        "options": [],
        "allow_custom": False,
    }
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','kernel',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], card["body"], json.dumps(card))
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                       # noqa: BLE001
        log.exception("stall cleared card: fan-out notify failed "
                      "(story=%s)", row["story_id"])
    return True


def _stall_title(reason: str) -> str:
    if reason.startswith("workspace setup failed"):
        return "Setup failed"
    if reason.startswith("publish failed"):
        return "Push failed — work is verified but not delivered"
    return "Verification could not run"


async def _post_stall(conn, row) -> bool:
    reason = row["stall_reason"] or ""
    if not (row["thread_id"]
            and any(reason.startswith(p) for p in STALL_PREFIXES)):
        return False
    card = {
        "type": "agent_card",
        "card_type": "kernel_stall",
        "goal_id": row["goal_id"],
        "story_id": row["story_id"],
        "execution_id": row["execution_id"],
        "agent": "kernel",
        "agent_name": "Kernel",
        "title": f"{_stall_title(reason)} — "
                 f"{row['title'] or row['story_id']}",
        "body": reason + "\n\nThe executor has been told the same "
                "thing and retries on its own; this card is so a "
                "persistent failure is never a silent one. If the cause "
                "was yours to fix — a credential, a permission — fix it "
                "and press Retry.",
        "options": [],
        "allow_custom": False,
        # RETRY IS OFFERED HERE because this is where a person already
        # is when the cause is theirs. It clears the stall and the
        # strike counter and nothing else; the loops do the rest. The
        # button carries the run and the endpoint carries the person:
        # "the cause is fixed" is a claim about the world outside the
        # kernel, and only a person can make one.
        "retry_run": row["execution_id"],
    }
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO messages (id, thread_id, role, sender, content, "
        "card_data, created_at) "
        "VALUES ($1,$2,'agent','kernel',$3,$4::jsonb, NOW())",
        msg_id, row["thread_id"], card["body"], json.dumps(card))
    try:
        from app.chat_module.repository import notify_new_message
        await notify_new_message(row["thread_id"], msg_id)
    except Exception:                                       # noqa: BLE001
        log.exception("stall card: fan-out notify failed (story=%s)",
                      row["story_id"])
    return True


# THE CLAIM IS INSIDE THE TRY, AND EACH KIND IS INSIDE ITS OWN, because
# the alternative cost two and a half days of silence.
#
# Every loop below used to put the claim OUTSIDE its try -- only the post
# was guarded. A verdict claim then started raising `restrict_violation`
# (kernel.verdicts is append-only and the trigger refused to let the
# control panel set `card_posted_at`; migrations/verdicts_allow_the_card_
# claim.sql narrows it), and because verdicts are swept BEFORE questions,
# the exception left `sweep` before it reached the loop that posts
# QUESTION and PLAN cards. Ten verdicts were waiting, the oldest from
# 2026-09-17, and no plan card reached any thread in between.
#
# It reported itself the whole time -- "kernel park cards: sweep failed",
# every five seconds -- and was found only because somebody noticed a
# missing plan card two days later and asked. A loop that announces
# several kinds of thing must not let one kind decide whether the others
# are announced at all.
#
# So: per-row isolation, so one bad row does not end its kind; and
# per-kind isolation, so one kind cannot end the sweep. Both are needed.
# Per-row alone would not have survived `conn.fetch` itself failing.
def _kind(name: str):
    """A guard for one card kind. Logs and carries on."""
    class _Guard:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc is not None:
                log.exception("kernel park cards: %s kind failed; other "
                              "kinds still swept", name)
            return True
    return _Guard()


# A CARD WHOSE QUESTION HAS BEEN SETTLED, BY ANY ROUTE.
#
# `kernel_decide.decide()` stamps `decided` so the buttons do not come
# back on reload -- but it is the CONTROL PANEL's path, reached from the
# card's own buttons. GO settles a plan with the `decide_plan` TOOL,
# which goes through the kernel, and the kernel cannot write `messages`:
# that is the app's table and `kernel/` may not import `app/`.
#
# So the stamp depended on WHICH ROUTE answered the question. Observed
# on ERR17 question 416: `status = ANSWERED`, card row present,
# `decided` empty -- so the plan-approval card renders its buttons for a
# decision already taken, and the next click gets a 409 that reads as a
# broken page.
#
# HERE RATHER THAN AT EACH CALLER, because there is no end to the
# callers: the tool, the API, the buttons, Slack, a future one. The
# question's status is the fact; this makes the card agree with it on
# the next pass, whoever answered and however.
#
# Idempotent by its own WHERE: a card already stamped is not matched.
SETTLED_CARDS_SQL = """
    UPDATE messages m
       SET card_data = m.card_data
                     || jsonb_build_object('decided', 'answered')
      FROM kernel.questions q
     WHERE m.card_data->>'card_type' = 'kernel_park'
       AND m.card_data->>'question_id' = q.id::text
       AND m.card_data->>'decided' IS NULL
       AND q.status <> 'OPEN'
"""


async def _settle_answered_cards(conn) -> int:
    """Stamp every card whose question is no longer open. Never raises:
    an unstamped card is a card with stale buttons, which is untidy and
    not worth failing the whole sweep for."""
    try:
        done = await conn.execute(SETTLED_CARDS_SQL)
        n = int(done.rsplit(" ", 1)[-1]) if done else 0
        if n:
            log.info("park cards: stamped %d settled card(s)", n)
        return n
    except Exception:                                       # noqa: BLE001
        log.exception("park cards: could not stamp settled cards")
        return 0


async def sweep() -> int:
    pool = get_pool()
    posted = 0
    async with pool.acquire() as conn:
        await _settle_answered_cards(conn)
        # Stalls: claim = record the exact reason a card was posted for,
        # so the errored path re-writing the same text every tick posts
        # once, and a new reason posts again.
        with _kind("stall"):
            for row in await conn.fetch(STALL_THREAD_SQL):
                try:
                    claimed = await conn.fetchval(
                        "UPDATE kernel.executions SET stall_card_note = $2 "
                        "WHERE id = $1 AND stall_reason = $2 "
                        "AND stall_reason IS DISTINCT FROM stall_card_note "
                        "RETURNING id",
                        row["execution_id"], row["stall_reason"])
                    if claimed is None:
                        continue
                    if await _post_stall(conn, row):
                        posted += 1
                except Exception:                           # noqa: BLE001
                    log.exception("stall card: execution %s failed",
                                  row["execution_id"])
        # And the other edge of the same fact: a stall that cleared.
        # CLAIMED THE SAME WAY -- null the note first, and only post if
        # this tick is the one that nulled it. Two conductors sweep,
        # and posting before claiming would put two resolutions under
        # one card.
        with _kind("stall-cleared"):
            for row in await conn.fetch(STALL_CLEARED_SQL):
                try:
                    claimed = await conn.fetchval(
                        "UPDATE kernel.executions SET stall_card_note = NULL "
                        "WHERE id = $1 AND stall_reason IS NULL "
                        "AND stall_card_note IS NOT NULL RETURNING id",
                        row["execution_id"])
                    if claimed is None:
                        continue
                    if await _post_stall_cleared(conn, row):
                        posted += 1
                except Exception:                           # noqa: BLE001
                    log.exception("stall cleared: execution %s failed",
                                  row["execution_id"])
        # PR cards are NOT posted here: the git engine's own
        # git_pr_opened card already lands in the story thread -- the
        # first live PR (#70) proved it, two cards for one PR within a
        # second. KER04 never showed that card only because no PR was
        # ever truly opened. One event, one claim, one card.
        # Verdicts first: same claim-before-post discipline as questions.
        with _kind("verdict"):
            for row in await conn.fetch(VERDICT_THREAD_SQL):
                try:
                    claimed = await conn.fetchval(
                        "UPDATE kernel.verdicts SET card_posted_at = now() "
                        "WHERE id = $1 AND card_posted_at IS NULL "
                        "RETURNING id",
                        row["id"])
                    if claimed is None:
                        continue
                    if await _post_verdict(conn, row):
                        posted += 1
                except Exception:                           # noqa: BLE001
                    log.exception("verdict card: %s failed", row["id"])
        # QUESTION AND PLAN CARDS. Swept LAST, which is why the verdict
        # claim's exception was able to silence them -- see the note on
        # `_kind` above.
        with _kind("question"):
            for row in await _pending(conn):
                # CLAIM BEFORE POSTING, not after. Marking the row once
                # the card is in the thread leaves a window: a crash in
                # between re-posts on the next tick, and on cloud there
                # is more than one control panel, so two instances read
                # the same OPEN row and both announce it. The conditional
                # update is the claim -- exactly one caller gets the row
                # back, and a card that then fails to post is a card
                # nobody sees rather than a question announced twice. The
                # first is recoverable by the assistant and the parked
                # page; the second trains people to ignore the cards.
                try:
                    claimed = await conn.fetchval(
                        "UPDATE kernel.questions SET card_posted_at = now() "
                        "WHERE id = $1 AND card_posted_at IS NULL "
                        "RETURNING id",
                        row["id"])
                    if claimed is None:
                        continue
                    if await _post(conn, row):
                        posted += 1
                except Exception:                           # noqa: BLE001
                    log.exception("kernel park card: question %s failed",
                                  row["id"])
    return posted


async def run_forever() -> None:
    log.info("kernel park cards: watching for parked executors")
    while True:
        # ONE GUARD PER SWEEP, because they are four independent
        # announcements sharing one loop. They used to share one `try`,
        # so when the verdict claim started raising -- kernel.verdicts is
        # append-only and refused the card claim -- it took node cards,
        # answer cards and errand proposals down with it. Four card
        # systems silent for two and a half days, from one table's
        # trigger.
        #
        # The sweeps stay in one loop for the reason the comments below
        # give: a second poller is a second thing to remember to start.
        # Sharing a loop is fine. Sharing a failure is not.
        n = 0
        ok = True
        try:
            n += await sweep()
            # The GIT ENGINE's escalations, on the same tick and in the
            # same loop. Its failures are the ones nothing else
            # surfaced -- a push refused on a missing App permission
            # stopped the story with every other reader silent -- and
            # giving them their own loop would be a second thing to
            # remember to start.
        except Exception:                               # noqa: BLE001
            ok = False
            log.exception("kernel park cards: sweep failed; the "
                          "other sweeps still ran")
        try:
            from app.services.kernel_node_cards import sweep as node_sweep
            n += await node_sweep()
            # And the answers to questions people asked about the work.
            # BEFORE nothing and AFTER the questions above deliberately:
            # when an answer held the run, its `direction` card was
            # posted by this same tick, and posting the answer second
            # puts the two in the thread in the order they happened --
            # the question, then the pause it caused.
        except Exception:                               # noqa: BLE001
            ok = False
            log.exception("kernel park cards: node_sweep failed; the "
                          "other sweeps still ran")
        try:
            from app.services.kernel_answer_cards import sweep as answer_sweep
            n += await answer_sweep()
            # I5f — and GO's errand proposals, into the GOAL thread rather
            # than a story's. Same tick and same loop as the rest: a
            # separate poller would be a second thing to remember to
            # start, and the first deploy that forgot it would leave
            # every proposal invisible with nothing to say so.
        except Exception:                               # noqa: BLE001
            ok = False
            log.exception("kernel park cards: answer_sweep failed; the "
                          "other sweeps still ran")
        try:
            from app.services.errand_proposal_cards import (
                sweep as proposal_sweep)
            n += await proposal_sweep()
        except Exception:                               # noqa: BLE001
            ok = False
            log.exception("kernel park cards: proposal_sweep failed; the "
                          "other sweeps still ran")
        if n:
            log.info("kernel park cards: posted %s", n)
        # A PASS THAT DID ITS JOB, reported so /healthz/loops can tell a
        # working loop from a breathing one. The outage this comes from
        # was a loop that ran on time, caught its own exception and
        # achieved nothing -- liveness said healthy for two and a half
        # days. `ok` is false if ANY sweep raised, so the age on that
        # endpoint is the age of the last completely clean pass.
        if ok:
            try:
                from app.services.scheduler.supervisor import supervisor
                supervisor.beat("park_cards")
            except Exception:                           # noqa: BLE001
                pass
        await asyncio.sleep(INTERVAL_S)
