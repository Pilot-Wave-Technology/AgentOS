"""Notion and Notion Bot channel provisioners.

Called by the generic channel enable flow and member-join hook to create
Notion page structures and per-member subpages.

NotionBotProvisioner subclasses NotionProvisioner and only overrides
provision_goal (to fall back to the OAuth integration's database) and
provision_member (to use the notion_bot platform row).
"""
from .classes import NotionProvisioner, NotionBotProvisioner
from ._member import bind_pending_shca_subpage

__all__ = [
    "NotionProvisioner",
    "NotionBotProvisioner",
    "bind_pending_shca_subpage",
]
