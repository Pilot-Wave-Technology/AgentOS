"""What a channel is called, and why the thread id is in the name.

AN ARCHIVED SLACK CHANNEL KEEPS ITS NAME FOREVER. You can never create
a second channel with the name of an archived one, and
`conversations.list` does not return archived channels -- so the old
collision handler asked Slack for the channel that blocked it, failed
to find it, and raised "reported name_taken but not found in list".
Every re-imported story would have hit that, because a story re-opened
after its channel was archived wants the same name.

So the name is keyed on the THREAD id, which is new on every import,
rather than on the story id, which is not. A re-imported story gets a
new thread, and therefore a name nothing has ever held.

Story titles cannot carry uniqueness and are not asked to:

  * two goals can both hold "Fix login bug"
  * a CJK or emoji title slugifies to nothing, and the old code fell
    back to the literal "user" -- so twenty such stories all wanted
    the same name
  * titles that agree in their first N characters truncate together

The slug is therefore decoration for humans. NOTHING RESOLVES A
CHANNEL BY NAME -- the binding holds the channel id, and that is the
identity. The name can be wrong, stale or renamed by hand and the
system is unaffected.
"""

from __future__ import annotations

import re

SLACK_MAX = 80          # Slack's channel-name limit
THREAD_KEY = 8          # hex chars of thread id — the same prefix the UI shows


def slugify(text: str, max_len: int) -> str:
    """Lowercase, [a-z0-9-] only, collapsed, trimmed. May return ""."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def channel_name(*, goal_id: str, thread_id: str, title: str = "",
                 env_suffix: str = "") -> str:
    """`<env>-<goal>-<thread8>-<slug>`, always within Slack's 80 chars.

    `env_suffix` comes in as "" for production or "dev"/"feature"
    otherwise, and goes at the FRONT: all environments share one
    workspace, and a prefix sorts them together in Slack's channel list
    where a suffix scatters them.

    The thread key is what makes this unique, so it is never truncated;
    the slug absorbs whatever room is left and may vanish entirely,
    which is fine — it is for reading, not for finding.
    """
    env = slugify(env_suffix, 12)
    goal = slugify(goal_id, 20)
    key = re.sub(r"[^a-z0-9]", "", (thread_id or "").lower())[:THREAD_KEY]
    if not key:
        raise ValueError("channel_name needs a thread id — it is what makes "
                         "the name unique, and an archived channel keeps its "
                         "name forever")

    head = "-".join(p for p in (env, goal, key) if p)
    room = SLACK_MAX - len(head) - 1          # -1 for the joining hyphen
    slug = slugify(title, room) if room > 2 else ""
    return f"{head}-{slug}" if slug else head
