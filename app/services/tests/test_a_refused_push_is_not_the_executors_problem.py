"""A forge refusal must not be handed to the executor as homework.

WHAT HAPPENED. dev execution 3 committed its work, the push was
refused with `remote: Repository not found`, and the executor was told
to "check the branch state (diverged? protected?), resolve, and
propose completion again". It spent generations 5, 6 and 7 running
`git remote -v`, `git config --list --show-origin`, `git ls-remote`,
`git reflog` and `git status` against a cause that was never in the
repository -- a GitHub App token that could not see a private repo.

The backstop was the leg ceiling. `kernel/workspace/podfs_client.py`
records the same shape from an earlier run: it "bounced on `publish
failed` until it ran out of legs". That bounds the bill at twelve legs
and asks nobody, which is why nothing about this was noticed until a
person happened to be watching.

NOT ON THE FIRST FAILURE, and this is the part worth keeping: the very
failure that prompted the fix RECOVERED ON ITS OWN RETRY. The commit
is on GitHub. Parking the first refusal would have interrupted a
healthy run, so the floor is two -- which is also the `escalation`
evidence floor this park is raised under, met rather than worked
around.
"""

import pathlib

import pytest

from kernel.worker import publish_block

ROOT = pathlib.Path(__file__).resolve().parents[3]


# ── the classifier, on strings git actually produced ────────────────

# VERBATIM FROM dev execution 3. A test written against a paraphrase
# proves the paraphrase.
REAL_404 = (
    "the push was attempted and failed: 'publish' failed in the workspace "
    "pod: E_WORKSPACE_OP_FAILED: WorkspaceError: git push: remote: "
    "Repository not found.\nfatal: repository "
    "'https://github.com/Pilot-Wave-Technology/agent_os_test_suites/' "
    "not found")


# BOTH OF THESE CAME OUT OF ONE READ-ONLY TOKEN, minutes apart, on dev
# execution 4 -- a deliberate test that revoked the story's write grant
# mid-run. GitHub reports a missing write permission as EITHER a 403 or
# a 404, and which one you get is not stable. A classifier that knew
# only the 403 would have let the second refusal through to the
# executor; one that knew only the 404 would have let the first.
REAL_403 = (
    "the push was attempted and failed: 'publish' failed in the workspace "
    "pod: E_WORKSPACE_OP_FAILED: WorkspaceError: git push: remote: Write "
    "access to repository not granted.\n"
    "fatal: unable to access "
    "'https://github.com/Pilot-Wave-Technology/agent_os_test_suites/': "
    "The requested URL returned error: 403")


@pytest.mark.parametrize("detail", [
    REAL_404,
    REAL_403,
    "the push was attempted and failed: remote: Permission to x/y denied",
    "the push was attempted and failed: fatal: Authentication failed",
    "the workspace pod could not be reached, so nothing was pushed: timeout",
])
def test_a_forge_refusal_belongs_to_a_person(detail):
    assert publish_block.blocker_for(detail) == "person"
    assert not publish_block.is_executors(detail)


@pytest.mark.parametrize("detail", [
    "the push was attempted and failed: ! [rejected] main -> main "
    "(non-fast-forward)",
    "the push was attempted and failed: Updates were rejected because the "
    "tip of your current branch is behind its remote counterpart",
    "the push was attempted and failed: hint: Updates were rejected; "
    "fetch first",
    "the push was attempted and failed: remote: error: GH006: Protected "
    "branch update failed",
])
def test_a_branch_the_run_can_rebase_stays_with_the_executor(detail):
    """The half that must NOT change. A divergence is the executor's to
    fix, it has everything it needs to fix it, and a leg spent rebasing
    is a leg well spent. Parking these would turn a self-healing case
    into a person's interruption."""
    assert publish_block.blocker_for(detail) == "executor"


def test_an_unrecognised_git_error_goes_to_a_person():
    """THE DEFAULT IS THE EXPENSIVE-MISTAKE-AVOIDING ONE. Handed to the
    executor, an unknown error costs up to twelve legs of a model
    guessing; handed to a person it costs one card with a Retry button.
    """
    assert publish_block.blocker_for(
        "the push was attempted and failed: something nobody has seen") \
        == "person"


def test_the_floor_is_two_and_matches_the_escalation_schema():
    """`escalation` refuses fewer than two refusals, saying one "is the
    system working and is not a question for anybody". The park is
    raised as an escalation, so a floor of 1 here would ask a question
    the mailbox then rejects -- parking a run with nothing to answer,
    which `harness/retry.py` cannot release because it never sets
    `state`."""
    assert publish_block.PARK_AFTER == 2


