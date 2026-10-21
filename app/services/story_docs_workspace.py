"""Scaffolding for a story workspace that holds no checkout.

The operator's story workspace IS the working tree, and every document in
it is written for someone about to `cd repo` and start editing. A kernel
story has no `repo/` — the executor has the checkout, in its own sandbox —
so those documents describe a directory that does not exist. Handing an
assistant "the git repo is at ./repo" when there is no ./repo is how a
model ends up writing plans into an invented tree; it happened once
already on a single-repo story and cost a session.

So the docs-only workspace gets its own, much shorter scaffolding rather
than the operator's with the repo lines patched out. Two files:

  IDENTITY.md   which story this is, and where the code is NOT
  docs/         granted documents, downloaded by the resource provisioner

No CLAUDE.md. That file is the Claude Code operator system prompt — the
four-phase enum, the DAG schema, the commit rules — and none of it applies
to an assistant that dispatches a run and reads the result.
"""
from __future__ import annotations


async def build_identity_md(pool, goal_id: str, story_id: str) -> str:
    """IDENTITY.md for a workspace with documents in it and no code.

    Says plainly where the code is instead of leaving the assistant to
    infer it from an absent directory — the `workspace` view over the
    executor's sandbox, which is always the current tree, unlike a clone
    that stops being true at the executor's first commit.
    """
    story = await pool.fetchrow(
        "SELECT title, description, acceptance_criteria FROM stories WHERE id = $1",
        story_id,
    )
    if not story:
        return f"# Identity\n\nStory: {story_id}\n"

    goal = await pool.fetchrow(
        "SELECT name FROM goals WHERE id = $1", goal_id)
    goal_title = (goal["name"] if goal else "") or "(unknown)"

    lines = [
        "# Identity",
        "",
        f"**Story:** {story['title']}",
        f"**Story ID:** `{story_id}`",
        f"**Goal:** {goal_title}",
        f"**Goal ID:** `{goal_id}`",
        "",
        "## Mission",
        story["description"] or "_(no description provided)_",
    ]
    if story["acceptance_criteria"]:
        lines += ["", "## Acceptance criteria", story["acceptance_criteria"]]

    lines += [
        "",
        "## Where the code is",
        "",
        "**Not here.** This directory holds the story's documents and plans.",
        "There is no checkout in it and there is not meant to be one.",
        "",
        "The work happens in an executor sandbox with its own clone of the",
        "granted repositories. Read that tree with the `workspace` tool — it",
        "is the tree the executor is actually changing, so it is current by",
        "construction. A copy here would stop being true at the executor's",
        "first commit.",
        "",
        "Do not clone anything into this directory, and do not run git here.",
        "",
        "## What is here",
        "",
        "- `docs/` — the documents this story was granted, downloaded for you.",
        "- `RESOURCES.md` — what the story may read and write, and where.",
        "",
        "Plans belong to the run, not to this directory: the executor",
        "registers one and it goes to the person for approval.",
    ]
    return "\n".join(lines) + "\n"


def scaffolding_files(identity_md: str, resources_md: str) -> dict[str, str]:
    """The complete file set for a docs-only story workspace.

    Deliberately short. Every file written here is one the assistant may
    read and believe, so anything that is not true of this workspace is
    left out rather than softened.
    """
    return {
        "RESOURCES.md": resources_md,
        "IDENTITY.md": identity_md,
    }
