"""What one trunk-integration plan names: each lane, where it lands, what it runs.

THE SWEEP USED TO SEND TWO UNRELATED LISTS -- `lanes` and `shas`, each
sorted on its own. For one repo that reads fine. For a story writing two
repos, or two lanes of one repo, it could not say which sha belonged to
which lane, and the plan the person approved did not say where anything
would be merged. These helpers pair each behind lane with its repository,
the sha it moved to, and the MOUNT `sync_trunk` merges it into -- the
same `at` the pod mounted, read through `sync_lanes.lanes_for_mounts`,
so the plan and the merge cannot disagree about where a lane goes.

THE CHECKS ARE CARRIED, THEN EXTENDED. Approving a plan that has checks
REPLACES the story's contract (`contracts.new_version` takes the
complete set), so the integration plan carries the story's own command
checks forward and adds each merged mount's full suite. Artifact checks
are not carried: the integration leg cannot emit, and an artifact the
feature cycle already produced is not something catching up re-proves.
"""
from __future__ import annotations

SEP = "\t"


def parse_targets(raw) -> list[dict]:
    """`owner/name<TAB>lane<TAB>sha` rows from BEHIND_SQL -> dicts."""
    out = []
    for item in sorted(raw or []):
        repo, lane, sha = (str(item).split(SEP) + ["", "", ""])[:3]
        if repo and lane:
            out.append({"repo": repo, "lane": lane, "sha": sha})
    return out


def _repo_key(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"{owner}__{name}".lower()


def mount_for(target: dict, lanes_by_mount: dict) -> str | None:
    """The mount this lane merges into: same repo, same lane."""
    key = _repo_key(target["repo"])
    for at, lane in sorted(lanes_by_mount.items()):
        base = at.split("@", 1)[0].rsplit("/", 1)[-1].lower()
        if base == key and lane == target["lane"]:
            return at
    return None


async def attach_mounts(conn, execution_id: int, targets: list[dict]):
    """Fill `mount` on each target from what this execution mounted."""
    from kernel.harness.sync_lanes import lanes_for_mounts
    lanes_by_mount = await lanes_for_mounts(conn, execution_id)
    for t in targets:
        t["mount"] = mount_for(t, lanes_by_mount)
    return targets


async def carried_and_suite_checks(conn, execution_id: int,
                                   targets: list[dict]) -> list[dict]:
    """The story's command checks, then one suite check per merged mount."""
    from kernel.harness.selfcheck import checks_for
    from kernel.harness.suite_check import marker_check, suite_check

    checks, seen = [], set()
    for c in await checks_for(conn, execution_id):
        if c.get("kind", "command") != "command" or c["run"] in seen:
            continue
        carried = {"run": c["run"], "label": c["label"] or "unlabelled"}
        budget = (c.get("spec") or {}).get("timeout_s")
        if budget not in (None, ""):
            carried["timeout_s"] = budget
        checks.append(carried)
        seen.add(c["run"])
    for mount in sorted({t["mount"] for t in targets if t.get("mount")}):
        # Markers first: cheap, and the one a passing suite cannot see.
        for check in (marker_check(mount), suite_check(mount)):
            if check["run"] not in seen:
                checks.append(check)
                seen.add(check["run"])
    return checks
