"""A story can be cancelled from the board, and only before it is cut.

WHAT WAS WRONG, AND IT WAS NOT THE LOGIC. `internal_cancel_story` has
done the right thing for a long time: it refuses a started story with a
reason, BRIDGES the dependents (a->b->c with b cancelled becomes a->c,
so the dependent inherits b's dependencies instead of being orphaned
into its own workstream), re-derives the git topology so the PLANNED
node is GC'd, writes the ledger, relays to the channels and notifies the
assignee.

It was behind internal-bearer auth with NO CALLER and no button. So a
person could not cancel a story at all, and the guard explaining why a
started one cannot be cancelled was never shown to anybody.

I nearly rebuilt all of it. The only real gaps were the route, the
button, and that nothing stopped the run.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# -- reachable by a person ----------------------------------------------

def test_there_is_a_route_a_person_can_reach():
    src = _read("app/routers/stories.py")
    assert '@router.post("/goals/{pid}/stories/{sid}/cancel")' in src


def test_it_is_gated_on_goal_membership():
    src = _read("app/routers/stories.py")
    fn = src[src.index("async def cancel_story_ui("):]
    fn = fn[:fn.index("@router.post")]
    assert "get_goal_role(user[\"uid\"], pid)" in fn
    assert "status_code=403" in fn


def test_both_doors_share_one_body():
    """Two copies of a cancellation would diverge, and the half that
    diverged would be the one nobody tested."""
    src = _read("app/routers/stories.py")
    assert "async def _cancel_story(" in src
    assert src.count("await _cancel_story(") == 2


# -- the guard ----------------------------------------------------------

def test_a_started_story_is_cancellable_and_its_lane_comes_down():
    """THE INVARIANT WAS LIFTED, DELIBERATELY.

    This used to assert a 409: a started story could not be cancelled,
    because its branch is cut and cancelling "would strand commits and
    risk merging cancelled work". The hazard was real; the remedy was
    not, because a story somebody simply stopped working on then sat
    `in_progress` for ever holding its thread, its workspace, its
    merge-queue slot and its place in every dependent's chain.

    What answers the hazard is the teardown, not the refusal -- so
    what is pinned now is that the teardown runs.
    """
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "status_code=409" not in body, (
        "a started story is refused again; cancelling is the only way "
        "to stop one")
    assert 'old_state in ("in_progress", "blocked") and story_kind != "merge"' in body
    assert "tear_down_nodes(conn, sid)" in body


def test_the_button_and_the_server_still_agree():
    """A button that lets you press what the server refuses teaches
    people the product is broken — and so does one that hides an
    action the server would allow. They agreed on a refusal before;
    they must agree on permitting it now."""
    page = _read("app/templates/stories/board.html")
    src = _read("app/routers/stories.py")
    assert "branch is cut and work exists" not in src, (
        "the server still refuses a started story")
    assert "branch is cut and work exists" not in page, (
        "the board still explains a refusal that no longer happens")
    assert "state not in ('done', 'reviewed', 'cancelled', 'rejected')" in page


def test_the_button_is_offered_until_the_story_is_finished():
    """The condition had grown three arms, one per case somebody
    remembered, around a rule that is gone. It now says what it means:
    anything not already finished can be stopped."""
    page = _read("app/templates/stories/board.html")
    assert "state not in ('done', 'reviewed', 'cancelled', 'rejected')" in page
    for gone in ("{% if state in ('proposed', 'accepted')",
                 "or story.work_kind == 'errand'"):
        assert gone not in page, gone


def test_cancelling_asks_first():
    """It bridges dependents and removes a planned branch. Not a thing
    to do on a mis-click."""
    page = _read("app/templates/stories/board.html")
    card = page[page.index("stories/{{ story.id }}/cancel"):]
    card = card[:card.index("{% endif %}")]
    assert "data-confirm" in card


# -- the one thing that was missing from the logic -----------------------

def test_cancelling_also_stops_the_run():
    """A `proposed` story has no run; an `accepted` one can have a run
    in PENDING, ADMITTED or DISPATCHING holding an admission slot until
    the `abandoned.py` sweep notices. The sweep is a backstop, not a
    reason to leave it holding."""
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "from app.services.story_cancellation import" in body
    assert "stop_the_run" in body
    assert "await stop_the_run(conn, sid, actor_uid)" in body


def test_the_run_is_stopped_through_the_mailbox():
    """Writing CANCELLED into the table skips the machine edge, the
    lease release, the admission slot and the journal, and leaves the
    machine disagreeing with its own log."""
    src = _read("app/services/story_cancellation.py")
    assert "submit_proposal" in src
    assert "UPDATE executions" not in src


def test_a_run_that_cannot_be_stopped_does_not_undo_the_cancel():
    src = _read("app/services/story_cancellation.py")
    fn = src[src.index("async def stop_the_run("):]
    assert "except Exception" in fn and "return 0" in fn


# -- what was already right and must stay --------------------------------

def test_dependents_are_bridged_and_not_merely_detached():
    """a->b->c with b cancelled becomes a->c. Plain removal would orphan
    c into its own workstream, split the branch and leave a stale merge
    dependency -- which is what I planned to build before reading this."""
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "array_remove(d.depends_on, $1) || $2::text[]" in body


def test_a_cancelled_merge_story_becomes_rejected():
    """`cancelled` would read as "producer removed, dependency obsolete"
    and wrongly unblock dependents. A human merge-cancel is a decision
    that the work must NOT land."""
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "WHEN story_kind = 'merge' THEN 'rejected'" in body


def test_the_topology_is_re_derived_so_the_planned_node_goes():
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "_gg_reconcile(pid)" in body


def test_the_cancel_route_is_registered_before_the_catch_all():
    """FASTAPI MATCHES IN DEFINITION ORDER, and `stories.py` has a
    catch-all `POST /goals/{pid}/stories/{sid}/{action}` for accept,
    start and done.

    With the cancel route defined below it, `/cancel` matched
    `action="cancel"`, went to the transition handler and came back
    **"Unknown action 'cancel'."** The route existed, was correct, was
    tested, and was unreachable -- which from the outside is
    indistinguishable from a broken button. Reported the first time
    anybody pressed it.
    """
    src = _read("app/routers/stories.py")
    specific = src.index('@router.post("/goals/{pid}/stories/{sid}/cancel")')
    catchall = src.index(
        '@router.post("/goals/{pid}/stories/{sid}/{action}"')
    assert specific < catchall, (
        "the catch-all will swallow /cancel again")


def test_the_transition_handler_still_does_not_know_cancel():
    """Cancel is NOT a state transition -- it bridges dependents,
    re-derives the git topology, stops the run, relays and notifies. If
    somebody adds it to the action map, the catch-all becomes a second
    cancellation path that does none of that."""
    src = _read("app/services/story_transitions.py")
    block = src[:src.index("Unknown action")]
    assert '"cancel":' not in block


def test_an_empty_form_body_does_not_500_the_cancel():
    """THE THIRD "SHIPPED AND BROKEN ON FIRST PRESS" OF THE DAY.

    The board's Cancel form had no input fields -- just a submit
    button -- and `new FormData(form)` on a field-less form produces a
    body that is nothing but the terminating boundary:

        b'------WebKitFormBoundaryIGzFB9nMElgAbUO1--\r\n'

    Starlette's multipart parser rejects that with
    `MultipartParseError: Did not find CR at end of boundary`, and the
    request 500s before reaching the guard, the bridging or anything
    else. Every other `request.form()` in this file works because those
    forms carry inputs.

    Fixed at both ends: the form now sends a reason, and reading it
    cannot fail the cancellation -- the reason is optional and already
    defaulted, so an unreadable body must not cost the person their
    click."""
    src = _read("app/routers/stories.py")
    fn = src[src.index("async def cancel_story_ui("):]
    fn = fn[:fn.index("await _cancel_story(")]
    assert "try:" in fn and "except Exception" in fn, (
        "an unparseable body still takes the whole request down")
    assert 'reason = "Cancelled from the board"' in fn, (
        "the default must be set BEFORE the parse, or the except has "
        "nothing to fall back to")


def test_the_form_sends_a_field():
    """Belt to the braces above: a form with a field parses normally,
    and the ledger gets a real reason rather than a default."""
    page = _read("app/templates/stories/board.html")
    form = page[page.index("stories/{{ story.id }}/cancel"):]
    form = form[:form.index("</form>")]
    assert 'name="reason"' in form


def test_an_errand_stays_cancellable_after_it_starts():
    """AN ERRAND HAS NO BRANCH -- "no workstream, no node, no merge
    story, nothing in the queue" -- so "its branch is cut and work
    exists" cannot apply to one. Refusing it there left an errand with
    no clean stop at all: it is `accepted` for a few seconds and
    `in_progress` from then on, so the button was disabled before
    anybody could reach it, for a hazard that cannot occur.

    SUBSUMED, NOT LOST. The errand exemption existed because an errand
    has no branch and so could not be caught by a rule about branches.
    Now that every started story is cancellable, the exemption has
    nothing left to except it from -- so what is pinned is the OUTCOME
    (an errand in flight can still be stopped), not the special case
    that used to deliver it.
    """
    page = _read("app/templates/stories/board.html")
    assert "state not in ('done', 'reviewed', 'cancelled', 'rejected')" in page
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "status_code=409" not in body


# -- the node, when an errand is cancelled after the cut ----------------

def test_a_post_cut_errand_cancel_retires_its_nodes():
    """THE GAP THE ERRAND EXEMPTION OPENED.

    `_cancel_story` ends by re-deriving the topology, and that was
    enough because of an invariant it states in its own comment:
    "Cancel is pre-cut, so reconcile only prunes PLANNED nodes -- it
    never touches a live branch."

    Exempting errands broke it. An errand has no branch worth stranding
    -- it never merges -- which is why the exemption is right. But a
    cancel can now arrive AFTER the cut, and reconcile walks past a
    BRANCH_CREATED node without touching it: a live node for a cancelled
    errand, a ref nobody will merge, no cleanup queued, no row saying
    why.

    Six errands in this database have nodes. All six are ABANDONED and
    four have had their branches deleted -- so the path exists and
    works; it simply was not being reached from here.

    THE SCOPE WIDENED AND THE REASON SURVIVED. The gap below was
    opened by the errand exemption and is now the general case: any
    cancel can arrive after the cut, so the lane is always retired.
    """
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    assert "tear_down_nodes" in body
    assert ('if old_state in ("in_progress", "blocked") '
            'and story_kind != "merge":') in body


def test_it_fires_for_every_started_story_now():
    """It was scoped to errands because they were the only thing that
    could be cancelled after its branch was cut. Any story can be now,
    so a cancel arriving post-cut must retire the lane whatever kind of
    story it is — otherwise the branch is left live, mergeable, and
    unexplained."""
    src = _read("app/routers/stories.py")
    body = src[src.index("async def _cancel_story("):]
    call = body[body.index("tear_down_nodes(conn, sid)") - 260:]
    call = call[:call.index("tear_down_nodes(conn, sid)")]
    assert 'old_state in ("in_progress", "blocked")' in call
    assert "is_errand" not in call, (
        "the teardown is still errand-only; a cancelled code story "
        "leaves its branch live")


def test_the_branch_goes_to_the_cleanup_path_not_a_second_deleter():
    """`branch_cleanup_tries` drives a sweep that deletes the ref,
    retries a GitHub failure and records what it could not do. A second
    deleter would have to learn all of that again."""
    src = _read("app/services/story_cancellation.py")
    fn = src[src.index("async def tear_down_nodes("):]
    assert "branch_cleanup_tries" in fn
    # CODE ONLY: the docstring explains that the sweep retries a GitHub
    # failure, and a bare substring check reads its own explanation as
    # the violation.
    code = " ".join(l for l in fn.splitlines()
                    if not l.lstrip().startswith("#"))
    code = code[code.index("out: dict"):]
    assert "delete_branch" not in code
    assert "github" not in code.lower()


def test_a_shared_node_keeps_its_branch():
    """One node per story is the rule now, and this is what makes that
    an assertion rather than an assumption. Being wrong costs somebody
    else's branch."""
    src = _read("app/services/story_cancellation.py")
    fn = src[src.index("async def tear_down_nodes("):]
    assert "story_id <> $2" in fn
    assert "left_shared" in fn


