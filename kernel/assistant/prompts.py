"""The prompt files, and the one place that decides what they say.

SOUL.md and AGENTS.md sit in this package next to the tool definitions
they describe, so H8b can read the prompt and `TOOLS` together and
assert the prompt never names a tool the surface does not serve. That
adjacency is the point: the check is only possible because both halves
are in one place.

WHAT WENT WRONG. The template migration embedded a COPY of both files as
SQL string literals, so the bytes the model actually receives came from
the migration and the bytes the gate read came from disk. Two sources of
truth for one artifact, and no check compared them.

They drifted within a day. A section added to AGENTS.md -- telling the
assistant to check `can_write` before naming a path in its criteria --
passed H8b and never reached the model, because H8b reads the file and
the model reads the database. A gate that compares one half of a
surface to itself proves nothing about the other half; this project has
now found that same shape three times in one week.

So the FILES are authoritative and this runs on every startup. The
migration's copy still seeds a fresh database, which is what migrations
are for, and stops mattering the moment the service comes up. Editing
an applied migration is a no-op anyway -- it has already run.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_ID = "kernel-assistant"
FILES = ("SOUL.md", "AGENTS.md")

# Every kernel-authored template syncs the same way: the files are the
# source of truth, the DB copy is a seed. G7 added the GO's.
SYNCED_TEMPLATES = (
    ("kernel-assistant", HERE),
    ("go-v2", os.path.join(os.path.dirname(HERE), "go")),
)


def on_disk(root: str | None = None) -> dict:
    """The prompt files as text, keyed by filename."""
    out = {}
    for name in FILES:
        path = os.path.join(root or HERE, name)
        with open(path, encoding="utf-8") as fh:
            out[name] = fh.read()
    return out


async def sync_all(conn) -> dict:
    """Reconcile every kernel-authored template's prompts from disk."""
    out = {}
    for template_id, root in SYNCED_TEMPLATES:
        exists = await conn.fetchval(
            "SELECT 1 FROM templates WHERE id = $1", template_id)
        if not exists:
            # A template not yet migrated is not an error -- the seed
            # migration creates it, and the next startup syncs it.
            continue
        out[template_id] = await sync(conn, template_id=template_id,
                                      root=root)
    return out


async def sync(conn, template_id: str = TEMPLATE_ID,
               root: str | None = None) -> dict:
    """Push the files into `template_files`, and say what changed.

    Idempotent and cheap -- two short reads and at most two writes. It
    reports the filenames it actually rewrote rather than a count, so a
    drift that should not have happened is visible in the log instead of
    being silently repaired every boot.
    """
    changed = []
    # THE BARE NAMES TOO. `changed` carries a human sentence with byte
    # counts in it, and the control panel needs to know WHICH FILES moved
    # so it can push those to live agents -- parsing a name back out of
    # "AGENTS.md (1234b -> 1240b)" is the kind of thing that works until
    # somebody improves the message.
    changed_files = []
    for name, content in on_disk(root).items():
        current = await conn.fetchval(
            "SELECT content FROM template_files "
            "WHERE template_id = $1 AND filename = $2",
            template_id, name)
        if current == content:
            continue
        await conn.execute(
            """
            INSERT INTO template_files (template_id, filename, content,
                                        updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (template_id, filename)
            DO UPDATE SET content = $3, updated_at = now()
            """,
            template_id, name, content)
        changed.append(f"{name} ({len(current or '')}b -> {len(content)}b)")
        changed_files.append(name)

    if changed:
        # WARNING, not info. Reaching here means the database and the
        # repository disagreed about what the assistant was told, and
        # somebody should know which way it was resolved.
        logger.warning("%s prompts: rewrote %s from disk",
                       template_id, ", ".join(changed))
    return {"template_id": template_id, "changed": changed,
            "changed_files": changed_files}
