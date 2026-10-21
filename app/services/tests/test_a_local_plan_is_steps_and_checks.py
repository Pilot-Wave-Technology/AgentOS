"""A local plan is the same object a pod's plan is, validated the same way.

The local lane planned in `## Task DAG` YAML -- an h2 heading, a fenced block,
ids matching ^T\d+$, a `phase` drawn from the `claude_phases` registry, and
one child task per entry. None of that vocabulary exists in the kernel. Two
planning languages for one platform meant a plan could not be read, approved
or reasoned about the same way depending on where the story ran.

THE VALIDATION IS IMPORTED, NOT REWRITTEN, and that is the point of these
tests: every rule below was already argued for and already paid for by a real
failure. Re-deriving them locally would be re-earning them, and the copy would
drift.
"""

import pytest

from app.services.tests._source import code, source as src


def checks(*raw):
    from app.services.local_plan import normalise_checks
    return normalise_checks(list(raw))


# ── the rules, which are the kernel's ────────────────────────────────

def test_a_well_formed_plan_is_accepted():
    from app.services.local_plan import refusal_for
    ok = checks({"run": "cd repos/acme__api && pytest", "label": "goal"})
    assert refusal_for(["do the thing"], "because", ok) is None


def test_a_plan_needs_steps():
    from app.services.local_plan import refusal_for
    assert refusal_for([], "why", []) 
    assert refusal_for(["  "], "why", [])


def test_every_check_must_be_labelled():
    """`goal` must FAIL now and pass when done -- that is what makes it
    evidence rather than decoration."""
    from app.services.local_plan import refusal_for
    r = refusal_for(["x"], "y", checks({"run": "cd repos/a__b && pytest"}))
    assert r and "label" in r


def test_a_command_check_must_say_where_it_runs():
    """One global PATH made the first repository's .venv shadow the second's,
    and the failure read as "the tests are broken"."""
    from app.services.local_plan import refusal_for
    r = refusal_for(["x"], "y",
                    checks({"run": "pytest repos/acme__api/tests", "label": "goal"}))
    assert r and "repository director" in r


# ── and that is what settles two clones of one repo ──────────────────

def test_two_clones_of_one_repo_are_two_directories():
    """A lane-qualified mount is `repos/<owner>__<name>@<branch>`, so a check
    that must `cd` into one can never be ambiguous about which working tree it
    means. This needed no new rule -- only the kernel's existing one."""
    from app.services.local_plan import refusal_for
    for lane in ("calculator", "main"):
        assert refusal_for(
            ["x"], "y",
            checks({"run": f"cd repos/acme__api@{lane} && pytest", "label": "goal"})
        ) is None, f"a check naming the {lane} clone was refused"


def test_naming_a_lane_without_cd_is_still_refused():
    from app.services.local_plan import refusal_for
    r = refusal_for(["x"], "y",
                    checks({"run": "pytest repos/acme__api@main/tests", "label": "goal"}))
    assert r


# ── the hash is the kernel's ─────────────────────────────────────────

def test_the_hash_is_the_kernels_own():
    """An approval binds to the exact plan it was shown. Two hash functions
    for one concept is how they come to disagree about what was approved."""
    from kernel.harness.park_approval import plan_hash
    from app.services.local_plan import digest

    c = checks({"run": "cd repos/a__b && pytest", "label": "goal"})
    assert digest(["a"], "b", c) == plan_hash(["a"], "b", c)


def test_editing_a_plan_changes_its_hash():
    from app.services.local_plan import digest
    c = checks({"run": "cd repos/a__b && pytest", "label": "goal"})
    assert digest(["a"], "b", c) != digest(["a", "and more"], "b", c)


# ── the DAG is gone from the local lane ──────────────────────────────

@pytest.mark.parametrize("rel", [
    "app/services/local_plan_gate.py",
    "app/services/local_dispatch.py",
    "app/services/local_diff.py",
])
def test_no_local_service_builds_or_reads_a_dag(rel):
    body = code(rel)
    for gone in ("parse_dag_from_markdown", "ingest_approved_plan",
                 "claude-code-dag", "claude-code-phase"):
        assert gone not in body, f"{rel} still uses {gone}"


def test_the_dispatch_endpoints_are_gone():
    """They were the graph's dispatch loop. Leaving them would answer about a
    graph nothing builds."""
    body = src("app/routers/local_execution_run.py")
    for gone in ('"/next-phase"', '"/task-state"', '"/report-phase"'):
        assert gone not in body, f"{gone} is still routed"


def test_get_state_survives_because_it_is_not_part_of_the_loop():
    """The state-injection hook reads it every turn to tell the session where
    it is, which is as true of a self-operated story as a dispatched one."""
    assert '"/get-state"' in src("app/routers/local_execution_run.py")


# ── done is the person's, and the loosening is deliberate ────────────

def test_done_requires_a_plan_and_nothing_else():
    """The DAG clause was the only thing stopping a local story being marked
    done with nothing executed. Nothing replaces it: the person is
    self-operating, they can see the tree, and "done" is their click. The plan
    requirement stays for a different reason -- a story marked done with no
    plan leaves nothing on the board saying what was meant to happen."""
    body = code("app/services/local_dispatch.py")
    fn = body[body.index("async def story_done_blocker"):]
    fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
    assert "plan_versions" in fn, "a story can be marked done with no plan at all"
    assert "claude-code-phase" not in fn, "the DAG clause is still there"


def test_a_diff_needs_a_plan_not_a_graph():
    body = code("app/services/local_diff.py")
    assert "no_dag_awaiting_review" not in body
    assert "no_approved_plan" in body
