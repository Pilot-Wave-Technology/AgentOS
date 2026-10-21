"""Two branches, one story — in one repository or in several.

WHAT WAS BROKEN, and it was not the engine. The git engine has cut one node
per write target for a long time: two lanes of a repo, or two repos, become
two branches that land as one merge group. The control panel's provisioning
never learned the plural. Three separate places assumed one branch per repo:

    provision_story_workspace   `LIMIT 1` on the story's git_nodes, so only
                                one lane's branch was ever back-filled into
                                goal_resources
    resolve_repos_for_story     collapsed grants by REPOSITORY, so a second
                                lane of a repo already seen hit `continue`
    _clone_descriptors          `repos/<owner>__<name>`, no lane in it, so
                                two lanes would have been cloned over each
                                other even if both had survived

Different repositories always worked, because their repository keys differ.
Two lanes of ONE repository is what these tests are about.

THE READ CASE ALREADY WORKED and is easy to mistake for the write case.
Execution 289 on 2026-09-23 held two branch grants of one repo and finished
cleanly -- both were `relation='needs'`. A read of a lane is ALWAYS given its
own branch-suffixed directory, so two of them never collided. It is not
evidence that two-lane writes worked, and it was read as such once.
"""

from __future__ import annotations

import io
import re

from app.services.story_clone_layout import (assign_subdirs, clone_key,
                                             group_clones, lane_of)

OWNER, REPO = "Pilot-Wave-Technology", "multi_agent_dummy_repo"
BASE = f"repos/{OWNER}__{REPO}"


def _lane(branch: str, relation: str, role: str = "feature",
          base_branch: str | None = None) -> dict:
    """An engine-cut branch grant: branch_name is the WORKING branch,
    base_branch the lane it merges into."""
    return {"id": branch, "kind": "branch", "role": role,
            "branch_name": branch, "base_branch": base_branch,
            "repo_owner": OWNER, "repo_name": REPO, "relation": relation}


def _repo(relation: str = "produces") -> dict:
    return {"id": "bare", "kind": "repo", "role": "feature",
            "branch_name": "", "base_branch": None,
            "repo_owner": OWNER, "repo_name": REPO, "relation": relation}


# ── which lane a grant is about ──────────────────────────────────────

def test_an_engine_branch_is_identified_by_what_it_merges_into():
    """`base_branch`, not `branch_name`. The working branch is already
    unique per story, so naming directories after it would give every
    single-lane story a suffix -- and relocate every workspace that
    exists."""
    row = _lane("feat/G-s1-backend", "produces", base_branch="backend")
    assert lane_of(row) == "backend"


def test_a_trunk_grant_names_the_lane_directly():
    """A `role='trunk'` row is a LANE DECLARATION -- what GO grants when
    it says "this story writes the backend lane". Its branch_name IS the
    lane, and it is never cloned: the story works on the engine's branch
    for that lane, not on the lane itself."""
    assert lane_of(_lane("backend", "modifies", role="trunk")) == "backend"


def test_a_plain_repo_grant_has_no_lane():
    assert lane_of(_repo()) == ""


# ── how many clones ──────────────────────────────────────────────────

def test_two_lanes_of_one_repo_are_two_clones():
    """THE BUG. Both rows share a repository, so the old repo-keyed
    collapse kept the first and dropped the second with a bare
    `continue` -- no log, no error, and a lane the assistant was granted
    and never given."""
    rows = [_lane("feat/G-s1-backend", "produces", base_branch="backend"),
            _lane("feat/G-s1-frontend", "produces", base_branch="frontend")]
    assert len(group_clones(rows)) == 2


def test_the_bare_repo_grant_is_still_folded_in():
    """What the repo-keyed collapse was RIGHT about, and must survive. A
    story normally holds both the repo grant and the engine branch cut
    for it; those are one checkout, and two would mean cloning the same
    repository twice."""
    rows = [_lane("feat/G-s1-backend", "produces", base_branch="backend"),
            _repo()]
    clones = group_clones(rows)
    assert len(clones) == 1
    assert lane_of(clones[0]) == "backend"


def test_a_repo_with_no_lanes_keeps_its_own_row():
    """Absorption must not swallow the only row naming the clone."""
    clones = group_clones([_repo()])
    assert len(clones) == 1 and clones[0]["kind"] == "repo"


def test_two_repos_still_work_and_always_did():
    rows = [_lane("feat/G-s1-a", "produces", base_branch="main"),
            dict(_lane("feat/G-s1-b", "produces", base_branch="main"),
                 repo_name="other_repo")]
    assert len({clone_key(r) for r in rows}) == 2


# ── where they land ──────────────────────────────────────────────────

def test_one_lane_keeps_the_canonical_path():
    """NO SUFFIX for the ordinary story, and this is the load-bearing
    half. Every existing workspace, recorded `story_workspace_nodes`
    row, plan root and CLAUDE.md path says `repos/<owner>__<name>`.
    Suffixing unconditionally would relocate all of them at once."""
    rows = assign_subdirs(
        [_lane("feat/G-s1-backend", "produces", base_branch="backend")])
    assert rows[0]["subdir"] == BASE


