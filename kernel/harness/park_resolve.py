"""Recording a human's reply, and un-parking the run on it.

SPLIT OUT OF `park.py` (plan 11.5c). One function, because everything it
does has to happen together or not at all: the verdict is written, the
checks are adopted, a waiting grant is pinned, and the execution leaves
`PARKED_HOT` — and a caller that could do three of those four would be a
way to leave a run parked forever with its plan approved.

IT RUNS IN THE MAILBOX WORKER, not in the executor. `park_open`'s writes
happen while the leg still holds the lease; this one happens minutes or
hours later from another process, which is why it re-reads the row it is
about instead of being handed one.

E9 REMOVED THE F11 DRY-RUN GATE FROM HERE (plan 5.6, and 5.18 item 2
named this file as the thing owed). What is gone: the
`blocks_approval` call, the `dryrun` and `checks` columns this function
read only to feed it, and the `approved_despite` note it appended to
`step_notes` afterwards. `blocks_approval` had already been reduced to a
`return ""` shim, so the advisory was always empty, the append never
fired, and the two extra columns were parsed for nothing on every
approval. The AUTHORITY gate below (H8a) is a different rule and stays:
it is about WHO approves, never about whether the plan can be measured.
"""

from __future__ import annotations

import json

from kernel.harness.park_approval import pin_plan_to_grant
from kernel.harness.park_kinds import ApprovalRefused


