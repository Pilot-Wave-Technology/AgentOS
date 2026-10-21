"""An errand's branch goes, and what it pointed at is written down.

THE DELETE IS CORRECT AND STAYS. `delete_errand_branch` removes the ref
when an errand is accepted because "an errand's branch is scaffolding,
not a record" -- left alone, every accepted errand leaves one dead ref
that looks like work somebody forgot to merge.

WHAT WAS WRONG IS THAT NOTHING WROTE THE SHA DOWN FIRST. A deleted
branch is not deleted history: the commits stay reachable by SHA, and
`read_resource` already takes an arbitrary `ref`. So the only thing
between "what did that errand actually change" and an answer was that
the one pointer to it was dropped without being read.

AND A SECOND THING, FOUND BY FOLLOWING THE FIRST.
`cleanup_landed_branch` archives the branch RESOURCE, and it is called
from exactly one place: the merge lane. An errand never merges. So every
accepted errand left an ACTIVE row in the resource catalogue pointing at
a ref that no longer existed -- which reads, to anyone opening the list,
as a branch they could still check out.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from app.services.git_graph import branch_cleanup
from app.services.tests.shipped_tree import (IS_DISTRIBUTION, baseline_schema,
                                             table_has_column)

_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src() -> str:
    return inspect.getsource(branch_cleanup.delete_errand_branch)


# ── the sha ───────────────────────────────────────────────────────────

def test_the_head_is_read_before_the_ref_is_deleted():
    """The ordering IS the feature. Earlier records a SHA the errand
    then commits past; later has no ref to read."""
    src = _src()
    assert "get_ref_sha" in src
    assert src.index("get_ref_sha") < src.index("delete_ref("), (
        "the branch is deleted before its head is recorded, so the "
        "column is written from nothing")


def test_it_is_written_to_the_node():
    src = _src()
    assert "SET final_sha = $2" in src
    assert "final_sha IS NULL" in src, (
        "a re-run of cleanup would overwrite the recorded head with "
        "whatever a later read returns -- which after the delete is "
        "nothing")


def test_a_failed_read_does_not_keep_the_branch():
    """Bookkeeping attached to an operation whose whole contract is that
    it cannot fail the caller. A cleanup that stopped because a
    convenience read failed would be the tail wagging the dog."""
    src = _src()
    block = src.split("get_ref_sha", 1)[1].split("delete_ref(")[0]
    assert "except Exception" in block


def test_the_column_exists_and_is_in_sequence():
    if IS_DISTRIBUTION:
        assert table_has_column("git_nodes", "final_sha"), (
            "the head is read and there is nowhere to write it")
        return
    mig = _ROOT / "migrations" / "git_nodes_final_sha.sql"
    assert mig.exists()
    body = mig.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS final_sha" in body
    seq = (_ROOT / "migrations" / "SEQUENCE").read_text(encoding="utf-8")
    assert "git_nodes_final_sha" in seq.split(), (
        "a migration not in SEQUENCE never runs")


def test_it_is_not_sold_as_permanent():
    """An unreferenced commit is collectable, and nothing here tags or
    backs one up. Promising more than that in a column comment is how
    somebody later relies on it."""
    if IS_DISTRIBUTION:
        # What ships of the migration's prose is the column comment.
        m = re.search(r"COMMENT ON COLUMN public\.git_nodes\.final_sha IS '(.*?)';",
                      baseline_schema())
        assert m, "git_nodes.final_sha carries no comment"
        body = m.group(1)
    else:
        body = (_ROOT / "migrations" / "git_nodes_final_sha.sql").read_text(
            encoding="utf-8")
    assert "collect" in body.lower()
    assert "artifacts are the durable record" in body.lower() or \
           "artifacts remain the deliverable" in body.lower() or \
           "artifacts are the deliverable" in body.lower()


# ── the catalogue row ─────────────────────────────────────────────────

def test_the_branch_resource_is_archived_on_an_errand_too():
    src = _src()
    assert "cleanup_landed_branch" in src


def test_only_after_the_delete_actually_succeeded():
    """Archiving the resource for a branch GitHub refused to delete
    would hide a ref that is still there."""
    src = _src()
    ok_block = src.split("ok = await gh.delete_ref", 1)[1]
    assert "cleanup_landed_branch" in ok_block.split("_record(pool, node_id, \"deleted\")")[0]


def test_it_cannot_fail_the_cleanup_either():
    src = _src()
    block = src.split("cleanup_landed_branch", 1)[1]
    assert "except Exception" in block


def test_the_merge_path_is_untouched():
    """`cleanup_landed_branch` is the merge lane's and stays the merge
    lane's -- this ADDS a caller for a case that never merges."""
    from app.services.git_graph import branches
    src = inspect.getsource(branches.cleanup_landed_branch)
    assert "engine" in src, "the human-registered-row guard is gone"
    assert "created_by = 'engine'" in src


# ── the rails that must not move ──────────────────────────────────────

@pytest.mark.parametrize("rail", [
    "refused: trunk", "refused: merged", "not an engine branch",
    "shared workstream",
])
def test_every_refusal_still_short_circuits_before_the_delete(rail):
    """An errand may hold trunk WRITABLE, so a mis-derived ref here
    would delete the branch the whole goal lands on. None of these may
    now reach the sha read or the archive either."""
    src = _src()
    assert rail in src
    assert src.index(rail) < src.index("get_ref_sha"), rail


# ── reading it back ───────────────────────────────────────────────────

def test_a_sha_is_a_legal_ref_for_a_file_read():
    """`read_resource` and `fetch_file_from_repo` both take a ref, and
    raw.githubusercontent accepts a commit SHA -- which is the whole
    reason recording one is worth anything."""
    import app.github_files as gf
    src = inspect.getsource(gf.fetch_file_from_repo)
    assert "COMMIT SHA" in src


def test_the_owner_can_be_the_rows_own():
    """It was always the deployment's `github_org`. Correct for every
    repo in it, and a 404 for one belonging to a different account --
    which reads as a missing file while the token is fine."""
    import app.github_files as gf
    src = inspect.getsource(gf.fetch_file_from_repo)
    assert 'owner: str = ""' in src
    assert '(owner or "").strip() or _get_settings().github_org' in src


def test_the_default_is_unchanged_for_every_existing_caller():
    """A new keyword argument with a default, so nothing that calls it
    today changes behaviour."""
    import app.github_files as gf
    sig = inspect.signature(gf.fetch_file_from_repo)
    assert sig.parameters["owner"].default == ""
    # SCOPED TO app/, because `_ROOT.rglob("*.py")` walked the worktree
    # including every virtualenv and node_modules under it and took
    # three minutes. Every caller is in app/.
    callers = 0
    for path in (_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        callers += len(re.findall(r"fetch_file_from_repo\(",
                                  path.read_text(encoding="utf-8",
                                                 errors="replace")))
    # Was >= 4. app/slack/ held one of those callers and was deleted on
    # 2026-10-16 with the legacy Slack stack; three remain. The number is
    # only here so a sweep that matches nothing cannot pass silently.
    assert callers >= 3, "the caller sweep found nothing and has gone blind"


# ── the GO is told ────────────────────────────────────────────────────

def test_the_prompt_says_the_branch_is_discarded():
    go = " ".join((_ROOT / "kernel/go/AGENTS.md").read_text(
        encoding="utf-8").split())
    assert "That branch is scaffolding" in go
    assert "deleted when the errand is done" in go
    assert "quote the artifact, never the branch" in go
