"""The goal's own resource file names the lanes the goal works on.

WHAT IT SAID BEFORE, on a goal with two declared trunk lanes of one
repository:

    ## repo — cal_kernel
    - **clone:** …
    - **branch (mandatory):** none assigned. Stop and ask the human
      (via your assistant) or the GO to provision one — never create
      one yourself.

That file is the GO's. So the GO was reading, in its own context, an
instruction to go and ask the GO — about lanes that already existed,
which the same file was not showing it, because the goal-scoped query
excluded every `role='trunk'` row.

WHY THEY WERE EXCLUDED, and the reason was a real one on the other
path. A STORY's resource file must not render a lane as a working ref:
the assistant would read `- **branch (mandatory):** dev` and commit
straight to trunk, bypassing branch/PR/merge entirely. The guard was
then copied onto the goal path, where there is no checkout and no
writable-ref hazard — and it removed the lane as INFORMATION, which is
the one thing the GO needs: choosing and granting the lane a story lands
on is its job.

SO A LANE IS RENDERED AS A LANE. Its own entry kind, its own block, and
the word "lane" rather than "branch (mandatory)", so nothing can read as
an instruction to commit somewhere.

WHICH MADE THE STORY PATH'S EXCLUSION THE WRONG INSTRUMENT TOO. Measured
on KER20 story 7eca8fda: its ONLY grants are its two trunk lanes -- the
normal shape, because `mounts.story_resources` resolves a lane grant to
the story's own engine-cut branch and the engine therefore need not
grant a branch resource -- so the query excluded everything the story
had and the file read, in full, "No resources have been assigned to this
agent." Both paths render lanes now; the hazard lived in the WORDING.

AND A GRANTED BRANCH IS NOT A LANE. The first version of this cleared
the working ref of every repo entry sharing an address with a lane,
which took `feat/KER20-1cbc7b3e-cal_frontend` -- the branch the story
exists to commit to -- and replaced its mandatory-branch line with a
list of lanes it must not commit to. Only the `default_branch` FALLBACK
may be overruled; see `ref_is_fallback`.
"""
from __future__ import annotations

import inspect

from app.resource_provisioning import (
    _attach_lanes,
    _build_resource_entry,
    _render_resource_block,
    _summary_table,
)


def _row(branch="", role="", name="cal_kernel"):
    return {
        "type": "git_repo", "name": name,
        "uri": "https://github.com/acme/cal_kernel",
        "repo_owner": "acme", "repo_name": "cal_kernel",
        "branch": branch, "role": role,
        # Mirrors `_goal_resources_to_rows`: a ref is a FALLBACK only
        # when it came from `default_branch` rather than from a grant.
        "ref_is_fallback": not branch,
        "access_level": "read-write", "human_context": "", "go_note": "",
        "protected_branches": [], "constraints": None,
    }


def _entry(**kw):
    return _build_resource_entry(
        r=_row(**kw), access_level="read-write", source="explicit",
        github_token=None, gcs_token=None, bq_token=None)


# ── a lane is its own kind of thing ──────────────────────────────────

def test_a_declared_trunk_renders_as_a_lane_not_a_working_ref():
    """THE FIX. `role='trunk'` is not 'the branch you work on'."""
    e = _entry(branch="cal_backend", role="trunk")
    assert e["kind"] == "lane"
    assert e["lane"] == "cal_backend"
    assert e["working_ref"] == "", (
        "a lane in the working-ref slot renders as "
        "'branch (mandatory)' and the reader commits on trunk")


def test_and_its_block_says_so():
    block = "\n".join(_render_resource_block(_entry(branch="cal_backend",
                                                    role="trunk")))
    assert "trunk lane" in block
    assert "branch (mandatory)" not in block, (
        "the one wording that means 'check this out and commit here'")


