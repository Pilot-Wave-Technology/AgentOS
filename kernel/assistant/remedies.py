"""What can be done about a stuck story, and by whom.

WHY THIS IS ITS OWN MODULE. "Here is the error" is not help. The
assistant was already able to say `stalled: workspace not ready: clone
failed` and a person reading that still has to know whether it retries
by itself, whether they have to do something, and what. Everything in
this file is that second half, and it is separated so that adding a new
failure mode means adding a rule here rather than growing prose in a
reader.

WHO IS THE LOAD-BEARING FIELD. Three answers and they are not
interchangeable:

    nobody   the system retries; doing anything is worse than waiting
    you      a person must act, and the action is outside the kernel
    assistant  the assistant can do it now, with a tool it holds

Getting this wrong in the cheerful direction is the expensive one: an
assistant that says "I'll retry" about a permanent credential failure
costs somebody the twenty minutes before they look themselves.
"""

from __future__ import annotations

NOBODY = "nobody"
YOU = "you"
ASSISTANT = "assistant"


def _r(do: str, who: str, because: str = "") -> dict:
    return {"do": do, "who": who, **({"because": because} if because else {})}


def for_stall(reason: str) -> list:
    """Remedies for whatever `executions.stall_reason` is saying."""
    low = (reason or "").lower()
    if not low:
        return []
    if "workspace" in low:
        return [
            _r("wait — dispatch retries the workspace every tick, and a "
               "transient clone failure clears itself", NOBODY),
            _r("if it has not cleared in a few minutes, the clone or the "
               "git credential is failing permanently; check the repo "
               "resource and its token, then press Retry on the stall "
               "card in this thread", YOU,
               "a run with no workspace is never dispatched, deliberately: "
               "a sandbox with an empty tree invents work to fill it"),
        ]
    if "verification crashed" in low or "adjudicator" in low:
        return [
            _r("this is a platform fault, not a judgement on the work — "
               "the run is parked rather than looping", NOBODY),
            _r("once the cause is fixed, press Retry on the stall card "
               "in this thread (or on the Executions board); three "
               "crashes in a row park it for a person on purpose", YOU),
        ]
    if "contract_refused" in low:
        return [_r("the story's contract was refused before it ran; amend "
                   "the scope or criteria and start it again", ASSISTANT)]
    if low.startswith("admission:"):
        # QUEUED IS NOT STUCK, and the fallback below said the opposite.
        # Admission is level-triggered — it runs every tick and admits
        # this row the moment a slot frees — so "nothing retries this
        # automatically" is exactly backwards, and acting on it means
        # restarting a run that was about to start by itself.
        #
        # The two ceilings need different answers, which is why the
        # reason names which one. Behind their own, a person has a real
        # choice; behind the platform's, they have none and should be
        # told so rather than left looking for a lever.
        if "the system is at its ceiling" in low:
            return [_r("nothing to do — the platform is full and this "
                       "starts the moment any goal's run finishes; it "
                       "keeps its place in line", NOBODY,
                       "admission runs every tick, so a queued run needs "
                       "no nudge and restarting it only loses its place")]
        return [_r("nothing to do if the wait is acceptable — it starts "
                   "as soon as one of the runs ahead of it finishes",
                   NOBODY),
                _r("to start it sooner, finish or stop one of the runs "
                   "already going in this goal", YOU,
                   "the limit is per person per goal, so another goal's "
                   "work is not what is in the way")]
    return [_r("read the reason above and decide; nothing retries this "
               "automatically", YOU)]


