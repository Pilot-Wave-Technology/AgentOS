"""A recorded clone carries the trunk it was cut from, per repo.

THE INCIDENT (goal VSCT1, dev, 2026-10-06). A local story's review is told to
diff against "the trunk it was cut from", and to answer `code_not_reviewed`
rather than guess if it cannot establish one. It could not:
`story_workspace_nodes.trunk_branch` was always ''. So it fell back to the
repository's ROOT COMMIT and reviewed against that. On a two-commit test repo
the output looks right; on a real repository it presents the entire codebase
as the story's work, and every judgement in the review answers the wrong
question.

Three things were wrong at once and each alone would have been enough:
  * nothing ever WROTE the column -- the shared writer omitted it, in both
    the gateway and the local lane;
  * no migration ever CREATED it -- it existed on one database by hand, so
    `local_hydrate`'s SELECT is a missing-column error anywhere else;
  * the extension read ONE trunk (the first repo's) and applied it to every
    repo of a multi-repo story.

PER CLONE IS THE POINT. A story can hold several repos and a repo can carry
several trunk lanes, so this is a (story, repo, branch) answer -- the grain
the table already uses.
"""
from __future__ import annotations

import contextlib
import io
import pathlib

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION, table_has_column
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip

ROOT = pathlib.Path(__file__).resolve().parents[3]
OWNER = "Pilot-Wave-Technology"


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _story(conn, gid: str, sid: str) -> None:
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "base fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,story_kind) "
        "VALUES ($1,$2,'base fixture',$3,'code')", sid, gid, f"{PREFIX}-user")


async def _lane(conn, gid: str, sid: str, rid: str, repo: str, lane: str) -> None:
    await conn.execute(
        "INSERT INTO goal_resources (id,goal_id,kind,name,role,status,"
        " repo_owner,repo_name,branch_name,created_by) VALUES "
        "($1,$2,'branch',$3,'trunk','active',$4,$5,$6,$7)",
        rid, gid, f"{repo}:{lane}", OWNER, repo, lane, f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO story_resources (story_id,resource_id,relation) "
        "VALUES ($1,$2,'modifies')", sid, rid)


def test_the_column_is_created_by_a_migration():
    """It existed on dev by hand and on no other database -- `next` included,
    verified 2026-10-06. `local_hydrate` SELECTs it, so without this the whole
    local lane is a missing-column error wherever it was never hand-patched."""
    if IS_DISTRIBUTION:
        assert table_has_column("story_workspace_nodes", "trunk_branch"), (
            "local_hydrate SELECTs a column no install creates")
        return
    seq = (ROOT / "migrations" / "SEQUENCE").read_text(encoding="utf-8").split()
    name = "clone_records_the_trunk_it_was_cut_from"
    assert name in seq, "a migration not in SEQUENCE never runs"
    sql = (ROOT / "migrations" / f"{name}.sql").read_text(encoding="utf-8")
    assert "story_workspace_nodes" in sql and "trunk_branch" in sql
    assert "IF NOT EXISTS" in sql, (
        "dev already carries the hand-made column, so a bare ADD COLUMN fails "
        "there"
    )


def test_the_writer_persists_it():
    src = io.open(ROOT / "app" / "services" / "operator_provisioning.py",
                  encoding="utf-8").read()
    body = src[src.index("async def _record_story_workspace_nodes"):]
    body = body[:body.index("async def primary_write_subdir")]
    assert "trunk_branch" in body, (
        "the shared writer omits the column, so every row in BOTH lanes "
        "carries '' however carefully the caller resolved it"
    )
    assert "EXCLUDED.trunk_branch" in body, "a re-record leaves a stale trunk"


@pytest.mark.asyncio
async def test_each_repo_gets_its_own_trunk():
    """The multi-repo case, which one `trunk` value cannot express."""
    from app.services.workspace_trunks import annotate_trunks

    async with _conn() as c:
        gid, sid = f"{PREFIX}-dbase-g1", f"{PREFIX}-dbase-s1"
        await _story(c, gid, sid)
        await _lane(c, gid, sid, f"{PREFIX}-dbase-api", "api", "develop")
        await _lane(c, gid, sid, f"{PREFIX}-dbase-ui", "ui", "main")

        clones = [
            {"repo_owner": OWNER, "repo_name": "api",
             "branch_name": "feat/x", "trunk_branch": ""},
            {"repo_owner": OWNER, "repo_name": "ui",
             "branch_name": "feat/y", "trunk_branch": ""},
        ]
        await annotate_trunks(c, gid, sid, clones)

    assert clones[0]["trunk_branch"] == "develop"
    assert clones[1]["trunk_branch"] == "main", (
        "both repos got one trunk -- the extension's bug, moved into the "
        "control plane"
    )


