"""Local execution takes hand-started errands and refuses autonomous ones.

THE TEMPTING SHORTHAND IS WRONG. `work_kind <> 'errand'` would refuse the
hand-started errands local execution is FOR. `kernel/go/drivable.py` draws the
line for the GO and its opening paragraph is the same line read the other way:

    Stories and hand-started errands stay with the assistant, and the reason is
    not policy but access: that is where the human conversation is... An
    autonomous errand is the opposite -- a person approved a plan once and left.

A person working a story in their own editor IS that conversation.

WHY IT MATTERS MORE THAN A TIDY RULE. `work_kind` appeared NOWHERE in the local
path, so an autonomous errand imported cleanly -- and the kernel cannot see
`execution_mode`, so it kept dispatching pod runs on the grant cadence against
the branch the person was working. Two writers, one branch, neither aware.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── one definition, not two ──────────────────────────────────────────

def test_the_predicate_is_exported_rather_than_restated():
    """Two copies of this would drift silently, and in the direction that
    admits an errand the GO is also driving."""
    body = src("kernel/go/drivable.py")
    assert "def autonomous_errand_predicate(" in body
    assert "async def is_autonomous_errand(" in body


@pytest.mark.parametrize("rel,what", [
    ("app/services/local_hydrate.py", "autonomous_errand_predicate"),
    ("app/services/local_checkout.py", "is_autonomous_errand"),
])
def test_both_local_gates_use_the_shared_definition(rel, what):
    body = src(rel)
    assert "from kernel.go.drivable import" in body, f"{rel} does not import it"
    assert what in body, f"{rel} does not use {what}"


def test_neither_gate_uses_the_tempting_shorthand():
    """`work_kind <> 'errand'` would refuse hand-started errands, which are
    exactly what local execution is for."""
    for rel in ("app/services/local_hydrate.py", "app/services/local_checkout.py"):
        body = src(rel)
        assert not re.search(r"work_kind\s*(<>|!=)\s*['\"]errand", body), (
            f"{rel} refuses every errand, not only the autonomous ones")


# ── the SQL says what the helper says ────────────────────────────────

def test_the_two_forms_spell_the_same_two_conditions():
    """One is for a WHERE clause over many rows, the other for a single story.
    They must not be able to disagree."""
    from kernel.go.drivable import autonomous_errand_predicate

    sql = autonomous_errand_predicate("s").lower()
    for condition in ("work_kind", "errand", "errand_schedules",
                      "approved_by is not null", "revoked_at is null",
                      "expires_at"):
        assert condition in sql, f"the list predicate omits {condition!r}"


def test_the_inner_alias_cannot_collide_with_the_caller():
    """Every current caller's outer table is already `s`; an inner `s` would
    silently correlate against the wrong row."""
    from kernel.go.drivable import autonomous_errand_predicate

    sql = autonomous_errand_predicate("s")
    assert "FROM kernel.errand_schedules es" in sql
    assert "es.goal_id = s.goal_id" in sql


# ── the gate refuses rather than guesses ─────────────────────────────

def test_a_database_that_cannot_answer_refuses_the_import():
    """`is_autonomous_errand` raises rather than answering False, and the gate
    turns that into a refusal. A helper that answered "no" on a bad query would
    open the boundary exactly when the database is unwell."""
    body = src("kernel/go/drivable.py")
    fn = body.split("async def is_autonomous_errand")[1][:900]
    assert "except" not in fn, (
        "is_autonomous_errand swallows the error, so a failed query reads as "
        "'not autonomous' and the import is admitted")

    gate = src("app/services/local_checkout.py")
    assert "autonomous_errand_unknown" in gate, (
        "the import gate does not refuse when it cannot establish the answer")


def test_the_gate_runs_before_the_resume_short_circuit():
    """A story that ACQUIRED a grant after being checked out must be refused
    too, not resumed -- the same reason the multi-repo interlock sits there."""
    body = src("app/services/local_checkout.py")
    guard = body.index("is_autonomous_errand(pool")
    resume = body.index("resume of my own local story")
    assert guard < resume, (
        "an already-checked-out story that became autonomous would resume")


# ── and the kernel stops dispatching what it no longer owns ──────────

def test_the_kernel_refuses_a_locally_checked_out_story():
    """`one_live_run` stops two KERNEL runs on one story. Nothing stopped a
    kernel run starting on a story checked out to somebody's machine -- the
    pod would clone the branch the person is working in and push over them."""
    body = src("kernel/assistant/write.py")
    assert "E_RUNS_LOCALLY" in body, "the kernel still dispatches local stories"
    live = body.index("E_ALREADY_RUNNING")
    local = body.index("E_RUNS_LOCALLY")
    assert local > live, "the local guard should sit beside the one-live-run guard"


def test_the_kernel_can_see_execution_mode_at_all():
    """It could not, anywhere, before this. That absence is what made the
    double-drive invisible rather than merely possible."""
    hits = 0
    for p in (ROOT / "kernel").rglob("*.py"):
        hits += p.read_text(encoding="utf-8").count("execution_mode")
    assert hits > 0, "kernel/ is still blind to locally-checked-out work"
