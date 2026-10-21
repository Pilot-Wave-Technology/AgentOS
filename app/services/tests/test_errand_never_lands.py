"""D1 · an errand is allocated like a story, and lands nowhere.

THE RULE, which reversed an assumption the build was made of:

    An errand is allocated exactly what a story is allocated. It may
    write anything it has been granted, including a trunk checkout. Its
    work never LANDS in trunk.

What it is NOT: "an errand may not write" (it may), or "an errand may
not touch trunk" (it may have trunk checked out and change files there
to get its job done). The constraint is on the DESTINATION of the work.

The old model — scratch workspace, no repository — produced three
defects in one afternoon: an errand that could never dispatch because
the `none` driver clones nothing while the completeness check demands a
checkout; a workspace panel empty for the entire life of every errand
ever run, because `none` never sets `workspace_path`; and a git engine
deriving branches for errands with nothing downstream expecting them.

TWO ENFORCEMENT POINTS, and the second exists BECAUSE trunk may be
granted writable — a commit in a trunk checkout reaches trunk by push
alone, with no merge involved and no node for the merge gate to skip.
Testing only the first would leave the direct path open.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── allocated like a story ───────────────────────────────────────────────

def _code(rel: str) -> str:
    """A file with its comments and docstrings stripped.

    THESE GUARDS USED TO READ THE RAW TEXT, and both went red on a
    COMMENT that named what they forbid -- prose explaining that
    derivation stays `work_kind`-free is not derivation filtering on
    `work_kind`. A guard that cannot tell code from a sentence about
    code teaches people to stop writing the sentence.
    """
    import io as _io
    import tokenize
    out, src = [], _io.StringIO(_read(rel))
    prev_type = None
    for tok in tokenize.generate_tokens(src.readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev_type in (
                None, tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE,
                tokenize.NL):
            continue                       # a docstring
        out.append(tok.string)
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_type = tok.type
    return " ".join(out)



def test_the_errand_profile_gets_a_real_workspace():
    """`workspace_driver = "none"` is what made the panel empty for every
    errand ever run: the `none` driver never sets `workspace_path`, and
    the explorer resolves its tree FROM `workspace_path`."""
    from kernel.profiles.loader import load_all

    errand = load_all()[("errand", "ops")]
    assert errand.workspace_driver != "none"


def test_an_errand_still_has_no_verifier():
    """D1 widened what an errand is ALLOCATED. It did not give it a
    Verifier — there is no tree to rebuild and no merge to defend, and
    the self-check plus a person's acceptance is the gate."""
    from kernel.profiles.loader import load_all

    assert load_all()[("errand", "ops")].has_verifier is False


def test_an_errand_can_still_propose_its_own_completion():
    from kernel.profiles.loader import load_all

    assert load_all()[("errand", "ops")].allows_completion is True


# ── enforcement 1 · no merge ─────────────────────────────────────────────

def test_the_merge_chokepoint_excludes_errands():
    """THE PRIMARY GUARD, in the one place the function's own docstring
    calls "the ONLY chokepoint that needs the check" — every stage after
    QUEUED is per-node with no path back in."""
    src = _read("app/services/git_graph/merge_lane.py")
    body = src[src.index("async def enqueue_ready_merges"):]
    body = body[:body.index("\nasync def ", 10)]

    assert "git_node_stories" in body, "the merge gate cannot see stories"
    assert "est.work_kind = 'errand'" in body, (
        "the merge gate does not exclude errands, so an errand's branch "
        "can be queued and merged to trunk")
    # As a NOT EXISTS, so a node with ANY errand member is held — not a
    # join that could multiply rows or silently drop the predicate.
    assert "NOT EXISTS" in body


def test_a_hand_started_errand_still_gets_its_branch():
    """D1 REVERSED THE ORIGINAL FIX. Refusing EVERY errand a branch was
    the old model and it produced the three defects above: somebody is
    sitting with a hand-started errand and may well want the diff, so it
    gets a lane exactly as a story does and only the landing is withheld.

    WHAT CHANGED, AND ONLY FOR THE AUTONOMOUS CASE. Nobody is watching an
    autonomous errand, its deliverable is the artifacts it emits, and a
    branch it will never merge is a dead ref plus a row on the board
    promising work that is not coming. Those are excluded at the READ --
    `load_inputs` -- so `derive_plan` itself stays pure and is still
    handed whatever it is handed.

    `work_kind` IS STILL NOT THE DISCRIMINATOR, and that is the part
    worth keeping: the filter joins `errand_schedules` on a LIVE GRANT,
    so it separates autonomous from hand-started rather than errand from
    story."""
    code = _code("app/services/git_graph/plan.py")
    assert "work_kind" not in code, (
        "derivation filters on work_kind — that is the OLD model, and it "
        "would take the branch off a hand-started errand too")
    assert "derive_plan" in code