@pytest.mark.asyncio
async def test_a_lane_worked_on_directly_records_no_base():
    """`trunk...HEAD` against your own branch is empty, so recording it would
    hand the reviewer a diff of nothing and call it the story."""
    from app.services.workspace_trunks import annotate_trunks

    async with _conn() as c:
        gid, sid = f"{PREFIX}-dbase-g2", f"{PREFIX}-dbase-s2"
        await _story(c, gid, sid)
        await _lane(c, gid, sid, f"{PREFIX}-dbase-self", "api", "develop")
        clones = [{"repo_owner": OWNER, "repo_name": "api",
                   "branch_name": "develop", "trunk_branch": ""}]
        await annotate_trunks(c, gid, sid, clones)
    assert clones[0]["trunk_branch"] == ""


@pytest.mark.asyncio
async def test_an_explicit_trunk_is_left_alone():
    """A caller that already knows -- the engine, recording a node it cut --
    is not second-guessed."""
    from app.services.workspace_trunks import annotate_trunks

    clones = [{"repo_owner": OWNER, "repo_name": "api",
               "branch_name": "feat/x", "trunk_branch": "release/9"}]
    await annotate_trunks(None, "G", "S", clones)
    assert clones[0]["trunk_branch"] == "release/9"


@pytest.mark.asyncio
async def test_resolution_failure_is_not_a_checkout_failure():
    """No base is an honest review outcome. A refused claim is not."""
    from app.services.workspace_trunks import annotate_trunks

    class _Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("down")

        async def fetchval(self, *a, **k):
            raise RuntimeError("down")

        async def fetchrow(self, *a, **k):
            raise RuntimeError("down")

    clones = [{"repo_owner": OWNER, "repo_name": "api",
               "branch_name": "feat/x", "trunk_branch": ""}]
    out = await annotate_trunks(_Boom(), "G", "S", clones)
    assert out[0]["trunk_branch"] == ""


def test_done_is_not_terminal_while_a_review_is_owed():
    """The reaper ticks every 60s; a review takes minutes.

    A coding story sits at `done` for the whole review window, so the
    terminal sweep used to reclaim the checkout out from under a review in
    progress: the lease stops being refreshed, the story stops looking held,
    and another session can claim it while the first is still reading.
    `reviewed` is terminal for those; `done` is a waypoint.
    """
    src = io.open(ROOT / "app" / "services" / "local_checkout.py",
                  encoding="utf-8").read()
    body = src[src.index("async def reap_local_checkouts"):]
    terminal = body[body.index("terminal = await pool.fetch"):]
    assert "story_reviews" in terminal, (
        "the sweep treats `done` as terminal with no regard for an owed review"
    )
    assert "'errand'" in terminal, (
        "errands are review-exempt and must still be swept at `done`"
    )


def test_the_verdict_reaches_the_client():
    """`reviewed` is a state, not a pass. A client that cannot see `revise`
    reports the opposite of what the reviewer concluded."""
    src = io.open(ROOT / "app" / "services" / "local_dispatch.py",
                  encoding="utf-8").read()
    assert '"review": await _latest_review(' in src, (
        "local_state serves the state but not the verdict"
    )


# ── two lanes of ONE repo reach the local workspace ─────────────────────

@pytest.mark.asyncio
async def test_two_lanes_of_one_repo_are_two_clones():
    """Story 7f995412 on dev, exactly.

    `_resolve_live_repos` keyed on (owner, name) and dropped one of the two
    live nodes as "the normal re-cut case". The session was handed a single
    clone and reported that the two branches "have never been combined" --
    true, and not its fault: it had only one of them.

    A re-cut produces two nodes on the SAME trunk; two lanes differ by trunk.
    That is the whole distinction, and it is now what the key says.
    """
    from app.services.local_hydrate import _resolve_live_repos

    async with _conn() as c:
        gid, sid = f"{PREFIX}-lanes-g", f"{PREFIX}-lanes-s"
        await _story(c, gid, sid)
        rid = "gr_" + "a" * 16
        await c.execute(
            "INSERT INTO git_repos (id,owner,name,clone_url,trunk_branch) "
            "VALUES ($1,$2,'suites','https://x/suites','main')", rid, OWNER)
        for lane in ("calculator_backend", "calculator_frontend"):
            nid = f"gn_{lane[:12]}"
            await c.execute(
                "INSERT INTO git_nodes (id,goal_id,git_repo_id,branch_name,"
                " trunk_branch,state) VALUES ($1,$2,$3,$4,$5,'IN_PROGRESS')",
                nid, gid, rid, f"feat/{sid}-{lane}", lane)
            await c.execute(
                "INSERT INTO git_node_stories (git_node_id,story_id) "
                "VALUES ($1,$2)", nid, sid)

        repos = await _resolve_live_repos(c, sid)

    assert len(repos) == 2, (
        f"one lane was dropped as a re-cut: {[r['branch'] for r in repos]}"
    )
    assert {r["trunk"] for r in repos} == {
        "calculator_backend", "calculator_frontend"}
    assert all(r["trunk"] in r["branch"] for r in repos), (
        "a clone is paired with the wrong lane's trunk"
    )