def test_a_feature_branch_is_still_a_working_ref():
    """Only `role='trunk'` changed. A story's own branch is exactly the
    thing the mandatory-branch line exists for, and breaking that would
    break every story that works."""
    e = _entry(branch="feat/auth", role="feature")
    assert e["kind"] == "repo"
    assert e["working_ref"] == "feat/auth"
    assert "branch (mandatory)" in "\n".join(_render_resource_block(e))


def test_a_plain_repo_row_is_unchanged():
    e = _entry()
    assert e["kind"] == "repo" and e["lane"] == ""


# ── the repo block stops asking the GO to ask the GO ─────────────────

def test_a_repo_with_lanes_lists_them_instead_of_demanding_a_branch():
    entries = [_entry(), _entry(branch="cal_backend", role="trunk"),
               _entry(branch="cal_frontend", role="trunk")]
    _attach_lanes(entries)
    block = "\n".join(_render_resource_block(entries[0]))
    assert "cal_backend" in block and "cal_frontend" in block
    assert "Stop and ask" not in block, (
        "this file IS the GO's context; the sentence tells the GO to ask "
        "the GO for lanes that are declared two blocks further down")


def test_lanes_are_matched_to_their_own_repository():
    """Two repos in one goal, one laned and one not. Attaching by
    address rather than by position is what keeps the second one's block
    from claiming the first one's lanes."""
    other = _build_resource_entry(
        r={**_row(name="tools"), "repo_name": "tools",
           "uri": "https://github.com/acme/tools"},
        access_level="read", source="explicit",
        github_token=None, gcs_token=None, bq_token=None)
    entries = [_entry(), other, _entry(branch="cal_backend", role="trunk")]
    _attach_lanes(entries)
    assert entries[0]["lanes"] == ["cal_backend"]
    assert entries[1]["lanes"] == []
    assert "Stop and ask" in "\n".join(_render_resource_block(entries[1]))


def test_lanes_outrank_the_default_branch_fallback():
    """MEASURED ON KER15, which has two lanes. The repo block read

        - **branch (mandatory):** `main` — checkout and commit on this
          branch only.

    because `_goal_resources_to_rows` falls back to `default_branch`,
    which is GitHub's `main`, and only `GITGRAPH_ENABLED` was suppressing
    it. A feature flag is the wrong thing to be deciding whether a file
    tells its reader to commit straight to trunk — and on a goal with
    declared lanes the answer is never a repo-wide branch, flag or no
    flag."""
    repo = _entry()
    repo["working_ref"] = "main"            # the default_branch fallback
    entries = [repo, _entry(branch="cal_backend", role="trunk")]
    _attach_lanes(entries)
    block = "\n".join(_render_resource_block(entries[0]))
    assert "branch (mandatory)" not in block
    assert "cal_backend" in block
    assert entries[0]["working_ref"] == "", (
        "the summary table reads working_ref too, and showed `main` in "
        "the Working ref column of a repo with two lanes")


def test_one_lane_reads_correctly_too():
    """The wording fires for a single declared lane as well, so it must
    not say 'more than one'."""
    entries = [_entry(), _entry(branch="cal_backend", role="trunk")]
    _attach_lanes(entries)
    block = "\n".join(_render_resource_block(entries[0]))
    assert "more than one" not in block


def test_a_repo_with_no_lanes_still_says_to_ask():
    """The refusal was never wrong in itself — a goal that has declared
    nothing genuinely has no branch to work on. It was wrong only where
    lanes existed."""
    entries = [_entry()]
    _attach_lanes(entries)
    assert "Stop and ask" in "\n".join(_render_resource_block(entries[0]))


# ── and the reader can see it without reading the blocks ─────────────

def test_the_summary_table_shows_the_lane():
    rows = _summary_table([_entry(branch="cal_backend", role="trunk")])
    assert any("cal_backend" in r for r in rows), (
        "a lane has no working ref by design, so the column read '—' for "
        "every lane and the table said nothing at all")
    assert any("lane" in r for r in rows)


# ── the two paths, and why only one of them changed ──────────────────

