"""GO may start an autonomous errand, and nothing else.

THE AUTHORITY BEING GRANTED, said plainly: a person approved an errand
knowing it would run unattended, so when they type "approved" GO may
carry that and let it start. That is a narrow thing.

WHAT IT MUST NOT BECOME is an agent that starts ordinary stories.
Those belong to whoever they are assigned to, they are accepted from the
board by that person, and an agent quietly starting one is a much larger
authority than the one anybody granted. The difference is one column,
`work_kind`, and it is read OFF THE ROW rather than taken from the
caller -- so there is nothing here a prompt can talk its way past.

AND "APPROVED" IN PROSE IS NOT THE ACT. A sentence is evidence; the
tool is the act. An agent that infers approval from a sentence will
eventually infer it from a sentence that was not one, and the result is
work nobody sanctioned, started without them -- which is exactly the
trust this feature spends. So GO names the story and passes the
person's uid; unsure which story, it asks.
"""

from __future__ import annotations

import asyncio
import io
import pathlib

from kernel.go.accept_errand import accept_errand

ROOT = pathlib.Path(__file__).resolve().parents[3]


class _Conn:
    def __init__(self, row): self.row = row
    async def fetchrow(self, *a, **k): return self.row


def _call(row=None, story="s1", human="uid-abc"):
    conn = _Conn(row)
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        accept_errand(conn, "G", "go", story, human))


def _refusal(out):
    return getattr(out, "code", None)


# -- the guard that matters ---------------------------------------------

def test_an_ordinary_story_is_refused():
    out = _call({"id": "s1", "goal_id": "G", "state": "proposed",
                 "work_kind": "story", "title": "t"})
    assert _refusal(out) == "E_NOT_AN_ERRAND"


def test_a_story_with_no_work_kind_is_treated_as_ordinary():
    """`work_kind` is NULL on every story that predates errands. The
    default must be the SAFE reading, not the permissive one."""
    out = _call({"id": "s1", "goal_id": "G", "state": "proposed",
                 "work_kind": None, "title": "t"})
    assert _refusal(out) == "E_NOT_AN_ERRAND"


def test_an_errand_is_allowed():
    out = _call({"id": "s1", "goal_id": "G", "state": "proposed",
                 "work_kind": "errand", "title": "t"})
    assert isinstance(out, dict)
    assert out["state"] == "in_progress"


def test_the_kind_is_read_from_the_row_not_the_caller():
    src = io.open(ROOT / "kernel/go/accept_errand.py",
                  encoding="utf-8").read()
    assert 'row["work_kind"]' in src
    fn = src[src.index("async def accept_errand("):]
    sig = fn[:fn.index(")")]
    assert "work_kind" not in sig, (
        "the caller can declare what kind of work this is, which makes "
        "the guard a formality")


# -- a person has to be named -------------------------------------------

def test_it_refuses_without_a_human():
    out = _call({"id": "s1", "goal_id": "G", "state": "proposed",
                 "work_kind": "errand", "title": "t"}, human="")
    assert _refusal(out) == "E_NO_HUMAN"


# -- a bare "approved" resolves to the pending proposal ------------------
#
# THE TWO-STORY BUG. An errand has no story until somebody approves it:
# `errand_grants.approve()` creates the story AND the grant together,
# and the grant is what makes it autonomous. The first version of this
# refused a bare approval with `E_NO_STORY` -- so GO, holding a refusal
# and an approval it believed, created a story itself. ERR12 ended with
# `91996a9b` (no grant, sat on the board) beside `2e0a7c42` (grant,
# ran), for one errand.


class _ConnP:
    """A connection whose only content is some pending proposals."""

    def __init__(self, pending): self.pending = pending
    async def fetchrow(self, *a, **k): return None
    async def fetch(self, *a, **k): return self.pending


def _bare(pending):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        accept_errand(_ConnP(pending), "G", "go", "", "uid-abc"))


def test_one_pending_proposal_is_the_answer():
    out = _bare([{"id": 38, "name": "n", "task": "t"}])
    assert isinstance(out, dict), getattr(out, "message", out)
    assert out["proposal"] == 38
    assert "story" not in out, (
        "returning a story id here is the bug: the story does not exist "
        "until the proposal is approved")


def test_two_pending_proposals_is_the_question_worth_asking():
    out = _bare([{"id": 38, "name": "a", "task": "t"},
                 {"id": 39, "name": "b", "task": "t"}])
    assert _refusal(out) == "E_WHICH_ERRAND"
    assert "#38" in getattr(out, "message", "")
    assert "#39" in getattr(out, "message", "")


def test_no_pending_proposal_says_so_plainly():
    out = _bare([])
    assert _refusal(out) == "E_NOTHING_TO_APPROVE"


def test_the_app_half_approves_the_proposal_not_a_story():
    """One path for chat and card: both end at
    `errand_grants.approve()`, which writes the grant."""
    src = io.open(ROOT / "app/services/go_response.py",
                  encoding="utf-8").read()
    fn = src[src.index("async def _action_accept_errand("):]
    assert "from app.services.errand_grants import approve" in fn
    assert fn.index('if "proposal" in checked') <         fn.index('for action in ("accept", "start")')


# -- and GO cannot mint one itself ---------------------------------------

def test_create_story_refuses_work_kind_errand():
    """The structural half. Without this the refusal above just moves
    the workaround somewhere else -- which is exactly what happened."""
    src = io.open(ROOT / "app/services/go_response.py",
                  encoding="utf-8").read()
    assert 'if work_kind == "errand":' in src
    assert "E_ERRAND_NOT_CREATED_HERE" in src


