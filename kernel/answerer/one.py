"""Answering one claimed question, end to end.

Separated from the drain because the drain is fast database work and
this is a model call that can take minutes. Keeping them apart is what
lets the worker pass claim a question and move on.
"""

from __future__ import annotations

import logging

from kernel.answerer.agent import answer_question
from kernel.answerer.record import gather
from kernel.mailbox.api import submit_proposal

logger = logging.getLogger(__name__)

# Only a RUNNING story has work in flight to stop. Everything else is
# already stopped, and the card it is parked on is where a person acts.
HOLDABLE = ("RUNNING",)

_BROKE = ("Something went wrong while I was looking into this, so I have "
          "no answer for you. The work has not been touched or paused.")


async def _tree(conn, facts: dict) -> tuple[bytes, dict, str]:
    """The tree as it stands, the contract, and why there is no tree.

    Best effort. A story asked about before it has cloned anything, or
    one whose workspace has been reaped, is still worth answering from
    the record — a missing tree costs the file tools and nothing else.

    BUT "NO TREE" AND "THE TREE BROKE" ARE DIFFERENT, and conflating
    them hid a real bug for a whole story. `mounts_for` returns a flat
    tuple of mounts; this unpacked it as a pair, which raises for any
    story with more than two — and this except swallowed the ValueError,
    logged it at INFO as though the workspace simply were not there, and
    the answerer went on to tell a person "there is no repository tree
    available in this environment to inspect". Every word of that was
    false, and nothing in the output distinguished it from the honest
    case. A programming error must not be able to wear the costume of an
    empty workspace.

    NOTHING IS INJECTED. `archive_mounts` can write held-out check files
    into the copy it makes, which is how the Verifier tests behaviour the
    executor cannot tune against (F4). Putting them in front of an
    answerer whose every word goes to a person would leak them back
    through the one channel built to be helpful.
    """
    contract = {}
    if facts.get("contract_id"):
        row = await conn.fetchrow(
            "SELECT * FROM contracts WHERE id = $1", facts["contract_id"])
        contract = dict(row) if row else {}
    root = facts.get("workspace_path")
    if not root:
        return b"", contract, ""
    try:
        import asyncio

        from kernel.bridge.client import BRIDGE_HOST
        from kernel.verifier.kinds import TREE_PREFIX, VERIFIER_UID
        from kernel.verifier.tree import archive_mounts
        from kernel.workspace import podfs_client
        from kernel.workspace.mounts import mounts_for

        # A FLAT TUPLE OF MOUNTS. Not a pair — see above.
        mounts = await mounts_for(conn, facts["execution_id"])

        # THE TREE IS NOT ON THIS DISK, and reading as though it were is
        # how this came back empty for every pod execution.
        #
        # `archive_mounts` below opens files on THIS process's
        # filesystem. The answerer runs in the conductor -- the log line
        # is `question 207 claimed; answering beside the run` -- and on
        # Kubernetes the workspace claim is ReadWriteOncePod and belongs
        # to the execution's pod. So `/workspace/repos/<repo>` does not
        # exist here and every answer was made blind to the repository:
        #
        #   git rev-parse HEAD failed in /workspace/repos/<repo>: fatal:
        #   cannot change to '...': No such file or directory
        #
        # Measured on run 262. It degraded honestly -- `tree_error` said
        # the tree broke rather than that there was none -- but "what is
        # it doing with the tests" is mostly a question about the tree.
        #
        # THE SAME BRANCH `envsetup` AND `leg_env` TAKE, and the podfs
        # op already existed; nothing here is new except asking the pod
        # that has the disk.
        host = (facts.get("workspace_host") or "").strip()
        if host and host != BRIDGE_HOST:
            return await asyncio.to_thread(
                lambda: podfs_client.archive_mounts(
                    host, root, mounts, prefix=TREE_PREFIX,
                    uid=VERIFIER_UID,
                    execution_id=facts["execution_id"])), contract, ""
        return archive_mounts(root, mounts, prefix=TREE_PREFIX,
                              uid=VERIFIER_UID), contract, ""
    except (OSError, FileNotFoundError) as e:
        # The workspace is genuinely not readable. Expected on a reaped
        # or not-yet-provisioned run.
        logger.info("answerer: no tree for execution %s (%s); answering "
                    "from the record", facts.get("execution_id"), e)
        return b"", contract, ""
    except Exception as e:                                    # noqa: BLE001
        # Anything else here is OURS. Loud, with a traceback, and the
        # answerer is told so it can say "I could not read the tree"
        # rather than "there is no tree".
        logger.warning("answerer: could not build the tree for execution "
                       "%s — this is a fault, not an empty workspace: %s",
                       facts.get("execution_id"), e, exc_info=True)
        return b"", contract, f"{type(e).__name__}: {e}"


