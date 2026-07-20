"""DEPRECATED — superseded by app/channels/slack/ (generic channel system).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack integration module — bidirectional relay between Slack and the control panel.

Self-contained: imports from app/, but nothing in app/ imports from app/slack/.
Remove this module + the one include_router line in main.py to fully disable Slack.
"""
from app.slack.router import router as slack_router

__all__ = ["slack_router"]
