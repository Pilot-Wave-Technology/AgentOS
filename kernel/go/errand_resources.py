"""What an autonomous errand is granted, checked when it is PROPOSED.

`propose_errand` could not say what the errand needed, and the approval
path created the story with a bare INSERT, so the story was
structurally incapable of holding resources. On KER25 proposal 903 the
task read "run pytest against multi_agent_dummy_repo on cal_kernel" and
the story held nothing -- so the run spent its legs looking for a
repository nobody had given it.

VALIDATED HERE, NOT AT THE SWEEP, for the same reason `E_BAD_CRON` is:
an unusable proposal discovered by the worker pauses a schedule a person
has already approved, and the refusal reaches a log instead of the agent
that wrote it. GO gets told while it is still the one holding the pen.

OMITTED IS NOT EMPTY. `None` means GO did not think about it and is
refused; `[]` means "this errand genuinely needs no workspace", which is
true of some (counting stories, reading the goal's record). A silent
default is exactly what produced the bug.
"""

from __future__ import annotations

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id
from kernel.workspace import errand_write

# The relations a grant may carry. Same vocabulary as `grant_resource`:
# `needs` = read, `produces`/`modifies` = write. Nothing else means
# anything, and a typo here becomes a story nobody can start.
RELATIONS = ("needs", "produces", "modifies")

# Cloud reach is always read-only -- a run ACTS AS an identity, it never
# edits one. Restated from clouds.py's rule rather than re-derived,
# because the refusal has to name the kind and this is where it is made.
READ_ONLY_KINDS_HINT = ("gcp_", "aws_", "azure_")


class BadResources(Exception):
    """The proposal names resources it cannot have. Carries a code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def validate(conn, goal_id: str, resources) -> list:
    """Normalise and check, or raise `BadResources`.

    Returns a list of `{"resource_id", "relation", "why"}` ready to be
    stored on the proposal and replayed as grants at approval.
    """
    if resources is None:
        raise BadResources(
            "E_NO_RESOURCES",
            "An errand needs its resources named when it is proposed -- "
            "nobody can grant them later, because the story is created "
            "at approval and there is no second call. Pass `resources` "
            "with the ids from the goal's catalogue and a relation for "
            "each ('needs' to read, 'modifies' to change). If this "
            "errand genuinely needs no workspace at all, pass an empty "
            "list and say why in the rationale.")
    if not isinstance(resources, (list, tuple)):
        raise BadResources(
            "E_BAD_RESOURCES",
            f"`resources` is a list of "
            f"{{resource_id, relation, why}}, not {type(resources).__name__}.")

    out, seen = [], set()
    for item in resources:
        entry = await _one(conn, goal_id, item, seen)
        out.append(entry)
        seen.add((entry["resource_id"], entry["relation"]))
    return out


async def _one(conn, goal_id: str, item, seen) -> dict:
    if not isinstance(item, dict):
        raise BadResources(
            "E_BAD_RESOURCES",
            f"each entry is an object with `resource_id` and `relation`; "
            f"got {type(item).__name__}.")

    rid = str(item.get("resource_id") or "").strip()
    relation = str(item.get("relation") or "").strip().lower()
    why = str(item.get("why") or "").strip()

    if not rid:
        raise BadResources("E_BAD_RESOURCES",
                           "an entry has no `resource_id`.")
    if relation not in RELATIONS:
        raise BadResources(
            "E_BAD_RELATION",
            f"{relation or '(empty)'!r} is not a relation. Use 'needs' to "
            f"read, 'produces' or 'modifies' to write.")
    if (rid, relation) in seen:
        raise BadResources("E_BAD_RESOURCES",
                           f"{rid} is granted {relation} twice.")

    # By name or by id, and the catalogue named on a miss -- the same
    # question the other four resource tools ask. This one raises
    # instead of returning, so the refusal is carried across by hand.
    resolved = await resolve_resource_id(conn, goal_id, rid)
    if isinstance(resolved, Refused):
        raise BadResources(
            resolved.code,
            f"{resolved.message}. This goal has: "
            f"{', '.join(resolved.candidates) or 'nothing registered'}. "
            f"A resource that is not there yet is a person's act to add.")
    rid = resolved
    row = await conn.fetchrow(
        "SELECT id, kind, name, status FROM goal_resources "
        "WHERE id = $1 AND goal_id = $2", rid, goal_id)
    if row is None:
        raise BadResources(
            "E_NO_SUCH_RESOURCE",
            f"{rid} is not a resource of this goal.")
    if (row["status"] or "active") != "active":
        raise BadResources(
            "E_RESOURCE_NOT_ACTIVE",
            f"{row['name']} is {row['status']}, so granting it would "
            f"produce a story nobody can start.")

    kind = row["kind"] or ""
    if relation != "needs" and kind.startswith(READ_ONLY_KINDS_HINT):
        # The same refusal `grant_resource` makes, made earlier. A run
        # ACTS AS a cloud identity; widening what it may do is an IAM
        # change a person makes outside this system.
        raise BadResources(
            "E_CREDENTIAL_IS_NOT_WRITABLE",
            f"{row['name']} is cloud reach, and a run acts as it rather "
            f"than editing it. Grant it `needs`.")

    if relation != "needs" and not why:
        # `grant_resource` refuses a write grant without a reason and so
        # does this: on a story writing two places, `why` is the only
        # thing telling the executor which half goes where.
        raise BadResources(
            "E_NO_REASON",
            f"a write grant on {row['name']} needs `why` -- one line "
            f"naming what belongs there.")

    # ── AN ERRAND'S REPOS ARE WRITABLE, WHOEVER ASKED FOR WHAT ──────
    #
    # THE RULE ITSELF, AND WHY, IS IN `kernel/workspace/errand_write.py`.
    # It used to be written out here, under a comment claiming this was
    # "the one place the rule can be made" because `validate` has one
    # caller. That is true of the PROPOSAL door and only of it: a
    # hand-started errand is created by the GO and granted through
    # `grant_resource` -> `mounts.grant`, which never comes past here.
    # Errand `f87d306f` (dev, 2026-10-07) took that door and both its
    # branches mounted read-only.
    relation, why, _ = errand_write.upgrade(kind, relation, why)

    return {"resource_id": rid, "relation": relation, "why": why}