async def resolve_request(conn, request_id: int, answer: str,
                          answered_by: str, declined: bool = False,
                          verdict: str = "",
                          step_notes: list | None = None,
                          human_uid: str | None = None,
                          go_autonomous: bool = False) -> None:
    """Record a human's reply and un-park the run.

    `verdict` and `step_notes` are for a PLAN approval, where approve-or-
    decline was never enough. A person reading a five-step plan usually
    thinks *steps 1 and 2 are fine, step 3 should be X* — and with two
    buttons that has to go into a free-text blob for the model to guess
    at.

    `revise` is deliberately not `declined`. Conflated, a person saying
    "nearly right, change step 3" told the model its whole approach was
    rejected — and the sensible response to that is to abandon an
    approach somebody actually wanted.

    `human_uid` is the person doing the approving, and it is None unless
    a caller says otherwise — see the H8a note below for why the default
    is the safe one rather than the convenient one.
    """
    settled = verdict or ("declined" if declined else "approve")

    # ONE thing may refuse an approval now: WHO is approving (H8a).
    #
    # There were two, and they were orthogonal — the second was F11's
    # "can the plan's checks tell the difference", measured by a dry run
    # before a person saw the card. E9 retired it (plan 5.6): a dry run
    # was a full `verify_claim`, 112.5s on execution 208 and six Jobs
    # across five generations on 203, and it was ADDING to the wasted
    # generations rather than preventing them. `questions.dryrun` is
    # never written any more, so the advisory that path produced was
    # always empty — the whole branch was dead weight on every approval.
    #
    # `revise` and `declined` are unaffected by the one that remains.
    # Neither is permission, and refusing to let somebody reject a bad
    # plan would be absurd — which is also why an assistant may still
    # relay a rejection.
    #
    # `kind` IS THE ONLY COLUMN THIS NEEDS. It read `checks` and `dryrun`
    # too, both solely to feed the retired gate; a future reader adding a
    # rule here should add the column it actually reads, not restore
    # those two because the query once had them.
    if settled == "approve":
        row = await conn.fetchrow(
            "SELECT kind FROM questions WHERE id = $1", request_id)
        is_plan = bool(row) and row["kind"] == "authorization"

        # ── H8a · approving is a human act, and this is the only place
        # that can enforce it ────────────────────────────────────────
        #
        # §1.3 reserves approval to a human uid and says an agent token
        # is "refused at the router". But the mailbox exists precisely so
        # the assistant does NOT hold a connection — it submits an intent
        # and a worker applies it — so nothing it sends ever reaches a
        # router. Every path converges here instead, which makes here the
        # only seam where the rule can be true.
        #
        # FAIL-CLOSED BY DEFAULT, and that is the whole design. `human_uid`
        # is None unless a call site says otherwise, so a new caller that
        # has not thought about authority cannot approve a plan by
        # accident — it can only fail loudly. `answered_by` cannot serve:
        # it is a free string an agent fills in with its own id, which is
        # exactly how this hole stayed open.
        #
        # NOT AUTHENTICATION. E0 has none, and this does not invent one.
        # It is the seam authentication attaches to, shaped so that
        # attaching it later changes one argument per call site rather
        # than the control flow.
        is_acceptance = bool(row) and row["kind"] == "acceptance"
        # An escalation's APPROVE is accept-the-story-over-the-verifier
        # territory -- a person's call. Its "carry on with this note"
        # answer (revise) stays relayable by the assistant, which is the
        # third option the card offers.
        is_escalation = bool(row) and row["kind"] == "escalation"
        # Ec3b — AND A PERMISSION, which is the sharpest case of all. The
        # question is "may I run `terraform destroy`", and an agent able
        # to answer its own is no gate at all. A decline still relays:
        # saying no is safe from anybody.
        is_permission = bool(row) and row["kind"] == "permission"
        # ── the ONE exemption, and it is verified here ───────────────
        #
        # An AUTONOMOUS errand's plan may be settled by the GO alone. A
        # person approved the errand knowing it would run unattended;
        # requiring them to approve each plan as well puts back the
        # stall that consent existed to remove.
        #
        # `go_autonomous` IS NOT THE AUTHORITY. It is the caller saying
        # "no person is behind this one", and this function then checks
        # the grant ITSELF -- the same mistake as trusting `answered_by`
        # is the one available here, and it is the mistake H8a was
        # written about. The grant is read now rather than at dispatch
        # because a person may have revoked it since, and revoking is
        # how you take this authority back.
        #
        # ACCEPTANCE IS NOT COVERED, deliberately. Done-or-more-work is
        # the decision the person kept, and it is the reason handing the
        # plan over is tolerable at all.
        if is_plan and go_autonomous and not (human_uid or "").strip():
            from kernel.go.self_decide import alone_ok

            eid = await conn.fetchval(
                "SELECT execution_id FROM questions WHERE id = $1",
                request_id)
            if eid and not await alone_ok(conn, eid):
                is_plan = False          # allowed, on this run, just now

        if ((is_plan or is_acceptance
             or ((is_escalation or is_permission) and settled == "approve"))
                and not (human_uid or "").strip()):
            raise ApprovalRefused(
                "E_APPROVAL_NOT_HUMAN: approving requires a human uid and "
                "this call supplied none. Approval is reserved to a person "
                "(§1.3); an agent may relay a decline or a revise but never "
                "an approval.")

    await conn.execute(
        """
        UPDATE questions
        SET status = 'ANSWERED', declined = $2, answer_text = $3,
            answered_by = $4, answered_at = now(),
            verdict = $5, step_notes = $6::jsonb
        WHERE id = $1 AND status = 'OPEN'
        """,
        request_id, bool(declined), answer, answered_by, settled,
        json.dumps(step_notes or []),
    )

    # THE `approved_despite` APPEND WENT WITH THE GATE. It ran after the
    # write above — which sets `step_notes` wholesale and would otherwise
    # clobber it — and recorded what the dry run found so an approval
    # given over its objection stayed auditable. With no dry run there is
    # no objection to record, and it was appending nothing on every
    # approval. Nothing reads `approved_despite`; it is not a projection
    # somebody else is still rendering.

    # Approving the plan approves its checks — ONE act. Two mechanisms
    # would admit the state where the plan is approved and the checks are
    # not, and the story then works against a definition of done nobody
    # agreed to.
    if settled == "approve":
        from kernel.worker.adopt import adopt_checks
        await adopt_checks(conn, request_id, answered_by)
        # I5f — AND THE SAME ACT PINS A WAITING GRANT.
        #
        # Approving an errand's proposal authorises it to RUN unattended;
        # it cannot authorise a plan, because at that moment there is
        # none. So a fresh grant carries a NULL `approved_plan_hash` and
        # `standing_approval` — which requires a non-NULL one — covers
        # nothing. The feature is inert until the plan a person approved
        # is written onto it, and this is the only moment that plan and
        # that person are both known.
        #
        # HERE AND NOT IN THE CONTROL PANEL. Two callers reach an
        # approval — the CP endpoint and the console — and a rule
        # declared in one of them is a rule the other does not have.
        if is_plan:
            await pin_plan_to_grant(conn, request_id)
    row = await conn.fetchrow(
        "SELECT execution_id FROM questions WHERE id = $1", request_id)
    if row:
        # Un-parking starts a NEW leg, so it needs a new generation.
        #
        # A park ends the leg it happened in, and that leg's rows are
        # already journalled at the current generation. Resuming without
        # advancing re-runs step 0 at the same generation and collides on
        # the idempotency key — observed as
        # 'loop-5-gen-0-d0-step-0' the first time a human approved a plan
        # and the run tried to continue.
        #
        # Advanced here rather than at park time: parking is a stop, and a
        # run nobody ever answers should not silently accumulate
        # generations while it waits.
        q_kind = await conn.fetchval(
            "SELECT kind FROM questions WHERE id = $1", request_id)
        if q_kind == "landing":
            # MORE WORK FROM THE MERGE CARD. The only answer this park
            # takes from a person -- the other one is the merge itself,
            # which `story_landing` records without anybody clicking.
            #
            # NO NODE CYCLING HERE, and that is the difference from the
            # closeout arm below: nothing has merged yet, so the branch
            # and its PR are both still live. The next push updates the
            # PR the person was just looking at.
            from kernel.mailbox.api import (                # noqa: PLC0415
                submit_proposal)

            await submit_proposal(
                conn, answered_by or "person", "replan",
                {"reason": (
                    "A person read the pull request and asked for more "
                    "work rather than merging it. What they asked for: "
                    + ((answer or "").strip() or "(nothing said)")
                    + chr(10) * 2
                    + "Your branch is pushed and its PR is OPEN. Do "
                    "not start over, do not cut a new branch, and do not "
                    "close the PR -- you are continuing on the same "
                    "branch and your next push updates that same pull "
                    "request."
                    + chr(10) * 2
                    + "This is new work, so it needs its own plan: "
                    "call `register_plan` with steps and checks covering "
                    "what was asked for, and wait for it to be approved.")},
                f"replan-landing-{request_id}",
                row["execution_id"])
        elif q_kind == "pr_closed" and settled != "approve":
            # THEY MEANT IT. Closing the PR and then cancelling is one
            # decision expressed twice, so this is not a refusal to
            # record and argue with — the run ends and the workspace
            # goes.
            #
            # THROUGH THE MAILBOX, NOT BY WRITING CANCELLED HERE.
            # `apply_cancellation` is guarded on RUNNING, and this run is
            # PARKED_HOT until the un-park at the bottom of this
            # function — so calling it directly would be guarded out and
            # then the un-park would set the story back to RUNNING, which
            # is the opposite of what was asked for. A `cancel` proposal
            # is drained by `take_proposals` at the top of the resumed
            # leg, where the state is what the guard expects. Same path
            # every other cancel of a parked run takes.
            from kernel.mailbox.api import (                # noqa: PLC0415
                submit_proposal)

            await submit_proposal(
                conn, answered_by or "person", "cancel",
                {"reason": ("the PR was closed without merging and a "
                            "person chose to cancel the story rather "
                            "than carry on")},
                f"cancel-pr-closed-{request_id}",
                row["execution_id"])
        elif q_kind == "pr_closed":
            # KEEP GOING. The work is still on the branch and the
            # workspace is still up, so this is the same continuation
            # the closeout's "more work" is — including withdrawing the
            # plan, because whatever happens next is a new cycle and the
            # old approval covered the work that was just rejected.
            #
            # The node is left ESCALATED. Re-opening a PR is a thing a
            # person does on the forge, and the engine picks it up on
            # its next pass; guessing here would fight them for control
            # of a branch they just acted on.
            from kernel.mailbox.api import (                # noqa: PLC0415
                submit_proposal)

            await submit_proposal(
                conn, answered_by or "person", "replan",
                {"reason": (
                    "A person closed this story's PR without merging it, "
                    "and chose to keep working rather than cancel. What "
                    "they said: " + ((answer or "").strip() or "(nothing)")
                    + "\n\nYour work is still on the branch and your "
                    "workspace is intact — do NOT start over. The PR is "
                    "closed, which usually means something about the "
                    "change was wrong rather than something about the "
                    "code being broken; if they did not say what, ASK "
                    "before you build."
                    "\n\nThis is a new cycle, so it needs its own plan: "
                    "call `register_plan` with steps and checks covering "
                    "what you are going to do differently, and wait for "
                    "that to be approved.")},
                f"replan-pr-closed-{request_id}",
                row["execution_id"])
        elif q_kind == "closeout" and settled == "approve":
            # THE STORY IS CLOSED. The work is already on trunk — this
            # answered whether anything more belonged in THIS story, and
            # the answer was no. From here it is the ordinary ending:
            # review if it owes one, then DONE, then `reclaim` collects
            # the workspace on the story's own signal.
            #
            # `owes_a_landing` is false by construction (nothing is left
            # unmerged, which is why this park was opened at all), so
            # `finish_execution` cannot park it again.
            from kernel.worker.finish import finish_execution

            await finish_execution(
                conn, row["execution_id"],
                only_from=("PARKED_HOT", "PARKED_COLD"))
        elif q_kind == "closeout":
            # MORE WORK, AFTER THE MERGE. Same act as the acceptance arm
            # below and written the same way: the plan is withdrawn
            # here, and the un-park at the end of this function starts
            # the next leg.
            #
            # WHAT IS NOT DONE HERE is cutting the next node. The
            # branch this story rode is merged and its PR is closed, so
            # the next round needs a node of its own — and `git_nodes`
            # lives on the other side of a boundary the kernel may not
            # import across. `story_landing.sweep_resumed` does it, on
            # the same poll that moves every other story state, and
            # finds this run by exactly the shape left behind here: a
            # RUNNING execution whose newest closeout says `revise`.
            from kernel.mailbox.api import (                # noqa: PLC0415
                submit_proposal)

            await submit_proposal(
                conn, answered_by or "person", "replan",
                {"reason": (
                    "A person asked for more work rather than closing "
                    "this story. What they asked for: "
                    + (answer or "").strip()
                    + "\n\nThe work you already did has been MERGED to "
                    "trunk and its PR is closed. You stay on the SAME "
                    "branch — one feature branch per story — so do NOT "
                    "cut a new one, do NOT start over, do NOT revert "
                    "anything, and do not try to reopen the old PR. Bring "
                    "your tree up to date with trunk first (what you "
                    "already wrote is in there now), then build what was "
                    "asked for on top."
                    "\n\nYour approved plan covered the work you have "
                    "already done, and its checks are what your claim was "
                    "measured against. This is new work, so it needs its "
                    "own plan: call `register_plan` with steps and checks "
                    "that cover what was just asked for, and wait for that "
                    "to be approved before carrying on.")},
                f"replan-closeout-{request_id}",
                row["execution_id"])
        elif q_kind == "acceptance" and settled == "approve":
            # THE HUMAN SAID DONE. Verification already passed and the
            # work is already pushed (done-means-delivered ran before
            # the park); the person's acceptance is the last word, so
            # the run finishes instead of taking another leg.
            # ...unless it owes a review, in which case it takes one
            # more phase in the same pod before going terminal. The
            # decision is shared with the three other paths that mean
            # "finished" — see kernel/worker/finish.py.
            from kernel.worker.finish import finish_execution

            await finish_execution(
                conn, row["execution_id"],
                only_from=("PARKED_HOT", "PARKED_COLD"))
        else:
            # ── MORE WORK WITHDRAWS THE PLAN ─────────────────────────
            #
            # "Ask for more work" is a continuation route, and every
            # OTHER continuation route in this system withdraws the plan
            # approval on the way through: `worker/amend.py` submits a
            # `replan`, and `worker/reopen.py` lists one as one of the
            # three things reopening is made of. This route did not, and
            # it is the newest of the three -- it only exists because the
            # completion park replaced a grant that used to finish the
            # run outright.
            #
            # WHAT THAT COST, observed on errand 282. The plan gate is
            # once per EXECUTION, not once per leg: `requires_plan`
            # blocks tools until *a* plan is approved, so an approval in
            # leg 1 leaves the gate open for the rest of the run. Asked
            # for more work, the executor walked past it -- no
            # `register_plan` anywhere in leg 2 -- and ran a test suite
            # that was then judged against a contract derived from the
            # plan for DOCUMENTING ENDPOINTS. New work, old definition of
            # done, and nobody asked to approve the difference.
            #
            # SUBMITTED BEFORE THE UN-PARK, for the reason `_resume`
            # gives about its steer: `take_proposals` drains the mailbox
            # at the top of the step loop, before the first model call,
            # so a proposal submitted now is read before work continues.
            # After the un-park it would be a step late.
            #
            # ACCEPTANCE ONLY. A plan question answered `revise` or
            # `declined` already means "no approved plan" -- the gate is
            # shut by the absence of an approval, and a replan there
            # would withdraw one that was never given.
            if q_kind == "acceptance":
                from kernel.mailbox.api import (            # noqa: PLC0415
                    submit_proposal)

                # ONE REPLAN PER ACCEPTANCE QUESTION. The key is the
                # question, so a relay that retries -- which is exactly
                # what the mailbox exists to absorb -- withdraws the
                # plan once rather than once per attempt.
                await submit_proposal(
                    conn, answered_by or "person", "replan",
                    {"reason": (
                        "A person asked for more work rather than accepting "
                        "this as finished. What they asked for: "
                        + (answer or "").strip()
                        + "\n\nYour approved plan covered the work you have "
                        "already done, and its checks are what your claim "
                        "was measured against. This is new work, so it "
                        "needs its own plan: call `register_plan` with "
                        "steps and checks that cover what was just asked "
                        "for, and wait for that to be approved before "
                        "carrying on.")},
                    f"replan-acceptance-{request_id}",
                    row["execution_id"])

            # ── AND THE CONVERGENCE BUDGET RESTARTS, if a PERSON
            #    answered ─────────────────────────────────────────────
            #
            # The leg ceiling exists to end a run that will not converge
            # while nobody is watching. A person answering a park is
            # somebody watching: they have just read the plan or the
            # result and said what happens next. Charging that to the
            # same budget made the two workflows this card is built
            # around self-limiting -- twelve plan revisions, or four
            # rounds of "ask for more work" (two legs each, because the
            # replan withdraws the plan approval), and the run is FAILED
            # for "not converging".
            #
            # `generation + 1` is the generation this UPDATE is about to
            # write, so the mark is "count from the leg that is starting
            # now".
            #
            # GATED ON `human_uid`, NOT ON THE KIND. An agent must not be
            # able to reset it, or there is no ceiling: a model could ask
            # a `clarification`, have the assistant answer it, and buy
            # itself a fresh twelve legs -- and `clarification` is
            # deliberately assistant-answerable, which is what makes
            # asking cheap. The GO settling an autonomous errand's plan
            # alone is the same shape. Both are the unattended loop this
            # is guarding. `answered_by` is a free string an executor
            # fills in; `human_uid` is not, which is why H8a uses it.

        # ── AND THEN IT RUNS AGAIN ───────────────────────────
        #
        # OUTSIDE THE CHAIN, which it was not. This sat inside the
        # `else` arm while three arms above it -- `landing` and
        # `closeout` answered `revise`, and both `pr_closed` arms --
        # submitted a mailbox proposal and then fell off the end of
        # the chain still PARKED. Their own comments say "the un-park
        # at the bottom of this function", which is what it was meant
        # to be; the indentation said otherwise.
        #
        # WHAT THAT LOOKS LIKE. A person clicks "ask for more work" on
        # the merge card, the replan proposal lands in the mailbox,
        # and nothing reads it: `take_proposals` runs at the top of a
        # leg and a PARKED run takes no leg. The card is gone, the
        # question is ANSWERED, and the run sits PARKED_HOT for ever
        # with no open question and nothing to show a person. Seen on
        # run 100.
        #
        # SAFE FOR THE ARMS THAT FINISH INSTEAD. `finish_execution`
        # has already moved them out of PARKED_*, and this is guarded
        # on `state IN ('PARKED_HOT', 'PARKED_COLD')` -- so for those
        # it is a no-op rather than a resurrection.
        await conn.execute(
            "UPDATE executions "
            "SET state = 'RUNNING', generation = generation + 1, "
            "    convergence_generation = CASE WHEN $2::bool "
            "        THEN generation + 1 "
            "        ELSE convergence_generation END "
            "WHERE id = $1 AND state IN ('PARKED_HOT', 'PARKED_COLD')",
            row["execution_id"], bool((human_uid or "").strip()),
        )
