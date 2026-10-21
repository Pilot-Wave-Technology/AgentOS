"""`done -> reviewed` is gated on a review, so a review must always exist.

THE GATE IS THE HAZARD. Before this, a story whose review never
happened simply stayed at `done` and the board rendered it under the
`reviewed` column so nobody noticed. Now `done` means something is still
coming, and a story stuck there holds three things: its assistant thread
binding, its protected SHCA workspace, and -- since `reclaim` stands off
while the review phase runs -- its node.

So the design is only as good as its completeness. This file is one test
per way a story could reach `done` and never be reviewed. The rows are
the same ones the plan enumerates, and each names the layer that catches
it:

    L1  the final-leg nudge in the brief
    L2  in-pod synthesis on clean exhaustion
    L3  the review-phase wall clock
    L4  the control-panel backstop sweep

L4 exists because a pod that has vanished cannot write its own epitaph.
"""
from __future__ import annotations

import inspect

import pytest


def code_only(src: str) -> str:
    """Source with every comment and docstring removed.

    WRITTEN BECAUSE THE FIRST VERSION OF FOUR TESTS BELOW ASSERTED
    AGAINST PROSE. They checked that a module does not mention
    `shca_instance_id`, `agent_workspace_files`, `REVIEW_BRIEF.md` or
    `last_reviewed_state` -- and each of those names appears in a
    docstring explaining why the module deliberately does NOT use it.
    The tests failed on the explanation of the very property they were
    checking. A guard that reads documentation and calls it behaviour is
    worse than no guard: it fails when the code is right and passes the
    moment somebody deletes a comment.

    AND THE SECOND VERSION WAS WRONG TOO, which is why this uses `ast`
    for the docstrings rather than "a string after a newline". Inside
    brackets a newline is an NL, so that rule ate every SQL literal
    passed as an argument -- leaving a stripped source with no queries
    in it, against which every "does it query X" assertion passes
    vacuously. `ast` knows what a docstring actually is.
    """
    import ast
    import io
    import textwrap
    import tokenize

    src = textwrap.dedent(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src

    drop: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            drop.update(range(first.lineno, (first.end_lineno or
                                             first.lineno) + 1))

    cut: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                cut[tok.start[0]] = min(cut.get(tok.start[0], 10**9),
                                        tok.start[1])
    except tokenize.TokenError:
        pass

    out = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in drop:
            continue
        out.append(line[:cut[i]].rstrip() if i in cut else line)
    return chr(10).join(out)


def code_of(obj) -> str:
    return code_only(inspect.getsource(obj))


# ── the switch ───────────────────────────────────────────────────────

def test_it_is_off_until_somebody_turns_it_on():
    """Every path writes DONE exactly as before until the flag is set.

    The cutover has to be a switch rather than a deployment: the first
    real story is the test, and being able to put it back without a
    revert is what makes running that test reasonable.
    """
    import os

    from kernel.worker.finish import in_pod_review_enabled

    os.environ.pop("STORY_REVIEW_IN_POD", None)
    assert in_pod_review_enabled() is False


def test_one_answer_to_the_switch_everywhere(monkeypatch):
    """Three callers, one accessor.

    They must agree. If `finish` thinks the review runs in the pod and
    `council.runs` does not, the story is reviewed twice and the two
    rows upsert over each other on `(story_id, reviewed_state)` -- the
    last writer winning, at random, between a reviewer that read the
    workspace and one that read a rebuilt bundle.
    """
    import app.services.council.runs as runs
    import app.services.go_wake_events as ev
    import kernel.worker.finish as finish

    for mod in (runs, ev):
        assert "in_pod_review_enabled" in inspect.getsource(mod), (
            f"{mod.__name__} does not read the shared switch")
    monkeypatch.setenv("STORY_REVIEW_IN_POD", "true")
    assert finish.in_pod_review_enabled() is True


# ── which stories ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_errand_owes_no_review(monkeypatch):
    """Read from the RUN, not the story row.

    `executions.kind` is what resolved the profile that actually ran, so
    it "cannot disagree with what happened" -- the same reason
    `go_wake_events` reads it there when deciding to delete a branch.
    An errand's branch is gone by review time anyway.
    """
    from kernel.worker import finish

    monkeypatch.setenv("STORY_REVIEW_IN_POD", "1")

    class _Conn:
        async def fetchrow(self, sql, *a):
            if "FROM executions" in sql:
                return {"kind": "errand", "domain": "ops", "story_id": "s1"}
            return None

    assert await finish.owes_a_review(_Conn(), 1) is False


@pytest.mark.asyncio
async def test_the_engine_s_own_stories_owe_no_review(monkeypatch):
    """Merge, diff and fix stories, on the one shared predicate."""
    from kernel.worker import finish

    monkeypatch.setenv("STORY_REVIEW_IN_POD", "1")

    class _Conn:
        async def fetchrow(self, sql, *a):
            if "FROM executions" in sql:
                return {"kind": "story", "domain": "coding",
                        "story_id": "s1"}
            return {"created_by": "engine", "story_kind": "code",
                    "merge_group_id": None}

    assert await finish.owes_a_review(_Conn(), 1) is False


@pytest.mark.asyncio
async def test_a_missing_review_profile_finishes_loudly(monkeypatch):
    """FAIL CLOSED, and the direction matters.

    A deployment whose profile tree lacks the review directory would
    otherwise set `phase='review'` and then raise `UnknownProfileError`
    on the next leg -- AFTER the run had been told not to finish. The
    story would sit in a phase nothing could serve. Finishing unreviewed
    is the safe failure; being stuck is not.
    """
    from kernel.profiles.record import UnknownProfileError
    from kernel.worker import finish

    monkeypatch.setenv("STORY_REVIEW_IN_POD", "1")

    def boom(*a, **k):
        raise UnknownProfileError("no such profile")

    monkeypatch.setattr("kernel.profiles.registry.resolve", boom)

    class _Conn:
        async def fetchrow(self, sql, *a):
            if "FROM executions" in sql:
                return {"kind": "story", "domain": "coding",
                        "story_id": "s1"}
            return {"created_by": "u_1", "story_kind": "code",
                    "merge_group_id": None}

    assert await finish.owes_a_review(_Conn(), 1) is False


# ── every path that means "finished" ─────────────────────────────────

def test_all_four_finish_paths_go_through_one_decision():
    """Four sites mean "the story is finished" and each wrote DONE itself.

    A path that forgot the review branch would finish its stories
    silently unreviewed -- indistinguishable, on the board, from
    reviewed and approved. That is why this is a guard and not a
    convention.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("kernel/harness/park_resolve.py",
                # `loop.py` IS NO LONGER ONE OF THEM. It used to finish a
                # granted errand itself; it always parks on an acceptance
                # now and somebody answers, so the finish happens through
                # `accept.py` like every other. A path that finishes
                # nothing owes no review.
                "kernel/worker/accept.py",
                "kernel/verifier/finish_pass.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "finish_execution" in src, (
            f"{rel} finishes a run without asking whether it owes a review")
        # ENDING A REVIEW PHASE IS NOT FINISHING A STORY, and `loop.py`
        # does both. The review's emergency release writes DONE directly
        # and must: it runs when recording the review has already
        # failed, and `reclaim` stands off while the phase is open, so
        # nothing else would free the node. Everything BEFORE that block
        # is a story-finish path and must go through the decision.
        head = src.split("if phase == \"review\":")[0]
        assert "SET state = 'DONE'" not in head, (
            f"{rel} still finishes a story by writing DONE itself, "
            f"bypassing the review decision")


def test_accept_still_raises_when_it_loses_the_race():
    """The one caller whose reaction to not-moving is different.

    Losing that race means a person decided about a run that had already
    changed underneath them, and the acceptance must roll back rather
    than be recorded against a state the run left.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/accept.py").read_text(encoding="utf-8")
    body = src[src.index("finish_execution"):]
    assert "AcceptRefused" in body[:600], (
        "accept.py no longer refuses when the run moved under it")


# ── finishing, guaranteed ────────────────────────────────────────────

def test_exhaustion_ends_the_review_DONE_and_not_FAILED():
    """L2. For work, FAILED is right; for a review it strands the story.

    Nobody is owed an answer by a run that would not converge. A review
    is owed one, because the transition depends on it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/loop.py").read_text(encoding="utf-8")
    block = src[src.index("ceiling != -1 and legs >= ceiling"):]
    block = block[:block.index("return {")+400]
    assert 'phase == "review"' in block
    assert "synthesise_not_produced" in block
    assert block.index('phase == "review"') < block.index("SET state = 'FAILED'")


def test_the_ceiling_is_counted_from_the_phase():
    """Otherwise a review inherits the executor's leg count.

    A story that took nine legs would get three review legs by
    arithmetic accident; one that took twelve would get none and fail
    instantly, at the one point where failing is ruled out.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/loop.py").read_text(encoding="utf-8")
    assert "phase_generation" in src
    assert "profile.max_legs or max_legs()" in src, (
        "the ceiling ignores the profile, so a review cannot have three")


def test_a_synthesised_review_does_not_read_like_a_pass():
    """If `not_produced` renders like `approve`, the system silently
    marks unreviewed work as reviewed -- worse than the stuck story it
    replaces."""
    from kernel.worker.review_finish import synthesise_not_produced

    src = inspect.getsource(synthesise_not_produced)
    assert "not_produced" in src
    assert "not an assessment" in src.lower() or "It is not" in src


@pytest.mark.asyncio
async def test_the_deadline_and_the_backstop_are_separate_questions():
    """L3 ends a phase that will not end. L4 covers a pod that is gone.

    Only the second can be answered from outside the pod, which is why
    a longer in-loop timeout would not have been enough.
    """
    from kernel.conductor import review_deadline

    src = code_of(review_deadline)
    assert "phase = 'review'" in src, "L3 does not look at live phases"
    assert "NOT EXISTS (SELECT 1 FROM story_reviews" in src, (
        "L4 does not look for stories with no review at all")
    assert "last_reviewed_state" not in src, (
        "the sweep keys on a column that is set BEFORE the transition is "
        "attempted and lies about reviewedness")


def test_the_deadline_runs_before_reclaim_releases():
    """Ordering. `reclaim` stands off during the phase, so the sweep is
    the only thing that frees the node -- and running it first means the
    release happens in this pass, not twenty seconds later."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/conductor/sweep.py").read_text(encoding="utf-8")
    assert src.index("review_sweep") < src.index("releasable("), (
        "the review deadline runs after the reclaim pass, so a freed "
        "node waits a whole tick")


def test_reclaim_holds_off_while_a_review_is_reading():
    """THE ONE THAT DESTROYS DATA.

    `reclaim` releases a workspace when the STORY reaches `done` --
    deliberately, per its own comment. The review runs after the story
    reaches `done`, in that workspace. Without this guard the first
    thing to happen after acceptance is the deletion of the tree the
    reviewer is about to read.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/conductor/reclaim.py").read_text(encoding="utf-8")
    assert "COALESCE(e.phase, '') <> 'review'" in src


# ── the surface the reviewer holds ───────────────────────────────────

def test_the_reviewer_cannot_write_to_the_workspace():
    """The guarantee the whole phase rests on, asserted rather than assumed.

    Chain stage 2 is fail-closed on the effective list, so a tool absent
    from it is refused BY NAME.
    """
    from kernel.profiles.registry import resolve

    r = resolve("story", "coding", "review")
    for banned in ("write", "edit", "bash", "bash_background", "bash_output",
                   "bash_kill", "story_commit", "sync_trunk", "subagent",
                   "emit_artifact", "publish_artifact", "env_setup",
                   "env_install", "register_plan", "propose_completion"):
        assert banned not in r.tools, f"a reviewer can call {banned}"


def test_the_reviewer_cannot_park_on_a_question():
    """There is nobody to ask: the person accepted and moved on. A
    reviewer that parks holds the pod until a deadline reaps it and
    produces nothing."""
    from kernel.profiles.registry import resolve

    assert "ask_human" not in resolve("story", "coding", "review").tools


def test_the_reviewer_can_read_the_whole_workspace():
    """The tree is the evidence, and all of it is reachable -- not just
    the files that changed. Following a change outward to its callers is
    the step that makes a review worth more than a skim."""
    from kernel.profiles.registry import resolve

    tools = resolve("story", "coding", "review").tools
    for needed in ("read", "glob", "grep", "list_dir",
                   "goal_knowledge", "submit_story_review"):
        assert needed in tools, f"a reviewer cannot {needed}"


def test_three_legs_and_no_plan():
    from kernel.profiles.registry import resolve

    r = resolve("story", "coding", "review")
    assert r.max_legs == 3
    assert r.requires_plan is False
    assert r.allows_completion is False


def test_the_work_profile_is_untouched():
    """The review profile is additive. A change that narrowed the
    executor's surface would be a different and much larger change."""
    from kernel.profiles.registry import resolve

    w = resolve("story", "coding")
    assert w.phase == ""
    for needed in ("write", "edit", "bash", "propose_completion"):
        assert needed in w.tools


# ── the brief ────────────────────────────────────────────────────────

def test_the_brief_is_never_written_into_the_workspace():
    """It would become a change the review reports on.

    The reviewer runs `git status` and `git diff` over this tree. A
    brief written into it appears in both -- and the harness writing
    into a workspace the phase exists not to write to is the rule
    breaking itself.
    """
    from kernel.harness import review_brief

    src = code_of(review_brief)
    assert "open(" not in src and "write_text" not in src
    assert "REVIEW_BRIEF.md" not in src


def test_a_section_that_fails_says_so():
    """A silently empty section reads as "there was nothing" rather than
    "this could not be read" -- which is precisely how the gateway
    reviewer came to judge a description against a plan it never got."""
    from kernel.harness import review_brief

    assert "could not be read" in inspect.getsource(review_brief)


def test_the_brief_never_ends_the_leg():
    """A reviewer with a thin brief can still read the diff. A leg that
    dies assembling one produces nothing at all."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/loop.py").read_text(encoding="utf-8")
    body = src[src.index("async def _opening"):]
    body = body[:body.index("async def _take_leg")]
    assert "except Exception" in body


def test_the_brief_reads_no_workspace():
    """The rule the last reviewer broke in three places at once."""
    from kernel.harness import review_sources

    src = code_of(review_sources)
    assert "shca_instance_id" not in src
    assert "_read_workspace_file" not in src


def test_go_memory_is_not_a_bundle_section():
    """It is not retrievable: the only reader is a gateway workspace
    call, the snapshot table is empty by configuration, and the audit
    table keeps 200 characters. A section built on it would render
    empty in exactly the way this rebuild is about."""
    from kernel.harness import review_sources

    src = code_of(review_sources.goal_context)
    assert "agent_workspace_files" not in src
    assert "goals" in src and "goal_decisions" in src


# ── delivery ─────────────────────────────────────────────────────────

def test_the_review_reaches_the_go_the_way_it_always_did():
    """Two surfaces and one marker, shared by both producers."""
    from app.services import story_review_delivery as d

    src = inspect.getsource(d)
    assert "persist_review_to_orchestrator" in src
    assert "[REVIEW:story]" in src, (
        "the marker that triggers the GO's review-action skill is gone; "
        "every card would still render and the GO would act on none")


def test_the_council_delegates_rather_than_duplicating():
    import app.services.council.dispatcher as dispatcher

    src = inspect.getsource(dispatcher.CouncilDispatcher._deliver_story)
    assert "story_review_delivery" in src
    assert "persist_review_to_orchestrator" not in src, (
        "two delivery paths that agree today is how they stop agreeing")


def test_the_review_text_is_read_back_not_carried_in_the_notify():
    """NOTIFY caps at 8000 bytes and would truncate a long review.

    CHECKED AS A PROPERTY, not as a spelling. This used to match the
    literal `SELECT review_text`, and broke when the read became the
    `RETURNING` of the delivery claim -- the same row, read back the
    same way, for the same reason. What matters is that the text comes
    from the ROW and that the caller cannot pass it in: a signature
    taking the text is how it would end up in the payload again.
    """
    from app.services.story_review_delivery import deliver_recorded

    src = inspect.getsource(deliver_recorded)
    assert "review_text" in src and "story_reviews" in src
    params = list(inspect.signature(deliver_recorded).parameters)
    assert params == ["goal_id", "story_id"], (
        "deliver_recorded takes the review text from its caller — that is "
        "the NOTIFY payload again, truncation and all")


# ── what execution 245 taught ────────────────────────────────────────

def test_the_pod_reports_the_review_rather_than_writing_it():
    """MEASURED, on execution 245.

    The reviewer produced three good reviews and every submission died
    on `permission denied for table story_reviews`. The pod's DSN is
    assembled in-container from an `execution-db` Secret -- a scoped
    execution identity with nothing outside the `kernel` schema -- and
    that scoping is the design, not an oversight: the pod's sandbox runs
    model-authored code and shares its network namespace.

    So the fix is the seam that already exists for bytes the pod may not
    store, not a grant.
    """
    from kernel.harness.tools_worker import review

    src = code_of(review.submit_story_review)
    assert "report_review" in src
    assert "record_review" not in src, (
        "the pod calls the privileged writer directly; it cannot, and "
        "the failure is silent until a reviewer tries to submit")


def test_a_failed_report_does_not_spend_the_leg_budget():
    """Retrying a permission error costs one of three legs to learn
    nothing. The sweep records it from outside instead."""
    from kernel.harness.tools_worker import review

    src = code_of(review.submit_story_review)
    tail = src[src.index("report_review"):]
    assert "exit_code=0" in tail, (
        "a failed report is returned as an error, so the reviewer "
        "retries a thing that cannot succeed")


def test_exhaustion_cannot_loop():
    """THE BUG THAT MADE 245 RUN AWAY.

    The ceiling is what ENDS a runaway, so an exception inside it turns
    a bounded phase into an unbounded one: the leg crashes, the
    generation advances, the ceiling fires again, for ever. Four legs
    and climbing, every one dying inside the thing meant to stop it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/loop.py").read_text(encoding="utf-8")
    block = src[src.index("synthesise_not_produced"):]
    block = block[:block.index("return {")]
    assert "except Exception" in block, (
        "the ceiling can raise, and raising there is how it loops")
    assert "SET state = 'DONE'" in block, (
        "the phase is not ended when recording fails, so the node is "
        "never released")


def test_the_node_is_released_even_when_the_review_cannot_be_recorded():
    """`reclaim` stands off while `phase = 'review'`, so ending the
    phase is the only thing that frees the workspace, the slot and the
    node. `kernel.executions` IS the pod's to write even when the
    public tables are not."""
    from kernel.worker import review_finish

    src = code_of(review_finish.synthesise_not_produced)
    assert "phase = NULL" in src and "state = 'DONE'" in src


def test_the_review_endpoint_takes_the_execution_s_own_token():
    """Not the shared internal bearer: this is called from the WORKER,
    which runs alongside the sandbox and shares its network namespace."""
    from app.routers import story_review

    src = code_of(story_review)
    assert "require_execution_token" in src
    assert "from app.internal_auth import" not in src


# ── the prompt and the surface must agree ────────────────────────────

def test_the_prompt_names_no_tool_the_reviewer_does_not_have():
    """CAUGHT BY HAND ONCE, so now it is a test.

    `story_diff` was removed from the profile and its two numbered steps
    stayed in the prompt -- a workflow instructing the model to call a
    tool that would be refused by name. Nothing else would have noticed:
    the profile loads, the prompt renders, and the failure appears as a
    reviewer that cannot follow its own step 2.
    """
    import pathlib
    import re

    from kernel.profiles.registry import resolve

    r = resolve("story", "coding", "review")
    root = pathlib.Path(__file__).resolve().parents[3]
    text = (root / "profiles/story-coding-review/prompt.md").read_text(
        encoding="utf-8")

    # Backticked snake_case words are how this prompt names tools. The
    # allowlist is the finish tool's own FIELDS, which are named the
    # same way and are not tools.
    fields = {"not_examined", "claim_check", "plan_drift", "cross_branch",
              "why_it_matters", "code_not_reviewed", "ask_human"}
    named = {w for w in re.findall(r"`([a-z_]{3,})`", text) if "_" in w}
    unknown = sorted(named - set(r.tools) - fields)
    assert not unknown, (
        f"the prompt tells the reviewer to call {unknown}, which the "
        f"profile does not grant -- chain stage 2 refuses them by name")


def test_the_reviewer_has_no_git_tool_at_all():
    """The workspace holds the tree; `read` answers "is this correct?"
    better than a patch does. What moved -- the one thing the tree
    cannot tell you -- is computed once into the brief from
    `git_nodes.trunk_branch`, so there is nothing to guess and nothing
    to browse with."""
    from kernel.profiles.registry import resolve

    tools = resolve("story", "coding", "review").tools
    for gone in ("story_diff", "bash", "story_commit", "sync_trunk"):
        assert gone not in tools


def test_what_changed_is_computed_from_the_recorded_trunk():
    """NOT GUESSED. `story_diff` tried `origin/main`, `origin/master`,
    and after a `sync_trunk` the merge-base with any of them sits before
    the merge -- so the diff showed the whole trunk instead of one file,
    and the reviewer spent three legs hunting for its own change."""
    from kernel.harness import review_sources

    src = code_of(review_sources.changed_files)
    assert "trunk_branch" in src, "the base is not read from the database"
    assert "..HEAD" in src and "...HEAD" not in src, (
        "a three-dot diff is merge-base relative and drags in the trunk "
        "merge; the two-dot form compares the trees")


def test_a_reviewer_keeps_its_own_notes_but_not_the_author_s():
    """Independence is from the EXECUTOR, not from itself.

    Rehydration injects the PREVIOUS leg's brief. For review leg 1 that
    is the executor's -- the author's own account of what it did and
    why, which anchors a reviewer before it has read anything. For legs
    2 and 3 it is the reviewer's own, and dropping that made three legs
    re-tread the same ground at 29, 16 and 8 steps.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (root / "kernel/worker/loop.py").read_text(encoding="utf-8")
    assert 'phase != "review" or generation > since' in src, (
        "the reviewer either inherits the executor's memory or forgets "
        "its own; the rule has to distinguish the two")


def test_the_backstop_sweeps_only_what_owes_a_review():
    """L4 used one rule and the finish path another, so the sweep found
    work in stories that were never going to be reviewed.

    `merge-*` stories have no execution, so `sweep` appended the
    finding and THEN hit `if eid is None: continue`. The row was
    counted, logged and left untouched -- on dev, twenty of them,
    pinned at twenty since July, reprinted every tick by both
    conductors. Nothing was broken; the number simply could not go
    down, and a number that cannot go down is one people stop reading.

    The fix is not a second exclusion list here. It is the SAME
    predicate the finish path uses, so "who owes a review" has one
    definition.
    """
    from kernel.conductor import review_deadline

    src = code_of(review_deadline)
    assert "owes_a_review" in src, (
        "the backstop does not use the shared predicate, so it can "
        "sweep errands, merge stories and the engine's own plumbing")
    assert "g.status = 'active'" in src, (
        "the backstop walks deleted goals' stories -- the reconcile "
        "line said `0 goal(s), 20 review_missing`")
    assert "EXISTS (SELECT 1 FROM kernel.executions e3" in src, (
        "a story with no execution cannot be repaired, so reporting "
        "it every tick is noise that never clears")
