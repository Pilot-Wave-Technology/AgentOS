"""What a LOCAL integration plan names: each lane, its mount, its checks.

THE CLOUD'S OWN HELPERS TAKE AN `execution_id` AND A LOCAL STORY HAS
NONE. `trunk_integration_targets.attach_mounts` reads the mounts off the
execution's sync lanes, and `carried_and_suite_checks` reads the story's
contract out of `kernel.executions`. Neither exists here: a local story
is worked in a clone on somebody's laptop, and the control plane's whole
picture of it is `stories` plus `plan_versions`.

SO THE TWO ANSWERS COME FROM WHERE THEY DO EXIST:

  * THE MOUNT IS READ FROM `story_workspace_nodes.subdir`, the row
    written when the workspace was laid out. See `lane_mounts` for why
    it is read and not re-derived.

  * THE CARRIED CHECKS ARE THE LAST APPROVED PLAN'S, read from the
    `plan_review` card. Approving a plan with checks REPLACES the story's
    contract, so an integration plan that carried none would quietly
    narrow what the story is judged against to the suite alone.
"""

from __future__ import annotations


def _lane_key(owner: str, name: str, trunk: str) -> tuple[str, str, str]:
    """A clone's identity: repository AND the trunk it was cut from.

    NOT THE REPOSITORY ALONE, which is the whole bug this module had.
    """
    return ((owner or "").lower(), (name or "").lower(),
            (trunk or "").lower())


async def lane_mounts(pool, story_id: str) -> dict:
    """Every clone this story has on disk, keyed by (owner, name, trunk).

    READ, NOT DERIVED, and that correction is the point of this function.
    The mount used to be computed from the repository name alone --
    `repos/<owner>__<name>` -- which is right until a story cuts ONE
    repository against TWO trunks. Those are two clones sharing a name,
    and the derivation collapses them into one directory. On story
    06295e15 the catch-up plan reported its conflicts as

        repos/...__agent_os_test_suites: README.md
        repos/...__agent_os_test_suites: README.md

    the same mount twice -- so one clone was named for both lanes and
    told to merge two different trunk shas into itself, while the other
    clone was never named at all and kept its conflict.

    `story_workspace_nodes.subdir` is the authority and already has the
    answer: `_clone_descriptors` suffixes the second lane of a repo with
    `@<lane>` precisely so the two can be told apart, and the extension
    clones into that recorded string verbatim. It is a PHYSICAL path
    somebody created; re-deriving it is how the two ends come to
    disagree about where a clone is -- the failure the materializer
    refuses outright with "two clones resolved to the same directory".

    AND IT IS NOT LOWERCASED, which the derivation also got wrong.
    `repo_clone_subdir` keeps GitHub's casing, so the directory on disk
    is `repos/Pilot-Wave-Technology__...`. A lowercased mount finds it
    on Windows and macOS and finds nothing on Linux, where a check that
    cannot `cd` to its mount passes by never running.
    """
    rows = await pool.fetch(
        "SELECT repo_owner, repo_name, trunk_branch, subdir "
        "  FROM story_workspace_nodes WHERE story_id = $1", story_id)
    return {_lane_key(r["repo_owner"], r["repo_name"], r["trunk_branch"]):
            r["subdir"] for r in rows if r["subdir"]}


def mount_for(mounts: dict, repo: str, lane: str = "") -> str:
    """Where this lane of this repo lives, from what `lane_mounts` read.

    THREE READINGS, NARROWEST FIRST:

      * the exact (repo, lane) row -- the only one that is certainly
        right, and the only one a two-lane story matches;
      * the repo's single recorded clone when it has exactly one, which
        covers a node whose `trunk_branch` is null or has been renamed
        under it. One clone means no ambiguity to resolve;
      * the derived name, for a workspace laid out before
        `story_workspace_nodes` existed.

    It never guesses between two clones of one repo: that guess is
    exactly the bug, and a derived name that matches neither is at
    least a path somebody can see is wrong.
    """
    owner, _, name = repo.partition("/")
    hit = mounts.get(_lane_key(owner, name, lane))
    if hit:
        return hit
    same_repo = sorted({v for (o, n, _t), v in mounts.items()
                        if (o, n) == (owner.lower(), name.lower())})
    if len(same_repo) == 1:
        return same_repo[0]
    return derived_mount(repo)


def derived_mount(repo: str) -> str:
    """`acme/api` -> `repos/acme__api`: the layout before lanes existed.

    A LAST RESORT, kept only for a checkout that predates
    `story_workspace_nodes`. Anything with a recorded clone should read
    it; see `lane_mounts`.
    """
    owner, _, name = repo.partition("/")
    return f"repos/{owner}__{name}"


async def attach_mounts(pool, story_id: str, targets: list[dict]) -> list[dict]:
    """Fill `mount` on each target from the clones the story actually has."""
    mounts = await lane_mounts(pool, story_id)
    for t in targets:
        t["mount"] = mount_for(mounts, t["repo"], t.get("lane") or "")
    return targets


async def carried_checks(pool, story_id: str) -> list[dict]:
    """The command checks of the plan this story is already working to.

    FROM THE CARD, not by re-parsing the plan's markdown. The markdown is
    a RENDERING of the plan; the card carries the normalised checks that
    were actually approved, which is the same reason the approval binds
    to the card's hash rather than to a re-hash of the text.

    FROM THE WORK PLAN'S CARD, not merely the newest one. After a single
    catch-up the newest IS a catch-up, so each offer carried the previous
    offer's generated mount checks instead of the story's own -- and on
    06295e15 that made a bad mount survive its own repair: the plan
    written with a collapsed mount was replaced, and the replacement
    carried the collapsed check forward and failed identically. The
    marker and suite checks need no carrying; they are rebuilt per mount
    on every offer, from the clones the story has now.
    """
    from app.services.local_report import latest_work_plan_card  # noqa: PLC0415

    card = await latest_work_plan_card(pool, story_id)
    if isinstance(card, str):
        import json  # noqa: PLC0415
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    out, seen = [], set()
    for c in (card or {}).get("checks") or []:
        run = (c or {}).get("run")
        if not run or run in seen:
            continue
        out.append({"run": run, "label": (c.get("label") or "unlabelled")})
        seen.add(run)
    return out


async def carried_and_suite_checks(pool, story_id: str,
                                   targets: list[dict]) -> list[dict]:
    """The story's own checks, then one marker + suite check per mount.

    MARKERS FIRST, and that ordering is a scar. On BMIF1 run 3 a README
    conflict was "resolved" by an edit that never landed, the markers
    were committed, and every test passed -- a README is not code. The
    marker check is the one a green suite cannot see.
    """
    from kernel.harness.suite_check import marker_check, suite_check  # noqa: PLC0415

    checks = await carried_checks(pool, story_id)
    seen = {c["run"] for c in checks}
    for mount in sorted({t["mount"] for t in targets if t.get("mount")}):
        for check in (marker_check(mount), suite_check(mount)):
            if check["run"] not in seen:
                checks.append(check)
                seen.add(check["run"])
    return checks