def for_question(kind: str) -> list:
    """Remedies for whatever the run is parked on."""
    if kind == "authorization":
        return [_r("approve, revise or decline the plan — approval is "
                   "yours alone and the run cannot move without it", YOU),
                _r("relay a revise or a decline with what should change",
                   ASSISTANT, "the assistant may carry a rejection, never "
                              "an approval")]
    if kind == "acceptance":
        return [_r("mark it done, or say what more the story should do",
                   YOU, "verification passed and the work is pushed; only "
                        "a person calls it finished")]
    if kind == "closeout":
        # NOT A REPEAT OF THE ACCEPTANCE, and the remedy has to say
        # which one this is or it reads as the system asking twice.
        # The first asked about work that was pushed; this one is
        # asked after it reached trunk, and it is the last moment
        # the workspace still exists.
        return [_r("close the story, or say what more it should do — "
                   "the work is merged, and this is the last point "
                   "at which more can be added to THIS story rather "
                   "than a new one", YOU,
                   "the workspace is still up; once this is closed "
                   "it is released")]
    if kind == "pr_closed":
        # NOT A FAILURE REPORT. Somebody closed the PR on purpose,
        # and the assistant saying "something went wrong" would be
        # telling them their own act was a fault.
        return [_r("say whether to keep working on this story or "
                   "cancel it — the PR was closed without merging, "
                   "which only you can interpret", YOU,
                   "the work is still on the branch and the "
                   "workspace is still up, so keeping going costs "
                   "nothing that has not already been spent")]
    if kind == "escalation":
        return [_r("decide whether to retry or abandon — the run stopped "
                   "asking so it would stop looping", YOU)]
    if kind == "direction":
        # THE ONE PARK NOBODY ASKED FOR. It follows a question a person
        # put to the story, so the remedy has to connect the two: the
        # commonest confusion is somebody reading "your story stopped"
        # without realising their own question caused it.
        return [_r("say whether to carry on as planned or change the "
                   "plan — your question turned something up and the "
                   "work is parked until you answer", YOU),
                _r("relay their decision — this one you may carry, "
                   "because carrying on authorises no new work; it "
                   "resumes the plan they already approved", ASSISTANT,
                   "if they want something different, that is a new "
                   "cycle: the executor plans and they approve before "
                   "anything is written")]
    if kind == "clarification":
        return [_r("answer the executor's question", YOU),
                _r("relay the answer once you have it", ASSISTANT)]
    return [_r("answer the open question", YOU)]


# Every engine failure ends the same way: a person fixes something at
# GitHub, and then something has to tell the engine to stop giving up.
# Naming the button once means the four branches below stay about the
# CAUSE, which is the part that differs.
_THEN_RETRY = (" Then press Retry on the git-engine card in this thread "
               "— the engine does not retry an escalated branch on its "
               "own, so a fixed permission changes nothing until "
               "somebody says so.")


def for_node_error(branch: str, error: str) -> list:
    """Remedies for a git engine failure, which nothing else surfaces."""
    low = (error or "").lower()
    if "403" in low or "permission" in low or "scope" in low:
        return [_r(f"the engine was refused on `{branch}` — the GitHub "
                   f"app installation is missing a permission, or its "
                   f"token was minted before one was added. Re-check the "
                   f"app's repo access." + _THEN_RETRY, YOU)]
    if "conflict" in low or "merge" in low:
        return [_r(f"`{branch}` conflicts with its trunk; the engine "
                   f"cannot resolve it and will not guess." + _THEN_RETRY,
                   YOU)]
    if "404" in low or "not found" in low:
        return [_r(f"the engine cannot see `{branch}` or its repo — the "
                   f"branch was deleted, or the app lost access."
                   + _THEN_RETRY, YOU)]
    return [_r(f"the git engine failed on `{branch}` and recorded the "
               f"error above." + _THEN_RETRY, YOU)]


def for_state(state: str, has_workspace: bool, mounts: int) -> list:
    """Remedies for a run sitting in a state longer than expected."""
    if state == "PENDING" and mounts and not has_workspace:
        return [_r("waiting on its workspace — the checkouts are being "
                   "prepared, and it dispatches once every one is there",
                   NOBODY)]
    if state == "PENDING" and not mounts:
        return [_r("this story holds no resources, so there is nothing to "
                   "check out and it cannot start. Ask GO to grant what "
                   "it needs to touch", ASSISTANT)]
    if state == "PENDING":
        return [_r("waiting for a concurrency slot; it starts when a "
                   "running story in this goal finishes", NOBODY)]
    if state == "VERIFYING":
        return [_r("the verifier is rebuilding the environment and "
                   "running the checks offline; a first build is the slow "
                   "one", NOBODY)]
    return []
