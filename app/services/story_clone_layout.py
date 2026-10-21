"""Where each granted branch lands on disk, when a story writes more than one.

THE SHAPE THAT WAS MISSING. A story may write several branches — two lanes
of one repository, or lanes of different repositories, or both. The engine
has handled that for a while: it cuts one node per write target, names each
branch after its lane, and lands them as one merge group. The control panel's
provisioning did not. It resolved a story's repos by REPOSITORY, so a second
lane of a repo already seen hit `continue` and disappeared, and it derived
every directory from `repos/<owner>__<name>`, which has no lane in it — so
even two surviving lanes would have been cloned over each other.

Different repositories already worked, because their repository keys differ.
Two lanes of ONE repository is the case this module exists for.

THE RULE IS NOT NEW, AND THAT IS THE POINT. `kernel/workspace/mounts.py`
already states it, in SQL, for the execution pod:

    reads of a lane always get their own branch-suffixed directory;
    writes get one only when the story writes MORE THAN ONE lane of that
    repository — a single-lane story keeps the canonical path every
    existing check names.

Restating it here is a duplication, and a deliberate one: an index predicate
and a resolver in another process cannot share a function. What keeps them
honest is `test_the_clone_layout_agrees_with_the_pod.py`, which runs both
over the same rows and fails if they ever answer differently. Change the rule
in one place and that test is what tells you about the other.

WHY `base_branch` IS THE LANE for an engine-cut branch. The row the engine
writes (`ensure_branch_resource`) has `branch_name = feat/<goal>-<story>-<lane>`
— the working branch — and `base_branch = <lane>` — where it merges. The lane
is the identity that must not collide; the working branch is derived from it
and already unique. Reading `branch_name` here would give every lane its own
directory named after a branch nobody refers to.
"""

from __future__ import annotations

from app.services.git_graph.repo_identity import (canonical_repo_id,
                                                  is_write_relation)

REPO_LAYOUT = "repos/{owner}__{name}"


def lane_of(row) -> str:
    """Which lane of the repository this grant is about. '' = the repo itself.

    Three row shapes reach here and they name the lane in three places:

      kind='repo'                 no lane of its own
      kind='branch' role='trunk'  a LANE DECLARATION -- branch_name IS the
                                  lane (the merge target). Never cloned
                                  directly, or the story would commit onto
                                  the lane instead of its own branch.
      kind='branch' role='feature' the engine's working branch -- base_branch
                                  is the lane it merges into.
    """
    if (row.get("kind") or "").strip() != "branch":
        return ""
    if (row.get("role") or "").strip() == "trunk":
        return (row.get("branch_name") or "").strip()
    return (row.get("base_branch") or "").strip()


def clone_key(row) -> tuple[str, str]:
    """What makes two grants the SAME clone: one repository, one lane.

    The old key was the repository alone, which folded a story's bare repo
    grant together with the engine branch cut for it -- correct, and the
    reason the key existed. It also folded two lanes of that repository
    together, which is the bug. Both survive here: the lane rows separate,
    and the bare repo row is absorbed by `group_clones` below.
    """
    return (canonical_repo_id(row.get("repo_owner"), row.get("repo_name")),
            lane_of(row))


def group_clones(rows: list[dict]) -> list[dict]:
    """One entry per clone, from grants already in preference order.

    Order is the caller's: the first row seen for a (repo, lane) wins, so
    the SQL's `ORDER BY` stays the thing that decides which grant describes
    a clone. This only decides HOW MANY clones there are.

    THE BARE REPO GRANT IS ABSORBED. A story normally holds both the repo
    grant and the engine's branch for it, and those are one checkout, not
    two. So a repository with any lane drops its lane-less row -- but a
    repository with NO lane at all keeps it, because then that row is the
    only thing naming the clone.
    """
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = clone_key(row)
        if key not in seen:
            seen[key] = dict(row)
    with_lane = {repo for repo, lane in seen if lane}
    return [entry for key, entry in seen.items()
            if key[1] or key[0] not in with_lane]


def assign_subdirs(entries: list[dict]) -> list[dict]:
    """Give every clone its directory, suffixing only where it must.

    SET-AWARE, which is why it takes the whole list. Whether a write lane
    needs a suffix is not a property of that grant -- it is a property of
    how many lanes of its repository the story writes. One lane keeps
    `repos/<owner>__<name>`, the path every existing story, plan root and
    recorded node already uses; two lanes each get `…@<lane>`.

    Returns the same list, each entry carrying `lane` and `subdir`.
    """
    write_lanes: dict[str, set[str]] = {}
    for entry in entries:
        lane = lane_of(entry)
        if lane and is_write_relation(entry.get("relation")):
            repo = canonical_repo_id(entry.get("repo_owner"),
                                     entry.get("repo_name"))
            write_lanes.setdefault(repo, set()).add(lane)

    for entry in entries:
        lane = lane_of(entry)
        repo = canonical_repo_id(entry.get("repo_owner"),
                                 entry.get("repo_name"))
        base = REPO_LAYOUT.format(owner=(entry.get("repo_owner") or "").strip(),
                                  name=(entry.get("repo_name") or "").strip())
        writes = is_write_relation(entry.get("relation"))
        # A read of a lane ALWAYS gets its own directory: it sits beside the
        # story's own working clone of the same repository, and without the
        # suffix the two derive one path and the read silently vanishes.
        suffix = lane and (not writes or len(write_lanes.get(repo, ())) > 1)
        entry["lane"] = lane
        entry["subdir"] = f"{base}@{lane}" if suffix else base
    return entries
