"""I5f — the endpoints a person uses to decide GO's errand proposals.

ITS OWN MODULE, not two more handlers on `kernel_park.py`. That file is
about a run that has already started and is waiting; this is about work
that has not been authorised to start at all. They share a membership
rule and nothing else.

THE MEMBERSHIP RULE IS THE FEATURE. "Any human in the goal may approve"
is the design's own words, and `in the goal` is the half that has to be
enforced — I5a found all three park endpoints taking a goal in the path
and authorising nobody against it, which meant any signed-in user could
approve any plan in any goal. Building autonomy on top of that would have
been a standing grant on an open door.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import get_current_user
from app.database import get_pool
from app.routers.kernel_park import _require_member
from app.services.errand_grants import ProposalError, approve, decline

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/goals/{pid}/kernel/errand-proposals")
async def list_proposals(pid: str, state: str = "PENDING",
                         user: dict = Depends(get_current_user)):
    """What GO has asked for and nobody has answered."""
    await _require_member(str(user.get("uid") or ""), pid)
    rows = await get_pool().fetch(
        """
        SELECT id, proposed_by, domain, task, name, rationale, cron,
               max_runs, expires_at, state, decided_by, decided_at,
               decline_reason, schedule_id, created_at
          FROM kernel.errand_proposals
         WHERE goal_id = $1 AND ($2 = '' OR state = $2)
         ORDER BY created_at DESC
         LIMIT 100
        """, pid, (state or "").strip().upper())
    return {"goal": pid, "proposals": [dict(r) for r in rows]}


@router.post("/goals/{pid}/kernel/errand-proposals/{proposal_id}/decide")
async def decide_proposal(pid: str, proposal_id: int, request: Request,
                          user: dict = Depends(get_current_user)):
    """A person in the goal approves or declines an errand GO proposed.

    ANY MEMBER, which is wider than the rest of the kernel's approvals
    and is meant to be: an errand GO thought of belongs to nobody in
    particular, and requiring one named person would make autonomy wait
    on whoever happens to be reading.

    THE APPROVER IS THE OWNER. Their uid goes on the grant, their
    assistant thread drives every run of it, and `principal_for` makes
    them the ledger principal and the cost centre for the life of the
    grant. That is the decision recorded in errand_integration.md and it
    is why this endpoint takes no `on_behalf_of`.
    """
    uid = str(user.get("uid") or "")
    await _require_member(uid, pid)

    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        body = dict(await request.form())
    verdict = str(body.get("verdict") or "").strip().lower()
    if verdict not in ("approve", "decline"):
        raise HTTPException(
            status_code=400,
            detail="verdict must be `approve` or `decline`")

    pool = get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            if verdict == "approve":
                out = await approve(conn, pid, proposal_id, uid)
            else:
                out = await decline(conn, pid, proposal_id, uid,
                                    str(body.get("reason") or ""))
    except ProposalError as refused:
        raise HTTPException(status_code=refused.status,
                            detail=refused.detail) from None
    # THE BUTTONS MUST NOT COME BACK ON RELOAD. Without this the card
    # renders live again for a proposal already settled, the second click
    # gets a 409, and a correct refusal reads as a broken page. Same
    # stamp the park cards use, and on the control panel's own pool
    # because `messages` is its table rather than the kernel's.
    await pool.execute(
        "UPDATE messages SET card_data = card_data || "
        "jsonb_build_object('decided', $2::text) "
        "WHERE card_data->>'card_type' = 'errand_proposal' "
        "AND card_data->>'proposal_id' = $1",
        str(proposal_id), verdict)

    log.info("goal %s: errand proposal %s %sd by %s", pid, proposal_id,
             verdict, uid)
    return out
