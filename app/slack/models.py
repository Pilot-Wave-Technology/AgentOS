"""DEPRECATED — superseded by app/channels/slack/ (generic channel system).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Pydantic models for Slack event and action payloads."""
from typing import Optional
from pydantic import BaseModel


class SlackEventChallenge(BaseModel):
    """Slack URL verification challenge."""
    type: str  # "url_verification"
    challenge: str
    token: str


class SlackEventWrapper(BaseModel):
    """Outer wrapper for all Slack Events API payloads."""
    type: str  # "event_callback" | "url_verification"
    token: Optional[str] = ""
    team_id: Optional[str] = ""
    event: Optional[dict] = None
    challenge: Optional[str] = ""
    event_id: Optional[str] = ""


class SlackActionPayload(BaseModel):
    """Slack interactive action payload (button click, modal submit)."""
    type: str  # "block_actions" | "view_submission"
    trigger_id: Optional[str] = ""
    user: Optional[dict] = None
    channel: Optional[dict] = None
    message: Optional[dict] = None
    actions: Optional[list] = None
    view: Optional[dict] = None
    team: Optional[dict] = None
