#!/usr/bin/env python3
"""Migrations must not destroy what the previous build still reads.

WHY THIS IS A GUARD AND NOT A CONVENTION. Rollback here is redeploy the
previous image (scripts/rollback.sh) -- Cloud Run revisions are pruned
after every deploy, because old revisions keep running their background
loops and were observed winning writes against the new ones. Redeploying
old code is safe only while the CURRENT schema still serves it. There are
no down-migrations: apply.py records what it ran and never re-runs it, so
a migration that drops a column the previous build selects turns rollback
from a one-line command into an incident, and it does so silently -- the
rollback succeeds and the errors arrive later, at request time, from code
nobody just changed.

EXPAND then CONTRACT is the shape that keeps rollback available:

    deploy N      add the column, write both, read the old one
    deploy N+1    read the new one          <- rollback to N still works
    deploy N+2    drop the old one          <- rollback to N is now gone

The rule this enforces is only that the contract step is DELIBERATE. It
cannot tell a safe drop from an unsafe one -- nothing can, without knowing
which build you would roll back to -- so it asks for the acknowledgement
and gets out of the way.

BASELINE. Nine migrations already in SEQUENCE do this, from before the
rule existed. They are listed in ACKNOWLEDGED so the guard starts green;
their presence is history, not permission.

Usage:
    python scripts/check_expand_contract.py [migrations_dir]
"""
from __future__ import annotations

import io
import pathlib
import re
import sys

# Already in SEQUENCE when this guard was written. NOT a template: adding
# a name here is a claim that no build anyone would roll back to reads the
# thing being removed.
ACKNOWLEDGED = {
    # WITHDRAWING A COLUMN THAT NO RELEASED BUILD EVER READ. `is_default_lane`
    # was added by `trunk_lane_can_be_designated` and dropped one day later,
    # before it reached any environment other than dev. The single code path
    # that read it (`trunk_lane._DEFAULT_LANE_SQL`'s ORDER BY) is removed in
    # the same commit as this migration, so the build you would roll back TO
    # is the one from before the column existed -- which does not read it
    # either. The window in which a rollback could break is the one dev build
    # in between, and it is not a build anybody would return to: the feature
    # was withdrawn for being the wrong model, not for being broken.
    "trunk_lane_has_no_default_designation",
    # RE-KEYING ADMISSION IS A CONTRACT STEP AND CANNOT BE ANYTHING ELSE.
    # `admission_buckets` moves from (goal_id, domain, kind) to
    # (goal_id, owner), and the old code writes bucket rows with no
    # owner at all -- so the previous build cannot insert against the
    # new shape whatever this file says. `SET NOT NULL` is the line the
    # guard sees; the primary key on (goal_id, owner) would do the same
    # thing silently. Rolling the code back therefore means rolling this
    # migration back with it. Stated here rather than worked around,
    # because a DEFAULT would only move the failure to the slots table's
    # foreign key and make the step look reversible when it is not.
    "admission_by_owner",
    "047_tasks_updated_at",
    "039_platform_channels_v2",
    "agent_metrics",
    "claude_phases_add_missing_phases",
    "drop_custom_tools",
    "kernel_story_resources_relation",
    "update_code_reviewer_and_deployment_structured_output",
    "update_deployment_cicd_detection_gate",
    "update_phases_permission_not_role",
}

# Each pattern is something a PREVIOUS build could be relying on.
# ADD COLUMN, CREATE TABLE and CREATE INDEX are deliberately absent: they
# are the expand half, and they are always safe to roll back over.
DESTRUCTIVE = {
    "DROP TABLE": re.compile(r"\bDROP\s+TABLE\b", re.I),
    "DROP COLUMN": re.compile(r"\bDROP\s+COLUMN\b", re.I),
    "DROP TYPE": re.compile(r"\bDROP\s+TYPE\b", re.I),
    "RENAME COLUMN": re.compile(r"\bRENAME\s+COLUMN\b", re.I),
    "RENAME TO": re.compile(r"\bRENAME\s+TO\b", re.I),
    # A column the old build does not write becomes un-insertable for it.
    "SET NOT NULL": re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),
}


