"""What GO must be handed the moment it creates a story.

A STORY WITH NO RESOURCES CANNOT BE WORKED, and the grant is a separate
call from the creation -- so it is a separate thing to forget. A
forgotten one is invisible until somebody tries to start the story and
the kernel refuses it with E_NO_RESOURCES, by which point a person has
been waiting on a story that was never runnable.

So `create_story` answers with the catalogue and the ids to grant, and
GO's prompt says in as many words: "there is nothing to look up."

WHY THIS IS A MODULE AND NOT A BLOCK INSIDE ONE HANDLER. It was a block
inside one handler, and there are two creation paths: the ACP action
(`go_response._action_create_story`) and the HTTP endpoint the gateway's
`create_story` tool actually POSTs to
(`stories.internal_create_story`). Only the first carried it. On the
live path GO created a story, received `{ok, story_id, title}`, and was
told by its own prompt that the catalogue had come back.

It had not -- and GO holds no tool that can list a goal's resources.
`list_resources` belongs to the assistant; GO has `resource_brief` and
`read_resource`, which both need an id, and `grant_resource`, which
names one. So the ids existed in a table GO could not read, the prompt
said they had already been handed over, and the only way out was to
guess an id or ask a person to read the Resources page aloud.

Observed on KER25: asked to plan against a linked repository, GO
reported that the goal "is currently listed with no designated branch
assigned yet" -- accurately, from its side. `cal_kernel` was linked and
GO had no way to see it.
"""
from __future__ import annotations

def _grantable_kinds() -> tuple:
    """Everything a story can hold, including cloud reach.

    `doc` is deliberately NOT here: `register_resource` normalises it to
    `document` at the one place it writes, because every other reader in
    the system speaks `document`, and a row nobody downstream accepts is
    a row that grants and then fails.

    THE CLOUD KINDS WERE MISSING AND THAT MADE Ec4d UNREACHABLE. The
    whole credential road -- authorise, mint, lease, pointer, sweep --
    was built and tested, and GO could not see a `gcp_environment` or a
    `gcp_test_sa` in the catalogue it is handed, so it could never grant
    one. A capability nothing can reach is a capability that does not
    exist.

    Read from `kernel.credentials.clouds` rather than restated, so a new
    cloud is one row there and not a second list to remember here.
    """
    from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                           ALL_IDENTITY_KINDS)

    return (("repo", "branch", "document", "file", "dataset")
            + tuple(ALL_ENVIRONMENT_KINDS) + tuple(ALL_IDENTITY_KINDS))


GRANTABLE_KINDS = _grantable_kinds()

NEXT_REQUIRED = (
    "This story has NO resources yet and cannot be started until it "
    "does. Call grant_resource for each one it touches, with relation "
    "'modifies' for what it changes and 'needs' for what it only reads, "
    "and `why` saying why. One story may write several branches; each "
    "becomes its own feature branch and they land together.")


async def grant_guidance(pool, goal_id: str) -> dict:
    """The catalogue, the trunk lanes, and what to do next.

    Returns the keys to merge into a create-story response, or `{}` when
    the goal has no catalogue at all -- in which case there is nothing
    to grant and nothing useful to say about granting.

    `pool` is anything with `.fetch`, so a connection works as well as a
    pool and this is callable from inside an open transaction.
    """
    catalogue = await pool.fetch(
        "SELECT id, name, kind, role, branch_name FROM goal_resources "
        "WHERE goal_id = $1 AND status = 'active' "
        "AND kind = ANY($2::text[]) "
        "ORDER BY kind, name", goal_id, list(GRANTABLE_KINDS))
    if not catalogue:
        return {}

    lanes = [r for r in catalogue
             if r["kind"] == "branch" and r["role"] == "trunk"]
    out = {
        "next_required": NEXT_REQUIRED,
        "grantable": [
            {"resource_id": r["id"], "name": r["name"], "kind": r["kind"],
             "branch": r["branch_name"],
             # A TRUNK LANE IS NOT AN ORDINARY RESOURCE. Granting one
             # `modifies` is what gives the story a feature branch off
             # that lane; granting the bare repo is not the same act,
             # and the difference is invisible from the name alone.
             "is_trunk_lane": bool(r["kind"] == "branch"
                                   and r["role"] == "trunk")}
            for r in catalogue],
    }
    if lanes:
        out["trunk_lanes"] = [r["branch_name"] for r in lanes]
    return out