def _fold_in(text: str, hold: dict) -> str:
    """A fork that could not stop the work still has to be visible.

    The run was already stopped — in verification, parked on its own
    question, or finished — so there was nothing to hold. The decision
    does not evaporate because of that: it goes into the answer, where
    the person sees it beside whatever they were already being asked.

    NO TRAILER ABOUT THE STATE. This used to end with "the story is X,
    so there was no work in flight to stop" — and the card's own
    consequence line said "nothing changed", and the model's answer text
    had already claimed "the story is held". Three statements about one
    fact, disagreeing. The card says it once now; `hold_declined` is how
    it knows to.
    """
    out = [text, "", "---", "",
           "**This needs a decision from you.** " + hold["decision"].strip()]
    if hold.get("options"):
        out += ["", hold["options"].strip()]
    return "\n".join(out)


async def record_answer(conn, eid, pid, claim, got, held,
                        hold_declined: bool = False) -> int:
    return await conn.fetchval(
        """
        INSERT INTO answers (execution_id, proposal_id, asked_by, question,
                             answer, looked_at, held, failed, hold_declined)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
        """,
        eid, pid, claim.get("asked_by") or claim.get("actor_id") or "",
        claim.get("question") or "", got["text"],
        got.get("looked_at") or "", held, bool(got.get("failed")),
        hold_declined)


async def answer_one(conn, claim: dict) -> dict:
    """Answer one already-claimed question. Never raises."""
    eid, pid = claim["execution_id"], claim["proposal_id"]
    state = claim["state"]
    try:
        record, facts = await gather(conn, eid)
        facts["execution_id"] = eid
        tree, contract, tree_error = await _tree(conn, facts)
        from kernel.drivers.registry import make as make_driver

        got = await answer_question(make_driver(), claim.get("question") or "",
                                    record, tree, contract,
                                    tree_error=tree_error)
    except Exception as e:                                    # noqa: BLE001
        # A QUESTION MUST NOT VANISH. It cost a person the trouble of
        # asking, and silence is indistinguishable from never having
        # asked — so the failure becomes the answer.
        logger.warning("answerer failed on execution %s: %s", eid, e)
        got = {"text": _BROKE, "looked_at": "", "hold": None, "failed": True}

    # The state is re-read: answering took minutes, and a story that was
    # RUNNING when the question was claimed may have parked or finished
    # since. Holding one that already stopped would open a second card
    # about a run nobody is waiting on.
    now = await conn.fetchval("SELECT state FROM executions WHERE id = $1",
                              eid) or state
    hold = got.get("hold")
    can_hold = bool(hold) and now in HOLDABLE
    declined = bool(hold) and not can_hold
    if declined:
        got["text"] = _fold_in(got["text"], hold)

    answer_id = await record_answer(conn, eid, pid, claim, got, can_hold,
                                    hold_declined=declined)

    if not can_hold:
        return {"execution_id": eid, "action": "answered",
                "detail": f"answer {answer_id}; the work was not touched"}

    # THROUGH THE MAILBOX, at a step boundary. `intake` turns this into
    # the park; nothing out here may move a running run, because a leg
    # stopped between a tool call and its result is a leg the journal
    # records as something that did not happen.
    await submit_proposal(
        conn, "answerer", "hold",
        {"answer_id": answer_id, "question": claim.get("question") or "",
         "answer": got["text"], "decision": hold["decision"],
         "options": hold.get("options") or "",
         "why_now": hold.get("why_now") or ""},
        f"hold-{answer_id}", execution_id=eid)
    logger.info("execution %s: answered and HELD — %s",
                eid, hold["decision"][:160])
    return {"execution_id": eid, "action": "answered_held",
            "detail": f"answer {answer_id}; the run parks at its next "
                      f"step boundary"}