def test_two_write_lanes_each_get_their_own_directory():
    rows = assign_subdirs(
        [_lane("feat/G-s1-backend", "produces", base_branch="backend"),
         _lane("feat/G-s1-frontend", "produces", base_branch="frontend")])
    assert sorted(r["subdir"] for r in rows) == [
        f"{BASE}@backend", f"{BASE}@frontend"]


def test_a_read_lane_always_gets_its_own_directory():
    """Even beside a single write lane of the same repo. Without the
    suffix the read derives the working clone's path, the one-mount-
    per-path dedupe keeps the writable one, and the read access
    silently vanishes."""
    rows = assign_subdirs(
        [_lane("feat/G-s1-backend", "produces", base_branch="backend"),
         _lane("frontend", "needs", role="trunk")])
    by_rel = {r["relation"]: r["subdir"] for r in rows}
    assert by_rel["produces"] == BASE
    assert by_rel["needs"] == f"{BASE}@frontend"


def test_two_lanes_of_different_repos_keep_the_canonical_path():
    """The count is PER REPOSITORY. Two repos written one lane each is
    two ordinary single-lane checkouts, and suffixing them would be a
    relocation for no reason."""
    rows = assign_subdirs(
        [_lane("feat/G-s1-a", "produces", base_branch="main"),
         dict(_lane("feat/G-s1-b", "produces", base_branch="main"),
              repo_name="other_repo")])
    assert {r["subdir"] for r in rows} == {
        BASE, f"repos/{OWNER}__other_repo"}


# ── and it must say the same thing as the execution pod ──────────────

def test_the_rule_matches_the_pods_sql():
    """The pod derives these paths in SQL (`kernel/workspace/mounts.py`)
    and cannot call this module. Two statements of one rule drift, and
    the drift is an assistant sent to a directory that is not there --
    so the shape of the SQL's condition is asserted here, and this test
    is what fails when either side moves.

    Three clauses have to be present on that side: reads of a lane
    always suffix, writes suffix on a count per (story, repo) greater
    than one, and the suffix is the branch name.
    """
    sql = io.open("kernel/workspace/mounts.py", encoding="utf-8").read()
    body = sql[:sql.index("AS at")]
    marker = "WHEN gr.kind = 'branch' AND gr.role = 'trunk'"
    condition = body[body.rindex(marker):]
    stripped = "\n".join(line for line in condition.splitlines()
                         if not line.strip().startswith("--"))

    assert "sr.relation = 'needs'" in stripped, (
        "the pod no longer always suffixes a READ lane")
    assert re.search(r"PARTITION BY\s+sr\.story_id,\s+gr\.repo_owner,\s+"
                     r"gr\.repo_name\) > 1", stripped), (
        "the pod no longer counts write lanes per (story, repo)")
    assert "'@' || gr.branch_name" in stripped, (
        "the pod no longer suffixes with the branch name")


# ── what the assistant is told ───────────────────────────────────────

def test_the_workspace_table_names_the_lane_not_just_the_branch():
    """Two rows of the table say the same repository. What tells them
    apart in the brief is the LANE -- `backend`, `frontend`, the words
    the person used -- while the branch is the engine's derived
    `feat/<goal>-<story>-<lane>`, which appears nowhere else the model
    has read. Without the lane column the table asks it to guess."""
    from app.services.operator_provisioning import _clone_layout_lines

    clones = [
        {"subdir": f"{BASE}@backend", "repo_owner": OWNER, "repo_name": REPO,
         "branch_name": "feat/G-s1-backend", "is_write": True,
         "lane": "backend"},
        {"subdir": f"{BASE}@frontend", "repo_owner": OWNER, "repo_name": REPO,
         "branch_name": "feat/G-s1-frontend", "is_write": True,
         "lane": "frontend"},
    ]
    text = "\n".join(_clone_layout_lines(clones))
    assert "Merges into" in text
    assert "`backend`" in text and "`frontend`" in text
    assert f"{BASE}@backend/" in text and f"{BASE}@frontend/" in text
    assert "SAME repository" in text, (
        "nothing warns that two rows are one repo, which is the thing "
        "most likely to read as a misconfiguration")


def test_a_plain_multi_repo_table_is_unchanged():
    """No lanes, no lane column. A two-REPO story reads exactly as it
    did; the extra column would be a blank one on every row."""
    from app.services.operator_provisioning import _clone_layout_lines

    clones = [
        {"subdir": BASE, "repo_owner": OWNER, "repo_name": REPO,
         "branch_name": "feat/G-s1", "is_write": True, "lane": ""},
        {"subdir": f"repos/{OWNER}__other", "repo_owner": OWNER,
         "repo_name": "other", "branch_name": "main", "is_write": False,
         "lane": ""},
    ]
    text = "\n".join(_clone_layout_lines(clones))
    assert "Merges into" not in text
    assert "| Directory | Repository | Branch | Access |" in text


def test_one_checkout_still_gets_no_table():
    from app.services.operator_provisioning import _clone_layout_lines

    assert _clone_layout_lines([{"subdir": BASE, "repo_owner": OWNER,
                                 "repo_name": REPO, "branch_name": "b",
                                 "is_write": True, "lane": ""}]) == []