def test_two_lanes_get_two_directories():
    """Same repo twice in one checkout is two directories or it is nothing:
    the extension refuses a set where two clones resolve to one path."""
    from app.services.story_clone_layout import assign_subdirs

    sources = [{
        "repo_owner": OWNER, "repo_name": "suites", "relation": "modifies",
        "kind": "branch", "role": "feature",
        "branch_name": f"feat/x-{lane}", "base_branch": lane,
    } for lane in ("calculator_backend", "calculator_frontend")]
    assign_subdirs(sources)
    dirs = [s["subdir"] for s in sources]
    assert len(set(dirs)) == 2, f"both lanes resolved to one directory: {dirs}"
    assert all("@" in d for d in dirs), (
        "the lane is missing from the path, so the two cannot be told apart"
    )


def test_one_lane_keeps_the_unsuffixed_path():
    """THE COMPATIBILITY PROPERTY. Every existing single-lane story, plan root
    and recorded node uses `repos/<owner>__<name>`; a suffix here would move
    them all."""
    from app.services.story_clone_layout import assign_subdirs

    sources = [{
        "repo_owner": OWNER, "repo_name": "suites", "relation": "modifies",
        "kind": "branch", "role": "feature",
        "branch_name": "feat/x", "base_branch": "main",
    }]
    assign_subdirs(sources)
    assert "@" not in sources[0]["subdir"]
    assert sources[0]["subdir"].endswith("suites")


def test_the_local_writer_uses_the_shared_layout_rule():
    src = io.open(ROOT / "app" / "services" / "local_workspace_nodes.py",
                  encoding="utf-8").read()
    assert "assign_subdirs" in src, (
        "the local lane derives directories itself, so it and the gateway can "
        "drift into two conventions for one repo"
    )
    assert '"trunk_branch": repo.get("trunk")' in src, (
        "the trunk is re-derived per repo instead of taken from the node that "
        "recorded which lane it was cut from"
    )


# ── push verification pairs a commit with ITS OWN branch ────────────────

def test_push_verification_is_keyed_by_lane_not_repo_name():
    """A repo name is not a clone.

    `live_by_name` mapped "owner/repo" -> ref, so a story writing two trunk
    lanes of one repository kept only the LAST clone and every entry resolved
    to it. The verification then asked GitHub whether one lane's commit was on
    the OTHER lane's branch, got 'diverged', and refused a submission whose
    pushes had both landed: story 7f995412, commit c8b781d (the backend's
    HEAD) checked against feat/VSCT1-7f995412-calculator_frontend.

    The extension has sent `trunk` on every entry all along, with a comment
    describing precisely this failure. The control plane ignored it.
    """
    src = io.open(ROOT / "app" / "services" / "local_diff.py",
                  encoding="utf-8").read()
    assert "live_by_name" not in src, (
        "the repo-name-keyed map is back; two clones of one repo collapse"
    )
    assert "live_by_key" in src and "entry_trunk" in src, (
        "the verification does not disambiguate clones by lane"
    )
    body = src[src.index("live_by_key = {"):]
    body = body[:body.index("push_verified")]
    assert 'r.get("trunk")' in body, "the key does not include the lane"


def test_an_ambiguous_entry_is_not_guessed():
    """A client that sends no lane is resolvable only when the name is
    unambiguous. With two lanes there is no honest answer, and guessing is
    what produced the wrong verification."""
    src = io.open(ROOT / "app" / "services" / "local_diff.py",
                  encoding="utf-8").read()
    i = src.index("ref = live_by_key.get(")
    guard = src[i:i + 400]
    assert "len(same_name) == 1" in guard, (
        "a nameless-lane lookup falls back to picking one of several clones"
    )


def test_no_token_is_scoped_to_one_repository_twice():
    """THE WHOLE CLASS, in one test.

    Two trunk lanes of one repository are two CLONES sharing a name. Every
    place that turns the clone set into a list of repository names for a
    GitHub token therefore has to collapse it, or the token request carries
    a duplicate. Found three: the clone mint, the push mint, and the
    credential broker.
    """
    for rel in ("app/services/local_hydrate.py",
                "app/services/local_diff.py",
                "app/routers/git_credential.py"):
        src = io.open(ROOT / rel, encoding="utf-8").read()
        assert "dict.fromkeys" in src, (
            f"{rel} builds a repo-name list for a token without deduping, so "
            f"a two-lane story asks GitHub to scope it to one repo twice"
        )


def test_a_requested_repo_resolves_to_write_when_any_clone_writes():
    """`first match wins` over a name that matches several clones hands back
    whichever the sort put first -- and a read-only row first means a WRITE
    clone of that repository gets read-only credentials, failing its push
    with a permission error that names the wrong cause."""
    src = io.open(ROOT / "app" / "routers" / "git_credential.py",
                  encoding="utf-8").read()
    assert "any(row[\"is_write\"] for row in matched)" in src, (
        "the credential scope is decided by the first matching clone rather "
        "than by whether ANY clone of that repository is writable"
    )