def test_that_refusal_names_the_route_back():
    """A refusal that does not say what to do instead gets worked
    around, which is how the two-story bug happened in the first
    place."""
    src = io.open(ROOT / "app/services/go_response.py",
                  encoding="utf-8").read()
    block = src[src.index("E_ERRAND_NOT_CREATED_HERE") - 900:]
    block = block[:block.index("E_ERRAND_NOT_CREATED_HERE") + 40]
    assert "propose_errand" in block and "accept_errand" in block


def test_an_errand_already_running_is_refused():
    out = _call({"id": "s1", "goal_id": "G", "state": "in_progress",
                 "work_kind": "errand", "title": "t"})
    assert _refusal(out) == "E_NOT_PENDING"


# -- the app half --------------------------------------------------------

def _go_response() -> str:
    return io.open(ROOT / "app/services/go_response.py",
                   encoding="utf-8").read()


def test_the_action_reuses_the_kernel_guard():
    """One definition of "may GO start this". A second copy app-side
    would drift, and the half that drifted would be the permissive
    one."""
    src = _go_response()
    assert "from kernel.go.accept_errand import accept_errand" in src


def test_a_refusal_stops_the_transition():
    src = _go_response()
    fn = src[src.index("async def _action_accept_errand("):]
    assert fn.index("if not isinstance(checked, dict)") <         fn.index("apply_story_transition(")


def test_it_goes_straight_to_in_progress():
    """An errand has nobody waiting to click Start. Approval IS the
    start."""
    src = _go_response()
    fn = src[src.index("async def _action_accept_errand("):]
    assert 'for action in ("accept", "start")' in fn


def test_a_start_that_cannot_happen_is_reported_not_hidden():
    """No slot, or an unmet dependency, leaves it `accepted`. GO must
    tell the person that rather than report it as running."""
    src = _go_response()
    fn = src[src.index("async def _action_accept_errand("):]
    assert '"state": "accepted"' in fn
    assert "cannot start yet" in fn


def test_accept_errand_is_in_the_allowlist():
    src = _go_response()
    block = src[src.index("VALID_ACTIONS = {"):]
    block = block[:block.index("}")]
    assert '"accept_errand"' in block


def test_go_still_cannot_create_a_task():
    """The forbidden list is the shape of authority GO does not have,
    and adding one must not quietly widen it."""
    src = _go_response()
    assert '"create_task"' in src[src.index("_FORBIDDEN_ACTIONS"):]


# -- naming one, or all of them -----------------------------------------
#
# THE AMBIGUITY REFUSAL WAS A DEAD END. GO asks which errand they meant,
# they answer "the first one", and GO had no way to say so: a proposal
# has no story id to pass, so it could only ask again. A refusal that
# cannot be satisfied is worse than no refusal, because it reads as an
# agent that is stuck.


class _ConnPr:
    """Proposals by id, plus the pending list."""

    def __init__(self, rows): self.rows = rows
    async def fetchrow(self, sql, *a):
        if "errand_proposals" in sql:
            return next((r for r in self.rows if r["id"] == a[0]), None)
        return None
    async def fetch(self, *a, **k):
        return [r for r in self.rows if r["state"] == "PENDING"]


def _named(rows, pid=None, all_pending=False):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        accept_errand(_ConnPr(rows), "G", "go", "", "uid-abc",
                      proposal_id=pid, all_pending=all_pending))


P38 = {"id": 38, "state": "PENDING", "name": "a", "task": "t"}
P39 = {"id": 39, "state": "PENDING", "name": "b", "task": "t"}


def test_a_named_proposal_is_approved():
    out = _named([P38, P39], pid=38)
    assert isinstance(out, dict), getattr(out, "message", out)
    assert out["proposal"] == 38


def test_a_named_proposal_that_is_already_decided_is_refused():
    out = _named([dict(P38, state="APPROVED")], pid=38)
    assert _refusal(out) == "E_ALREADY_DECIDED"


def test_a_proposal_from_another_goal_is_not_found():
    out = _named([], pid=38)
    assert _refusal(out) == "E_NO_SUCH_PROPOSAL"


def test_a_non_numeric_proposal_id_is_refused():
    out = _named([P38], pid="the first one")
    assert _refusal(out) == "E_BAD_PROPOSAL"


def test_all_pending_returns_every_one():
    out = _named([P38, P39], all_pending=True)
    assert out["proposals"] == [38, 39]


def test_all_pending_with_none_waiting_says_so():
    out = _named([], all_pending=True)
    assert _refusal(out) == "E_NOTHING_TO_APPROVE"


def test_the_ambiguity_refusal_names_the_way_out():
    """Without this the agent asks, is answered, and asks again."""
    out = _bare([P38, P39])
    msg = getattr(out, "message", "")
    assert "proposal_id" in msg and "all_pending" in msg


def test_a_partial_failure_across_a_batch_is_reported():
    """Approving two of three and reporting "ok" is the shape that
    loses work."""
    src = io.open(ROOT / "app/services/go_response.py",
                  encoding="utf-8").read()
    fn = src[src.index('if "proposals" in checked:'):]
    fn = fn[:fn.index('if "proposal" in checked:')]
    assert '"ok": not failed' in fn
    assert "still waiting" in fn


def test_the_prompt_teaches_both():
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    sec = md[md.index('### When somebody says "approved"'):]
    sec = sec[:sec.index("### ", 10)]
    assert "`proposal_id`" in sec
    assert "`all_pending`" in sec
    assert "not a dead end" in sec