def test_a_merged_node_is_never_touched():
    src = _read("app/services/story_cancellation.py")
    fn = src[src.index("async def tear_down_nodes("):]
    assert 'n["state"] == "MERGED"' in fn
    assert "state <> 'MERGED'" in fn


def test_a_failure_here_does_not_undo_the_cancellation():
    """The story is already cancelled and the run already stopped. A
    node that cannot be retired is a dead ref -- worth a log line, not
    worth rolling back a cancellation for."""
    src = _read("app/services/story_cancellation.py")
    fn = src[src.index("async def tear_down_nodes("):]
    assert "except Exception" in fn and "return out" in fn


def test_a_cancel_from_the_board_lands_back_on_the_board():
    """The board navigates to wherever its fetch landed. The JSON this
    route returned left a person on the POST URL itself; a 303 to the
    board brings them back to it, refreshed. A refusal is not redirected,
    so the board can still toast why."""
    src = _read("app/routers/stories.py")
    fn = src[src.index("async def cancel_story_ui("):]
    fn = fn[:fn.index("@router.post")]
    assert 'RedirectResponse(url=f"/goals/{pid}/stories", status_code=303)' in fn
    assert "if result.status_code == 200:" in fn
    assert "return result" in fn
    board = _read("app/templates/stories/board.html")
    form = board[board.index('stories/{{ story.id }}/cancel"'):]
    form = form[:form.index("</form>")]
    assert 'data-success="Story cancelled."' in form