# ── enforcement 2 · no push to a trunk lane ──────────────────────────────

def test_an_errand_never_pushes_a_trunk_lane():
    """THE GUARD THAT EXISTS BECAUSE TRUNK MAY BE GRANTED WRITABLE. With
    no merge node to skip, a commit in a trunk checkout would reach trunk
    by push alone."""
    body = _read("kernel/workspace/deliver.py")
    body = body[body.index("async def publish_execution"):]

    # STRONGER THAN IT WAS. This used to assert the lane mounts were
    # dropped from an errand's push and the rest still went -- the
    # reasoning being that refusing outright "would strand the errand's
    # own branch, which is the work we want". An errand's work is its
    # ARTIFACTS; the branch is deleted on acceptance anyway. So an
    # errand now returns before the lane guard with every writable
    # mount held back, which satisfies this test's subject by removing
    # the case rather than handling it.
    assert "on_lane" in body
    assert 'kind == "errand"' in body, (
        "nothing distinguishes an errand, so its scratch tree is pushed")
    early = body[:body.index("if on_lane:")]
    assert 'kind == "errand"' in early, (
        "the errand hold-back must come BEFORE the lane guard, or an "
        "errand pays for a round-trip to the pod it cannot need")
    held = early[early.index('kind == "errand"'):]
    assert "return True" in held, (
        "the errand hold-back must deliver, not refuse: pushing nothing "
        "is success for an errand, whose deliverable is its artifacts")


def test_a_story_on_a_trunk_lane_is_still_refused_outright():
    """The same fact means two different things. For a STORY a mount on a
    lane is a defect in ref derivation, and pushing it delivers
    unreviewed work straight to the merge target — observed once, when a
    whole story landed with no PR."""
    body = _read("kernel/workspace/deliver.py")
    body = body[body.index("async def publish_execution"):]
    # NO LONGER CONDITIONAL. An errand returns earlier, so the lane
    # refusal has one answer left and makes it unconditionally.
    refusal = body[body.index("if on_lane:"):]
    assert "return False" in refusal[:1600]
    assert "never the" in refusal[:1600]
    assert 'kind != "errand"' not in refusal, (
        "dead branch: an errand cannot reach the lane guard")


def test_an_errand_whose_only_writable_mount_is_trunk_pushes_nothing():
    """And says so rather than reporting success on an empty push or
    failing on a rule it obeyed."""
    body = _read("kernel/workspace/deliver.py")
    assert "nothing pushed: an errand" in body, (
        "an errand that pushed nothing must say why, not report an "
        "empty success or fail on a rule it obeyed")


# ── both agents carry the rule ───────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "kernel/go/AGENTS.md", "kernel/go/SOUL.md",
    "kernel/assistant/AGENTS.md", "kernel/assistant/SOUL.md"])
def test_no_prompt_still_says_the_workspace_is_thrown_away(rel):
    """The single most repeated sentence of the old model. A prompt that
    still says it will have an errand write its answer into a file it
    believes is about to be deleted."""
    text = re.sub(r"\s+", " ", _read(rel)).lower()
    assert "thrown away" not in text
    assert "no branch, no merge" not in text


def test_the_profile_does_not_still_teach_the_discarded_tree():
    """THE SAME SENTENCE, one layer down. D1 rewrote the `workspace_driver`
    block and left two comments twenty lines above and below it saying the
    tree is discarded — including the one that justifies why an errand has
    no Verifier. Nobody reads a profile comment at runtime, which is
    exactly why a wrong one survives: it is read by the next person
    changing this file, and it teaches them the model D1 reversed."""
    text = re.sub(r"\s+", " ", _read("profiles/errand-ops/profile.toml")).lower()
    assert "tree is discarded" not in text
    assert "has no branch" not in text


def test_the_branch_is_described_as_scaffolding_not_as_a_record():
    """R2-F4 made the earlier wording false. GO's prompt said the commits
    stay "inspectable afterwards" — and acceptance now DELETES the branch,
    so the one moment the prompt points a person at the branch is the
    moment it stops existing."""
    text = re.sub(r"\s+", " ", _read("kernel/go/AGENTS.md")).lower()
    assert "inspectable afterwards" not in text
    assert "deleted when a person accepts" in text


@pytest.mark.parametrize("rel", ["kernel/go/AGENTS.md",
                                 "kernel/assistant/AGENTS.md"])