def test_the_goal_scoped_query_no_longer_excludes_lanes():
    from app import resource_provisioning

    src = inspect.getsource(resource_provisioning.generate_resources_md)
    goal_scoped = src[src.index("No task_id or story_id"):]
    assert "role" in goal_scoped.split("ORDER BY kind, name")[0], (
        "`role` must be selected or the renderer cannot tell a lane from "
        "a branch to work on")
    assert "role = 'trunk'" not in goal_scoped


def test_the_story_scoped_query_renders_them_too():
    """It excluded them, and on a story whose only grants ARE its lanes
    that is every resource it has."""
    from app import resource_provisioning

    src = inspect.getsource(resource_provisioning.generate_resources_md)
    story_scoped = src[:src.index("No task_id or story_id")]
    assert "AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')" not in         story_scoped
    assert "gr.role," in story_scoped, (
        "without `role` selected every lane renders as a working ref, "
        "which is the commit-on-trunk hazard the exclusion guarded")


# ── a granted branch is not a fallback ───────────────────────────────

def test_every_query_that_feeds_the_renderer_selects_role():
    """Three queries reach `_goal_resources_to_rows` -- story-scoped,
    goal-scoped, and the task-level additions from
    `task_resource_deps`. A lane arriving through one that omits `role`
    renders as "branch (mandatory): cal_backend", which is the
    commit-on-trunk hazard by whichever door was left open."""
    from app import resource_provisioning

    src = inspect.getsource(resource_provisioning.generate_resources_md)
    selects = [b for b in src.split("SELECT")[1:] if "goal_resources" in b]
    missing = [b.split("FROM")[0].strip()[:70] for b in selects
               if "role" not in b.split("FROM")[0]]
    assert not missing, f"these select no `role`: {missing}"


def test_a_granted_feature_branch_survives_its_lanes():
    """THE REGRESSION. A story working a lane holds the engine-cut
    branch for it; that branch shares the repo's address with the lane
    rows, so matching on address alone erased the one line telling the
    story where to commit."""
    feature = _entry(branch="feat/KER20-1cbc7b3e-cal_frontend",
                     role="feature")
    entries = [feature, _entry(branch="cal_frontend", role="trunk")]
    _attach_lanes(entries)
    assert feature["working_ref"] == "feat/KER20-1cbc7b3e-cal_frontend"
    block = "\n".join(_render_resource_block(feature))
    assert "branch (mandatory)" in block
    assert "trunk lanes" not in block


def test_the_default_branch_fallback_is_still_overruled():
    """`ref_is_fallback` is what separates the two, and a row built
    without it must default to 'fallback' -- the safe side is refusing
    to name a branch, never naming `main`."""
    repo = _entry()
    repo["working_ref"], repo["ref_is_fallback"] = "main", True
    entries = [repo, _entry(branch="cal_backend", role="trunk")]
    _attach_lanes(entries)
    assert repo["working_ref"] == ""


def test_two_feature_branches_of_one_repo_get_distinct_headings():
    """A story on both lanes holds two engine branches of one repo. Both
    rendered as "## repo - <repo>" with contradictory mandatory lines."""
    a = _entry(branch="feat/S-cal_backend", role="feature")
    b = _entry(branch="feat/S-cal_frontend", role="feature")
    _attach_lanes([a, b])
    assert a["name"] != b["name"]
    assert "feat/S-cal_backend" in a["name"]


def test_a_single_repo_entry_keeps_its_plain_name():
    e = _entry(branch="feat/S-only", role="feature")
    _attach_lanes([e])
    assert e["name"] == "cal_kernel"


def test_a_goal_of_only_lanes_still_gets_its_git_instructions():
    """`has_repo` gates the Git Setup block and the git rules. Keyed on
    `kind == "repo"` alone, a set of lane entries would produce a
    resource file with credentials and rules for nothing."""
    from app import resource_provisioning

    src = inspect.getsource(resource_provisioning.generate_resources_md)
    assert 'e["kind"] in ("repo", "lane")' in src
