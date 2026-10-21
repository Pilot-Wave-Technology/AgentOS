"""The documents a local story was granted, listed for the extension.

THE WORKSPACE ALREADY PROMISED THESE. `local_bundle._layout_note`
draws the checkout root for every local session and names a directory
in it:

    |- docs/               resources served to you  (READ-ONLY)

and nothing in the local lane ever created it. The pod lane does --
`mounts.story_resources` maps every granted document to `docs/<name>`
and the workspace builder fetches the bytes through
`/internal/resource-object` -- so a story worked in a pod reads its
brief and the same story worked in the editor cannot, while being told
in its own CLAUDE.md that it can. Observed on CALY5/f4f2e9c4: imported
locally with resources attached, no `docs/` on disk.

That is worse than the resources simply being absent. A session told
where to find something it then cannot find does not conclude the
system is wrong; it concludes it has misread the layout, and goes
looking.

THE GRANT IS THE LIST, and `story_resources` is the grant table. Not
the goal's catalogue: a goal may hold twenty documents and a story be
entitled to two, and handing the editor all twenty would both leak and
drown. This is the same join `/internal/resource-object` authorises
each individual read against, so the list and the permission cannot
drift apart.

DOCUMENTS ONLY. Repositories are cloned, not downloaded, and arrive
through `repos[]` with their own tokens; artifacts are what the story
WRITES. `DOC_KINDS` is the kernel's own list, imported rather than
retyped -- the two lanes disagreeing about what counts as a document
is exactly the drift this module exists to prevent.
"""

from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

# The kernel's own list, so the two lanes cannot disagree. `doc` is the
# kernel's older word and `document` is what the control panel writes
# on upload; both are documents, and that mismatch has cost a story
# before.
try:                                                       # pragma: no cover
    from kernel.workspace.mounts import DOC_KINDS
except Exception:                                          # noqa: BLE001
    DOC_KINDS = ("doc", "document", "file", "dataset")

# A filename, not a path. The name comes from the catalogue, which a
# person typed, so it can hold anything -- and `docs/../../id_rsa` is
# the obvious thing it must never hold.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name: str, resource_id: str) -> str:
    """A filename safe to join under `docs/`.

    Path separators, traversal and control characters are stripped
    rather than rejected: a resource whose name is awkward should still
    arrive, under a name the session can read.
    """
    cleaned = _UNSAFE.sub("-", (name or "").strip()).strip(". -")
    # `..` survives the character filter (both characters are allowed
    # in a filename), so it is removed by name.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned or f"resource-{resource_id[:8]}"


async def docs_for_story(pool, story_id: str) -> list[dict]:
    """Every granted document this story can be handed, as files.

    Returns `[{resource_id, name, filename, kind}]`. A grant with no
    stored object is skipped: it is a catalogue entry with nothing
    behind it, and listing it would promise the extension a download
    that can only 404.
    """
    if not story_id:
        return []
    try:
        rows = await pool.fetch(
            """
            SELECT gr.id::text AS resource_id, gr.name, gr.kind
              FROM story_resources sr
              JOIN goal_resources gr ON gr.id = sr.resource_id
             WHERE sr.story_id = $1
               AND gr.kind = ANY($2::text[])
               AND COALESCE(NULLIF(gr.uri, ''),
                            NULLIF(gr.gcs_uri, '')) IS NOT NULL
             ORDER BY gr.name
            """, story_id, list(DOC_KINDS))
    except Exception as exc:                                # noqa: BLE001
        # NEVER FATAL. This rides on the hydration bundle, and a story
        # whose documents cannot be listed must still be workable --
        # the repo, the persona and the plan are the parts without
        # which nothing happens at all.
        _log.warning("local docs: could not list for story %s: %s",
                     story_id, exc)
        return []

    out, seen = [], set()
    for r in rows:
        fname = safe_name(r["name"], r["resource_id"])
        # TWO DOCUMENTS, ONE FILENAME. The catalogue does not enforce
        # unique names and the sanitiser can collapse two into one, so
        # the id disambiguates rather than one silently overwriting the
        # other on disk.
        if fname in seen:
            stem, _, ext = fname.rpartition(".")
            fname = (f"{stem}-{r['resource_id'][:8]}.{ext}" if stem
                     else f"{fname}-{r['resource_id'][:8]}")
        seen.add(fname)
        out.append({"resource_id": r["resource_id"], "name": r["name"],
                    "filename": fname, "kind": r["kind"]})
    return out
