"""Which OAuth scopes this integration needs, and why each one.

THERE WERE THREE LISTS AND THEY DISAGREED. The OAuth consent URL, the
manifest the admin page generates, and the manifest template in
documentation/ each hardcoded their own set. Slack grants the
INTERSECTION of what the manifest declares and what the install
requests, so a scope present in two of the three is silently not
granted -- and the failure arrives much later as a `missing_scope` on
one API call, inside an `except SlackApiError` that logs a warning and
drops the message.

Two things were actually wrong when this was written:

  * `files:write` was in none of the lists, while actions.py calls
    `files_upload_v2` in two places. Any plan or diff too large for a
    modal failed, silently.
  * `reactions:write` and `mpim:read` were requested everywhere and
    called nowhere.

So the list is derived from the Slack API methods the code calls,
rather than maintained by hand beside them. Add a method to the map
when you first call it; the test walks the module for real call sites
and fails if one has no entry.
"""

from __future__ import annotations

# Slack Web API method -> the scopes that method needs.
# Keys are the slack_sdk method names, exactly as called.
METHOD_SCOPES: dict[str, tuple[str, ...]] = {
    # posting
    "chat_postMessage": ("chat:write",),
    "chat_update": ("chat:write",),
    "chat_delete": ("chat:write",),
    "chat_postEphemeral": ("chat:write",),
    # we post under a per-agent display name, which is its own scope
    "_custom_username": ("chat:write.customize",),
    # channels
    "conversations_create": ("channels:manage", "groups:write"),
    "conversations_join": ("channels:join",),
    "conversations_info": ("channels:read", "groups:read"),
    "conversations_list": ("channels:read", "groups:read"),
    "conversations_members": ("channels:read", "groups:read"),
    "conversations_invite": ("channels:manage", "groups:write"),
    "conversations_kick": ("channels:manage", "groups:write"),
    "conversations_archive": ("channels:manage", "groups:write"),
    "conversations_unarchive": ("channels:manage", "groups:write"),
    "conversations_open": ("im:write",),
    # people
    "users_info": ("users:read",),
    "users_lookupByEmail": ("users:read.email",),
    # files
    "files_info": ("files:read",),
    "files_upload_v2": ("files:write",),
    # who are we? needs nothing beyond a valid token, and is how we tell
    # a channel this bot created from one it merely can see
    "auth_test": (),
    # modals need no scope beyond the interactivity the app already has
    "views_open": (),
    "views_update": (),
}

# Scopes that no single method implies -- they come from the app's
# subscriptions rather than from a call we make.
SUBSCRIPTION_SCOPES: tuple[str, ...] = (
    "commands",             # slash commands
    "app_mentions:read",    # app_mention
    "channels:history",     # message.channels
    "groups:history",       # message.groups
    "im:history",           # message.im
    "im:read",              # message.im metadata
)

# The events we subscribe to. One list, so the manifest generator and the
# documented template cannot drift (the template omitted `message.im`
# while the generator included it).
BOT_EVENTS: tuple[str, ...] = (
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "file_shared",
    "member_joined_channel",
    "member_left_channel",
)


def bot_scopes() -> list[str]:
    """Every scope the bot token needs, sorted and de-duplicated."""
    out: set[str] = set(SUBSCRIPTION_SCOPES)
    for scopes in METHOD_SCOPES.values():
        out.update(scopes)
    return sorted(out)


def scope_string() -> str:
    """Comma-separated, as the OAuth consent URL wants it."""
    return ",".join(bot_scopes())