def strip_sql_comments(sql: str) -> str:
    """Comments do not run, so they must not trip the guard.

    A test in this repo once failed on the comment DESCRIBING the
    anti-pattern it was checking for. A guard that fires on prose is a
    guard people learn to route around.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def sequence_names(migrations: pathlib.Path) -> list[str]:
    path = migrations / "SEQUENCE"
    out = []
    for line in io.open(path, encoding="utf-8"):
        name = line.strip()
        if name and not name.startswith("#"):
            out.append(name)
    return out


def findings_for(migrations: pathlib.Path) -> tuple[int, list[str]]:
    scanned, findings = 0, []
    for name in sequence_names(migrations):
        sql_path = migrations / f"{name}.sql"
        if not sql_path.is_file():
            continue                       # check_sequence.py owns this case
        scanned += 1
        if name in ACKNOWLEDGED:
            continue
        body = strip_sql_comments(
            io.open(sql_path, encoding="utf-8", errors="replace").read())
        hits = sorted(k for k, rx in DESTRUCTIVE.items() if rx.search(body))
        if hits:
            findings.append(f"{name}.sql  ->  {', '.join(hits)}")
    return scanned, findings


def main(argv: list[str]) -> int:
    # The allowlist-pruning check below is only meaningful against the
    # repository's own history. Pointed at any other directory none of the
    # nine exist, so it would fail every time -- which made the guard
    # impossible to test, and an untestable guard is one nobody trusts.
    explicit = len(argv) > 1
    root = pathlib.Path(argv[1]) if explicit else pathlib.Path("migrations")
    if not (root / "SEQUENCE").is_file():
        print(f"check_expand_contract: no SEQUENCE under {root}", file=sys.stderr)
        return 1

    scanned, findings = findings_for(root)
    print(f"check_expand_contract: scanned {scanned} migration(s) under {root}")

    # A SQUASHED DISTRIBUTION HAS NO HISTORY TO ACKNOWLEDGE.
    #
    # ACKNOWLEDGED records OUR migrations -- the ones that predate this
    # guard. The published repository ships `0001_baseline_schema.sql`
    # instead of that history: one pg_dump of the schema those
    # migrations produced. So in a published checkout every name on the
    # list is absent, and the pruning rule below reads eleven absences
    # as eleven stale entries and fails the build.
    #
    # It is not stale. It is inapplicable: there is nothing to prune
    # because there was never anything to acknowledge. The distinction
    # matters, and it is why this is ALL-or-nothing -- if SOME of the
    # list is missing, that is a real list nobody pruned and the rule
    # still fires. Only a tree where the whole history has been
    # replaced by the baseline is exempt.
    #
    # The guard itself is untouched: every migration present is still
    # scanned for destructive DDL, which in a published checkout means
    # every migration the DEPLOYER adds from 0003 on. That is the case
    # the rule exists for out there.
    #
    # Found by the first clean-room build: 1 migration scanned, 11
    # acknowledged names absent, build failed at step 8.
    squashed = ((root / "0001_baseline_schema.sql").is_file()
                and not any((root / f"{n}.sql").is_file()
                            for n in ACKNOWLEDGED))
    stale = [] if (explicit or squashed) else sorted(
        n for n in ACKNOWLEDGED if not (root / f"{n}.sql").is_file())
    if stale:
        print("")
        print("check_expand_contract: FAILED — these are acknowledged but no")
        print("longer exist. An allowlist nobody prunes stops meaning anything.")
        for name in stale:
            print(f"  {name}")
        return 1

    if not findings:
        print("check_expand_contract: OK — no unacknowledged destructive DDL")
        return 0

    print("")
    print("check_expand_contract: FAILED — these remove something a previous")
    print("build may still read, which makes scripts/rollback.sh unsafe.")
    print("")
    for line in findings:
        print(f"  {line}")
    print("")
    print("Either split it expand-then-contract (add the new thing now, drop")
    print("the old one a deploy later), or add the migration name to")
    print("ACKNOWLEDGED in this file with a reason it is safe.")
    print("")
    print(f"{len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
