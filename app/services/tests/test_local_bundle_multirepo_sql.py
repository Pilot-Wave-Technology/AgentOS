"""The local assistant persona (`local_bundle.build_local_bundle`) must make a
multi-repo story's agent aware of every repo it holds — otherwise it authors a
single-repo-shaped plan and discovers the second repo only via the DAG parser's
rejection (the exact incident this file's tests are named for: story `31c0e209`,
the first real multi-repo local checkout, hit `tasks[0] (T1) is missing required
field 'repo'` because its persona never mentioned a second repo existed).

Against a real database — `resolve_workspace_clones` is the same reader C7/C10
already rely on, so these tests exercise the actual data path, not a mock.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.local_bundle import build_local_bundle
from app.services.tests.test_local_workspace_nodes_sql import _node, _story
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = await conn.fetchval(
            "SELECT count(*) FROM stories WHERE id LIKE $1", f"{PREFIX}%",
        )
        await conn.close()
        assert leaked == 0, f"{leaked} fixture rows survived rollback — dev DB dirtied"


@pytest.mark.asyncio
async def test_single_repo_bundle_is_unchanged():
    """The common case: no repo layout note, memory path stays the pre-existing
    literal, no `repo:` field requirement — byte-identical to before this feature."""
    async with _conn() as conn:
        sid = f"{PREFIX}-bundle-single"
        await _story(conn, sid, execution_mode="local")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repo")

        from app.services import local_bundle
        orig = local_bundle.get_pool
        local_bundle.get_pool = lambda: conn
        try:
            bundle = await local_bundle.build_local_bundle(sid)
        finally:
            local_bundle.get_pool = orig

        claude_md = bundle["claude_md"]
        assert "[[" not in claude_md, "no unsubstituted placeholder must remain"
        assert ".agentos-memory/MEMORY.md" in claude_md
        assert "spans" not in claude_md.lower()  # no multi-repo note leaked in
        assert "`repo`" not in claude_md.split("Structural rules")[0].split(
            "hard rules"
        )[-1], "single-repo must not require a repo: field"

        # agent_md (the ALWAYS-written persona, unlike claude_md which is skipped
        # when the repo already commits its own CLAUDE.md) must also be clean and
        # must NOT claim a repo: field is required for this single-repo story.
        agent_md = bundle["agent_md"]
        assert "[[" not in agent_md, "no unsubstituted placeholder must remain"
        assert "spans" not in agent_md.lower()
        assert "unaffected by this story spanning" not in agent_md, (
            "the multi-repo plan-path note (PLAN_PATH_NOTE) must not leak into a "
            "single-repo persona"
        )


@pytest.mark.asyncio
async def test_multi_repo_bundle_documents_both_repos_and_the_repo_field():
    async with _conn() as conn:
        sid = f"{PREFIX}-bundle-multi"
        await _story(conn, sid, execution_mode="local")
        await _node(conn, sid, "acme", "svc-a", "feat/x", "repos/acme__svc-a")
        await _node(conn, sid, "acme", "svc-b", "feat/x", "repos/acme__svc-b")

        from app.services import local_bundle
        orig = local_bundle.get_pool
        local_bundle.get_pool = lambda: conn
        try:
            bundle = await local_bundle.build_local_bundle(sid)
        finally:
            local_bundle.get_pool = orig

        claude_md = bundle["claude_md"]
        assert "[[" not in claude_md, "no unsubstituted placeholder must remain"

        assert "repos/acme__svc-b" in claude_md or "acme__svc-b" in claude_md, (
            "the SIBLING repo must be named"
        )
        # UNIFORM LAYOUT (2026-10-06): cwd is the CHECKOUT ROOT for every local
        # story, so memory is right there. It used to be `../../` because the
        # governance files lived inside the primary repo and cwd was that repo
        # -- which also meant a second clone got no bundle at all.
        assert ".agentos-memory/MEMORY.md" in claude_md
        assert "../../" not in claude_md, (
            "a path relative to a repo survives the move to a root-anchored "
            "layout, and will send the agent somewhere that does not exist"
        )
        assert "checkout root" in claude_md.lower(), (
            "the persona must say where cwd actually is"
        )
        assert "artifacts/" in claude_md and "docs/" in claude_md, (
            "the layout promises both directories; the agent is told about "
            "neither"
        )
        # 2026-10-01: this was the Task DAG's per-task `repo:` field. A plan has
        # no tasks; what carries the same information is the `cd <repo-dir> &&`
        # prefix on a command check, which the control plane refuses a plan for
        # omitting. The CLAIM is unchanged -- documented, not discovered via
        # rejection -- and so is the reason it matters.
        assert "must say which one it runs in" in claude_md, (
            "the per-repo check requirement must be documented, not discovered "
            "via rejection"
        )
        assert "repos/acme__svc-a" in claude_md and "repos/acme__svc-b" in claude_md

        # The critical piece this fix adds (research/NREPO_LOCAL_EXECUTION_PLAN.md +
        # 2026-08-20 incident): agent_md is written UNCONDITIONALLY (unlike claude_md,
        # which is skipped when the repo already commits its own CLAUDE.md) — so it
        # must ALSO carry the repo: field requirement on its own, not just point at
        # CLAUDE.md for it.
        agent_md = bundle["agent_md"]
        assert "[[" not in agent_md, "no unsubstituted placeholder must remain"
        assert "must say which one it runs in" in agent_md, (
            "agent_md must document the per-repo check requirement itself — it's "
            "the only file guaranteed to reach the agent when the repo ships its "
            "own CLAUDE.md"
        )
        assert "repos/acme__svc-a" in agent_md and "repos/acme__svc-b" in agent_md

        # 2026-08-20 fix: confirmed live (stories 473eb1ee vs c22f2501, same primary
        # repo, two sessions) that the plan/scope path was NOT deterministic without
        # this note — one session wrote it at the checkout root, the other inside
        # the primary repo. agent_md is the only file guaranteed to reach the agent
        # (see above), so it must state this itself, not just point elsewhere.
        # 2026-10-01: "and scope" dropped. scope.md is an operator-lane artifact
        # the kernel has no concept of; what it held is the plan's own rationale
        # and steps. The claim here is unchanged: agent_md states the path itself.
        # 2026-10-06: the anchor is no longer "relative to cwd". cwd is the
        # checkout ROOT now, which is not a git repository, so a plan written
        # there is tracked by nothing. The plan goes INSIDE each repository it
        # describes -- which is also what makes it ride that repo's commits
        # into the PR diff.
        assert "Write your plan INSIDE each repository" in agent_md
        assert "agentos_plans" in agent_md or "agentos_plans" in claude_md, (
            "neither file names the plan folder, so the agent has to guess"
        )
        plan_note = agent_md.split("Write your plan")[1][:600]
        assert "repos/acme__svc-a/" in plan_note
        assert "repos/acme__svc-b/" in plan_note, (
            "only one write repo is named, so the other gets no plan"
        )

        # 2026-08-20 fix: the plan must also be COPIED into every other write
        # repo, not just the primary — a check scoped to a sibling write repo
        # has no other way to find it. ("both files" was the plan AND scope.md;
        # scope.md is gone, so it is one file now — the copy requirement is
        # what this is guarding, not the count.)
        # The wording moved from "copy the plan file" (primary -> others) to
        # "every WRITE repository gets the same file", which is the same
        # requirement without the primary-first framing -- there is no primary
        # to copy FROM once cwd is the checkout root.
        assert "byte-for-byte" in plan_note, (
            "nothing says the same plan must land in every write repository, "
            "so a check scoped to a sibling repo has nowhere to find it"
        )


@pytest.mark.asyncio
async def test_multi_repo_dag_field_row_lists_only_write_repos():
    """A read-only context repo is cloned for reference but never committed to — it
    must not appear as a valid `repo:` target, or the agent could target a task at
    a repo it structurally cannot commit to."""
    async with _conn() as conn:
        sid = f"{PREFIX}-bundle-readonly"
        await _story(conn, sid, execution_mode="local")
        await _node(conn, sid, "acme", "svc-write", "feat/x", "repos/acme__svc-write")
        # A second WRITE repo so this is genuinely multi-repo (>1 clone) while also
        # having a read-only entry to check the exclusion.
        await _node(conn, sid, "acme", "svc-write-2", "feat/x", "repos/acme__svc-write-2")
        await conn.execute(
            """INSERT INTO story_workspace_nodes
                   (story_id, git_repo_id, branch_name, subdir, is_write,
                    repo_owner, repo_name)
               VALUES ($1, 'gr_readonly_ctx', 'main', 'repos/acme__ctx', FALSE,
                       'acme', 'ctx')""",
            sid,
        )

        from app.services import local_bundle
        orig = local_bundle.get_pool
        local_bundle.get_pool = lambda: conn
        try:
            bundle = await local_bundle.build_local_bundle(sid)
        finally:
            local_bundle.get_pool = orig

        claude_md = bundle["claude_md"]
        # 2026-10-01: the valid targets used to be a `| \`repo\` |` table row for
        # the DAG field. They are now the list of directories a command check may
        # `cd` into. Same question, same wrong answer to guard against.
        note = [
            line for line in claude_md.splitlines()
            if "must say which one it runs in" in line
        ]
        assert note, "the per-repo check requirement must be present for a multi-repo story"
        targets = note[0]
        assert "repos/acme__ctx" not in targets, (
            "a read-only context repo must not be offered as a target — the agent "
            "would scope work at a repo it structurally cannot commit to"
        )
        assert "repos/acme__svc-write" in targets
        assert "repos/acme__svc-write-2" in targets