# ── the wiring: both callers, and the leg they must not spend ───────

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [
    "kernel/worker/loop.py",            # the unverified path
    "kernel/verifier/finish_pass.py",   # the verified path
])
def test_both_publish_callers_make_the_split(path):
    """TWICE, BECAUSE THERE ARE TWO CALLERS OF ONE PUBLISH. Fixing only
    `loop.py` would leave every VERIFIED story bouncing on a 404, which
    is the more expensive half."""
    body = _src(path)
    assert "publish_block" in body, f"{path} does not classify the failure"
    assert "is_executors" in body, f"{path} hands every failure to the executor"
    assert "park_on_forge_refusal" in body, f"{path} never parks"


@pytest.mark.parametrize("path", [
    "kernel/worker/loop.py",
    "kernel/verifier/finish_pass.py",
])
def test_the_park_does_not_also_spend_a_leg(path):
    """A parked run must not be charged for the generation it never
    ran. The `generation + 1` has to come AFTER the park returns, or
    the budget the park exists to protect is spent by the park."""
    body = _src(path)
    park = body.index("park_on_forge_refusal")
    after = body.index("generation = generation + 1", park)
    assert after > park, (
        f"{path} increments the generation before parking, so a parked "
        f"run is billed for a leg it never took")


def test_a_delivered_push_ends_the_streak():
    """Both success paths clear it. A streak left behind parks the next
    unrelated refusal one failure early."""
    for path in ("kernel/worker/loop.py", "kernel/verifier/finish_pass.py"):
        assert "clear_forge_refusals" in _src(path) \
            or "_clear_publish_streak" in _src(path), \
            f"{path} never resets the count after a successful push"


def test_retry_clears_the_streak_too():
    """Retry is the gesture that says the outside world changed. Without
    this, "I granted the permission" clears the stall and the run parks
    again on its very next refusal."""
    body = _src("kernel/harness/retry.py")
    assert "publish_error_count = 0" in body
    assert "publish_error_count" in body.split("SELECT")[1][:200], \
        "retry reads the column it reports on"


# ── the card that said "not delivered" over delivered work ──────────

def test_a_cleared_stall_is_announced_once_and_only_where_it_was_raised():
    """The other half of the same fact.

    `stall_reason` goes NULL the moment a run gets past what stopped
    it; `stall_card_note` keeps the posted text so the card posts once.
    Nothing joined them, so dev execution 3 kept a card reading "work
    is verified but not delivered" -- with a Retry button -- over a
    push that had succeeded on retry and a run that was DONE.

    THREE THINGS IT MUST NOT ANNOUNCE, each a real dev row:
    a stall whose kind never produced a card (`installing: ...` is not
    in STALL_PREFIXES), a run that is CANCELLED or FAILED, and a row
    with no thread. All three are still CLAIMED -- the note is
    released -- or the sweep re-reads them for ever.
    """
    body = (ROOT / "app/services/kernel_park_cards.py").read_text(
        encoding="utf-8")
    assert "STALL_CLEARED_SQL" in body, "a recovered stall is never noticed"
    assert "stall_reason IS NULL" in body and \
           "stall_card_note IS NOT NULL" in body, \
        "the sweep does not join the two halves of the fact"

    # CLAIM BEFORE POST, the same discipline the stall card itself uses
    # -- two conductors sweep, and posting first would put two
    # resolutions under one card.
    cleared = body.index("stall-cleared")
    claim = body.index("stall_card_note = NULL", cleared)
    post = body.index("_post_stall_cleared(conn, row)", cleared)
    assert claim < post, (
        "the resolution is posted before the row is claimed, so both "
        "conductors will post one")

    for guard in ("STALL_PREFIXES", "_NOT_RESOLVED", "thread_id"):
        assert guard in body.split("async def _post_stall_cleared")[1][:1400], \
            f"the resolution card does not check {guard}"


def test_the_resolution_does_not_delete_the_failure():
    """The stall card's stated job is that "a persistent failure is
    never a silent one". A failure that happened stays on the record;
    what it must stop doing is describing the PRESENT."""
    body = (ROOT / "app/services/kernel_park_cards.py").read_text(
        encoding="utf-8")
    after = body.split("async def _post_stall_cleared")[1][:2000]
    assert "DELETE FROM messages" not in after, \
        "the resolution deletes the original card instead of correcting it"
    assert "INSERT INTO messages" in after, \
        "the resolution is not posted as its own message"