def test_both_agents_are_told_where_the_constraint_actually_is(rel):
    """An agent told only "an errand is different" will infer a
    constraint, and the one it infers will be about ACCESS — which is
    both wrong and the thing that made an errand unable to read the code
    it was sent to review."""
    text = re.sub(r"\s+", " ", _read(rel)).lower()
    assert "lands on trunk" in text
    assert "never does" in text or "none of it lands" in text


# ── round 2 · what the live run showed was still missing ─────────────────

def test_no_merge_story_is_created_for_an_errand():
    """R2-F3. Errand b9f0ceb8 finished, its node parked correctly at
    READY — and `merge-8361abfd4f8a` sat on the board in `proposed`,
    waiting for a human to approve a landing the platform guarantees will
    not happen. GO's own prompt says an errand has "no merge story".

    A control that does nothing when pressed is worse than a missing
    one: approving it lands nothing and explains nothing."""
    src = _read("app/services/git_graph/reconcile.py")
    body = src[src.index("def _reconcile") if "def _reconcile" in src else 0:]
    assert "bool_and(work_kind = 'errand')" in body, (
        "reconcile still derives a merge story for an errand")
    # BEFORE the merge story is built, not after — a story created and
    # then deleted is still a story somebody may have seen.
    assert body.index("bool_and(work_kind = 'errand')") < \
        body.index("merge_id = _merge_story_id("), (
        "the errand check runs after the merge story is created")


def test_only_a_LIVE_GRANT_takes_the_branch_away():
    """The exclusion must separate AUTONOMOUS from hand-started, not
    errand from story. A revoked or expired grant means nobody delegated
    anything any more, so the errand is somebody's again -- and it gets
    its topology back."""
    code = _code("app/services/git_graph/plan.py")
    assert "errand_schedules" in code
    for clause in ("approved_by IS NOT NULL", "revoked_at IS NULL"):
        assert clause in code, clause


def test_the_engine_still_derives_when_it_cannot_read_the_grants():
    """Fails OPEN. An unreadable schedule table means the engine behaves
    as it did before autonomous errands existed -- a dead branch, which
    is untidy -- rather than silently deriving nothing, which is a story
    that never gets a lane."""
    import inspect
    from app.services.git_graph import plan as _plan
    src = inspect.getsource(_plan._autonomous)
    assert "except Exception" in src
    assert "return set()" in src


def test_an_errands_branch_is_deleted_when_it_is_accepted():
    """R2-F4. The branch is scaffolding: the run needs somewhere to work,
    its work never lands, and the artifacts are the deliverable. Left
    alone it accumulates one dead ref per errand, each looking like work
    somebody forgot to merge."""
    src = _read("app/services/git_graph/branch_cleanup.py")
    assert "async def delete_errand_branch" in src
    body = src[src.index("async def delete_errand_branch"):]
    # The rail that matters most: an errand may hold trunk WRITABLE under
    # D1, so a mis-derived ref here would delete the goal's own trunk.
    assert "refused: trunk" in body
    # And it must not reuse the merged-branch precondition, which is
    # exactly wrong here — an errand's node rests at READY for ever.
    assert 'row["state"] == "MERGED"' in body
    assert "refused: merged" in body, (
        "a MERGED errand node should be impossible and must be shouted "
        "about rather than silently deleted")


def test_the_cleanup_runs_when_the_run_reaches_done():
    src = _read("app/services/go_wake_events.py")
    assert "delete_errand_branch" in src
    body = src[src.index("delete_errand_branch") - 800:]
    assert "if is_errand:" in body


def test_the_completion_wake_tells_an_errand_it_is_delivered():
    """R2-F5. The wake said "nothing is delivered until the merge story
    lands" — right for a story, exactly backwards for an errand, and a
    direct contradiction of the paragraph in GO's prompt written to stop
    it waiting for a landing that cannot happen."""
    src = _read("app/services/go_wake_events.py")
    assert "is_errand" in src
    # ANCHOR ON THE WAKE, not the first `if is_errand:` — there are two
    # now, and the earlier one is the branch cleanup. A slice that takes
    # whichever comes first is a test that silently moves when the file
    # grows.
    body = src[src.index('if node and node["branch_name"]:'):]
    assert "DELIVERED" in body[:900]
    assert "no merge and no landing wake" in body[:1400]
    # And it points at the tools rather than inviting a summary from
    # memory — the fabrication this round found.
    assert "read_artifact" in body[:1600], (
        "the wake tells GO to say what the errand found without telling "
        "it how to actually read the artifacts")


def test_a_story_still_gets_the_story_wording():
    src = _read("app/services/go_wake_events.py")
    body = src[src.index('if node and node["branch_name"]:'):]
    assert "else:" in body
    assert "Nothing is delivered until" in body[:2500]
