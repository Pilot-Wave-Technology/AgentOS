"""An uploaded document has to reach the POD, not just the assistant.

GOAL1 STORY 4bb5e544. The assistant's workspace held the goal's three
documents; the executor's `/workspace/docs/` was an empty directory, and
`WORKSPACE.md` told it "INPUT DOCUMENT -- read it before you plan
against it; it is here because the work depends on what it says".

THE INVARIANT THAT BROKE, and it was written down before it broke.
`test_resource_uri_is_one_column` removed `gcs_uri` from `kernel/` and
recorded what that removal depends on:

    "every writer populates `uri`. A future writer that sets only
     `gcs_uri` would make the resource invisible to the kernel -- no
     mount, no egress host -- and nothing would raise."

Two writers set only `gcs_uri`: the resources page's "Upload files" and
the `resource_upload` service. A document uploaded through GO CHAT
writes BOTH columns with the same value, which is why chat uploads
always worked and this stayed hidden.

WHERE THE FIX IS NOT. Adding `gcs_uri` to the kernel's mount query does
make the document appear -- and puts a storage provider's name back in
the substrate-agnostic half of the system, which is the thing that test
exists to prevent. The writers are wrong, not the reader.
"""
from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _document_inserts(src: str):
    """Every `INSERT INTO goal_resources` that creates a document."""
    for m in re.finditer(r"INSERT INTO goal_resources", src):
        block = src[m.start():m.start() + 700]
        if "'document'" in block:
            yield block


# ── the invariant, at every writer ───────────────────────────────────

def test_every_document_writer_populates_uri():
    """The whole bug in one assertion."""
    for rel in ("app/routers/chat.py",
                "app/routers/goal_resources.py",
                "app/services/resource_upload.py"):
        blocks = list(_document_inserts(_read(rel)))
        assert blocks, f"{rel}: no document insert found -- did it move?"
        for block in blocks:
            cols = block[block.index("("):block.index("VALUES")]
            assert " uri," in cols or "(uri," in cols, (
                f"{rel} writes a document with no `uri`; the kernel "
                f"cannot see it, and nothing will raise")


def test_reviving_a_resource_rewrites_both():
    """A resource brought back from deleted gets a fresh object. Leaving
    `uri` at its old value points the row at something that is gone --
    the same invisibility, one step later."""
    src = _read("app/services/resource_upload.py")
    i = src.index("UPDATE goal_resources")
    block = src[i:i + 700]
    assert "uri = $2" in block and "gcs_uri = $2" in block


def test_the_kernel_is_still_free_of_the_cloud_column():
    """Stated here too, because the tempting fix to this bug is exactly
    the thing `test_resource_uri_is_one_column` forbids, and somebody
    arriving from the symptom will reach for it."""
    for path in (ROOT / "kernel").rglob("*.py"):
        if "__pycache__" in str(path) or "tests" in path.parts:
            continue
        for line in io.open(path, encoding="utf-8", errors="replace"):
            if line.strip().startswith("#"):
                continue
            assert "gcs_uri" not in line, (
                f"{path.name}: the fix belongs in the writers")


# ── and the rows already written ─────────────────────────────────────

def test_the_backfill_exists_and_only_fills_gaps():
    """New writers do not repair old rows. GOAL1's three documents were
    already on disk with an empty `uri` when the writers were fixed."""
    sql = _read("migrations/uploaded_documents_carry_a_uri.sql")
    assert "UPDATE goal_resources" in sql
    assert "SET uri = gcs_uri" in sql
    # Only where uri is empty -- a row where the two disagree is a fact
    # about that row, not a gap.
    assert "NULLIF(uri, '')" in sql
    seq = _read("migrations/SEQUENCE")
    assert "uploaded_documents_carry_a_uri" in seq


# ── the rest of the chain, which was never broken ────────────────────

def test_the_object_endpoint_authorises_by_grant():
    """The mount only makes the pod TRY. It then fetches by (story,
    resource) and the control panel resolves the object -- scoped to
    what the story was granted, never to what the caller asked for."""
    src = _read("app/routers/resource_object.py")
    assert "verify_internal_bearer" in src
    assert "FROM story_resources sr" in src
    assert "WHERE sr.story_id = $1 AND sr.resource_id = $2" in src


def test_the_endpoint_can_resolve_a_row_written_before_the_fix():
    """It coalesces gcs_uri, so a document whose `uri` was never
    backfilled still serves its bytes. Belt and braces: the mount is
    what was broken, and this is why fixing the mount is sufficient."""
    src = _read("app/routers/resource_object.py")
    assert "NULLIF(gr.gcs_uri, '')" in src


def test_go_is_told_to_grant_an_errands_documents():
    """MECHANICALLY ERRANDS ARE COVERED -- the sweep calls `inherit`,
    which reads the same `story_resources` query -- but an errand's
    story is created at APPROVAL from the `resources` list GO proposed.
    A document GO did not name is never mounted, and the run does not
    fail: it proceeds without it.

    The passage listed "its own branch, repositories, and the trunk
    branch". An errand sent to review code against a standard, holding
    only the code, reviews it against the standard it guesses at."""
    go = _read("kernel/go/AGENTS.md")
    assert "AND ITS DOCUMENTS, which are the ones most often left out" in go
    assert "a document you did not name is a document the run never" in go


def test_the_errand_path_reaches_the_same_query():
    """If the sweep stops calling `inherit`, documents stop arriving for
    errands and nothing here would otherwise notice."""
    src = _read("kernel/worker/schedules.py")
    assert "from kernel.workspace.mounts import inherit" in src
    mounts = _read("kernel/workspace/mounts.py")
    i = mounts.index("async def inherit")
    assert "story_resources(conn, goal_id, story_id)" in mounts[i:i + 900]
