"""Ec4c — what a story may DO with a cloud resource.

`story_resources` says a story HOLDS a resource, with a relation: `reads`,
`produces`, `modifies`. That is the right vocabulary for a repository or a
document, where the resource is a thing and the relation is what you do to
it. It says almost nothing about a cloud account, where the resource is a
project and the interesting question is which of ten thousand operations
are permitted.

So a capability grant names VERBS — `logs.read`, `storage.list`,
`run.deploy` — a SCOPE they apply within, and a CEILING.

WHY THE CEILING IS THE LOAD-BEARING PART. Without it the only available
check is "is this verb in the grant", and the grant is written per story by
whoever set the story up. The ceiling is registered ONCE against the
resource by whoever owns the account, and every grant is checked against
it — so a story cannot be given more than the resource was ever registered
to allow, however the grant was written and whoever wrote it. That is what
makes it a grant rather than a wish.

Checked at write AND at read. A row written before somebody narrowed the
registration is a row that has to stop working when they do, and a check
only at write time would leave it live.

KIND VALIDATION IS DELEGATED, deliberately. `app/resource_kinds` is the
single source of truth for what a kind means, and "adding a kind is a
one-file change" stays true only while the kernel asks it rather than
keeping its own list. A second registry's first divergence is a kind the
control panel accepts and the kernel refuses, with nothing saying which is
right.

NOTHING HERE MINTS A CREDENTIAL. This decides what is permitted; turning a
verb set into a real token is per provider — GCP is `Ec4d`, AWS is `Ec4e` —
and both need cloud access E0 does not have.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# `service.action`, lowercase. Narrow because a verb is compared, listed
# on a card and mapped to a provider role: `Logs.Read ` and `logs.read`
# being different strings is a bug nobody sees until a grant silently
# fails to match.
_VERB = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*){1,3}$")

# A verb registered as a ceiling may end in `.*` — "every action on this
# service". A GRANT may not: expanding a wildcard at use time means the
# grant's meaning changes when the provider adds an action, and nobody
# reviewed that.
_CEILING_VERB = re.compile(
    r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*){0,3}(\.\*)?$")


class BadCapability(ValueError):
    """The verbs, the scope or the ceiling are not usable as written."""


def normalise(verbs) -> tuple:
    """Lowercased, de-duplicated, order-stable. Raises on a bad verb."""
    out, seen = [], set()
    for raw in verbs or ():
        verb = str(raw or "").strip().lower()
        if not verb:
            continue
        if not _VERB.match(verb):
            raise BadCapability(
                f"{raw!r} is not a verb: `service.action`, lowercase, e.g. "
                f"`logs.read`. A grant may not use `*` — a wildcard's "
                f"meaning changes when the provider adds an action, and "
                f"nobody reviewed that.")
        if verb not in seen:
            seen.add(verb)
            out.append(verb)
    if not out:
        raise BadCapability("a grant of no verbs is not a grant")
    return tuple(out)


def normalise_ceiling(verbs) -> tuple:
    """The registered ceiling. `service.*` is allowed here and only here."""
    out, seen = [], set()
    for raw in verbs or ():
        verb = str(raw or "").strip().lower()
        if not verb:
            continue
        if not _CEILING_VERB.match(verb):
            raise BadCapability(
                f"{raw!r} is not a ceiling entry: `service.action` or "
                f"`service.*`, lowercase")
        if verb not in seen:
            seen.add(verb)
            out.append(verb)
    return tuple(out)


def covers(ceiling, verb: str) -> bool:
    """Does the ceiling permit this verb? `logs.*` covers `logs.read`."""
    verb = str(verb or "").strip().lower()
    for entry in ceiling or ():
        entry = str(entry or "").strip().lower()
        if entry == verb:
            return True
        if entry.endswith(".*") and verb.startswith(entry[:-1]):
            return True
    return False


def above_ceiling(verbs, ceiling) -> tuple:
    """Verbs the ceiling does not permit. Empty means the grant is legal."""
    return tuple(v for v in verbs or () if not covers(ceiling, v))


def check(verbs, ceiling) -> str:
    """"" when the grant is within the ceiling, else why it is not.

    The message NAMES the excess and the ceiling. "refused" sends whoever
    wrote the grant to read a table; "you asked for `storage.delete` and
    this resource is registered for `logs.*`, `storage.list`" tells them
    either to narrow the grant or to widen the registration, which are
    different people's decisions.
    """
    try:
        wanted = normalise(verbs)
        allowed = normalise_ceiling(ceiling)
    except BadCapability as bad:
        return str(bad)
    if not allowed:
        return ("this resource has no registered capability ceiling, so "
                "nothing can be granted on it. Register what the account "
                "is permitted to do before granting any of it.")
    excess = above_ceiling(wanted, allowed)
    if excess:
        return (f"{', '.join(excess)} exceeds what this resource is "
                f"registered to allow ({', '.join(allowed)}). Narrow the "
                f"grant, or have the resource's owner widen the "
                f"registration — those are different decisions.")
    return ""


async def ceiling_for(conn, resource_id: str) -> tuple:
    """The registered ceiling, from the resource's own registration.

    Read off `metadata` rather than a column of its own: the control
    panel owns `goal_resources` and its kind registry decides what a
    registration carries, and the kernel adding a column to that table
    would be the kernel deciding what a resource IS.
    """
    raw = await conn.fetchval(
        "SELECT metadata FROM goal_resources WHERE id = $1", resource_id)
    # ASYNCPG RETURNS JSONB AS TEXT unless a codec is registered. An
    # `isinstance(..., dict)` check alone returns "no ceiling" for every
    # resource, which fails CLOSED and so reads as a working guard —
    # nothing is granted, nothing complains, and the feature is dead.
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ()
    if not isinstance(raw, dict):
        return ()
    return normalise_ceiling(raw.get("capability_ceiling") or ())


async def granted_verbs(conn, execution_id: int) -> dict:
    """`{resource_id: (verbs…)}` this run may use, live and within ceiling.

    RE-CHECKED AGAINST THE CEILING HERE. A grant written when the
    registration was wider is a grant that must stop working when it
    narrows, and a check only at write time would leave it live. The
    database CHECK cannot do it either: it compares against the ceiling
    COPIED onto the row, which is exactly the value that goes stale.
    """
    rows = await conn.fetch(
        """
        SELECT g.resource_id, g.verbs, g.scope, g.expires_at
          FROM kernel.capability_grants g
          JOIN kernel.executions e ON e.goal_id = g.goal_id
         WHERE e.id = $1
           AND g.revoked_at IS NULL
           AND (g.expires_at IS NULL OR g.expires_at > now())
           AND (g.story_id IS NULL OR g.story_id = e.story_id)
        """, execution_id)
    out: dict = {}
    for row in rows:
        ceiling = await ceiling_for(conn, row["resource_id"])
        live = tuple(v for v in (row["verbs"] or ()) if covers(ceiling, v))
        dropped = set(row["verbs"] or ()) - set(live)
        if dropped:
            # LOUD. A grant that quietly lost half its verbs looks to the
            # executor like a provider outage, and to everybody else like
            # nothing happened.
            logger.warning(
                "execution %s: grant on %s lists %s, above the resource's "
                "registered ceiling %s — those verbs are not in effect",
                execution_id, row["resource_id"], sorted(dropped),
                sorted(ceiling))
        if live:
            out[row["resource_id"]] = tuple(sorted(set(
                out.get(row["resource_id"], ()) + live)))
    return out


def permits(granted: dict, resource_id: str, verb: str) -> bool:
    return str(verb or "").strip().lower() in (granted.get(resource_id) or ())
