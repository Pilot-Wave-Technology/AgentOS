"""A declared check can finally be run by the thing that declared it.

WHAT WAS IN THE WAY, in `selfcheck.py`'s own words: a command criterion
ran "as a foreground `bash` pinned to a 120s ceiling, which meant a
suite slower than two minutes could never pass; and running it uncapped
would repeat, in the executor's own sandbox, a suite the executor had
just run as its work."

A job with the criterion's OWN `timeout_s` removes the first half. The
second half is not removed and must not be: this runs a check ONCE, on
request, under a budget the contract already states. It does not turn
verification on and nothing re-runs anything.

THE BUDGET IS THE CONTRACT'S. A model choosing the budget for the check
that judges it is setting its own measurement -- the argument
`verifier/budget` makes about contracts generally.
"""
from __future__ import annotations

import inspect

from kernel.harness.tools_worker import checks


def test_the_budget_comes_from_the_contract_not_the_model():
    src = inspect.getsource(checks.run_check)
    assert "timeout_for(" in src
    assert "criterion)" in src.split("timeout_for(")[1][:80]
    assert "tool_args" not in src.split("timeout_for")[1][:200], (
        "the model must not be able to name the budget")


def test_the_criteria_come_from_the_one_reader_that_already_had_them():
    """EXECUTION 253 PAID FOR THE SECOND READER. This file used to query
    `contracts.criteria` and `contracts.verify_timeout_s`; neither column
    exists, criteria are ROWS in `kernel.criteria` keyed by contract, and
    the live call answered `UndefinedColumnError: column c.criteria does
    not exist`. `selfcheck.checks_for` had been reading them correctly
    the whole time."""
    src = inspect.getsource(checks.run_check)
    code = chr(10).join(l for l in src.splitlines()
                        if l.strip() and not l.lstrip().startswith("#"))
    assert "checks_for" in code
    assert "c.criteria" not in code
    assert "verify_timeout_s" not in code


def test_a_label_is_not_treated_as_a_name():
    """253's two criteria are BOTH labelled `goal`. The label says what a
    check promises -- fail now, or hold now -- and the prompt tells the
    model to use exactly those two words, so it identifies nothing."""
    both = [{"run": "pytest", "label": "goal"},
            {"run": "pytest -k divide", "label": "goal"}]

    # Passing the label picks nothing, and the refusal shows why: it is
    # on both, so it cannot be the thing that distinguishes them.
    picked, refusal = checks._pick(both, "goal")
    assert picked is None
    assert "pytest" in refusal and "pytest -k divide" in refusal

    # And the command DOES distinguish them.
    assert checks._pick(both, "divide")[0]["run"] == "pytest -k divide"


def test_one_check_needs_no_argument():
    picked, refusal = checks._pick([{"run": "pytest", "label": "goal"}], "")
    assert refusal == "" and picked["run"] == "pytest"


def test_several_checks_with_no_argument_lists_them_rather_than_guessing():
    """Running the wrong forty-minute suite costs more than asking."""
    picked, refusal = checks._pick(
        [{"run": "pytest", "label": "goal"},
         {"run": "ruff check .", "label": "guard"}], "")
    assert picked is None
    assert "pytest" in refusal and "ruff check ." in refusal


def test_a_check_can_be_named_by_its_command_or_its_number():
    checks_list = [{"run": "pytest tests", "label": "goal"},
                   {"run": "ruff check .", "label": "guard"}]
    assert checks._pick(checks_list, "ruff")[0]["run"] == "ruff check ."
    assert checks._pick(checks_list, "2")[0]["run"] == "ruff check ."
    assert checks._pick(checks_list, "1")[0]["run"] == "pytest tests"


def test_an_artifact_only_plan_says_so_rather_than_failing_oddly():
    """An errand's plan is usually artifact checks alone."""
    picked, refusal = checks._pick([], "")
    assert picked is None
    assert "no command checks" in refusal
    assert "measured at the claim" in refusal


def test_it_uses_the_ordinary_job_machinery():
    """A second way to run a command in the sandbox would be a second
    watchdog, a second kill path and a second buffer -- the duplication
    the registry merge existed to remove."""
    src = inspect.getsource(checks.run_check)
    assert '"bash_background"' in src
    assert "subprocess" not in src


def test_it_mirrors_the_job_it_starts():
    """Otherwise a check running for forty minutes is invisible to
    anyone asking how the story is going."""
    assert "_mirror_job" in inspect.getsource(checks.run_check)


def test_an_unknown_name_lists_the_ones_that_exist():
    """A refusal naming only the mistake makes the model guess again."""
    picked, refusal = checks._pick(
        [{"run": "pytest tests", "label": "goal"}], "mypy")
    assert picked is None
    assert "pytest tests" in refusal

    picked, refusal = checks._pick(
        [{"run": "pytest tests", "label": "goal"}], "7")
    assert picked is None
    assert "there is no check 7" in refusal and "pytest tests" in refusal


def test_it_says_the_result_is_not_a_verdict():
    """`selfcheck` reports command criteria as `unmeasured` and a person
    reads the counts. A measurement the executor took of its own check
    must not read as the thing that decides."""
    src = inspect.getsource(checks.run_check)
    assert "not a verdict" in src


def test_it_is_a_worker_tool_because_the_contract_is_in_the_database():
    from kernel.tools.catalog import tools_by_side

    assert "run_check" in set(tools_by_side("worker"))
    assert "run_check" not in set(tools_by_side("sandbox"))


def test_it_is_wired_to_a_connection():
    """The bug this generalises: `web_fetch` was dispatched to a function
    that did not exist and every call answered E_TOOL_RAISED."""
    from kernel.harness import executor_worker_dispatch as dispatch

    assert "run_check" in dispatch._NEEDS_CONN
    assert "run_check" in inspect.getsource(dispatch.worker_execute)


# -- what the leg is allowed to read -----------------------------------

# `agentos_leg` is deliberately privilege-free and gets SELECT one table
# at a time, by an explicit grant in a migration. Verified against the
# live `next` database on 2026-09-21 with `has_table_privilege`.
LEG_MAY_READ = {"criteria", "executions", "execution_jobs", "questions",
                "answers", "step_events"}

# Named because it is the one that bit: run 255 answered `permission
# denied for table contracts` from this very function.
LEG_MAY_NOT_READ = {"contracts", "story_timings", "stories", "goals",
                    "goal_resources", "git_nodes", "story_reviews"}


def _tables_named(*functions) -> set:
    """Every table a SELECT in these functions reads."""
    import inspect
    import re

    found = set()
    for fn in functions:
        for match in re.finditer(r"\bFROM\s+(?:kernel\.)?(\w+)|"
                                 r"\bJOIN\s+(?:kernel\.)?(\w+)",
                                 inspect.getsource(fn), re.I):
            found.add((match.group(1) or match.group(2)).lower())
    return found


def test_run_check_reads_no_table_the_leg_is_denied():
    """THE THIRD TIME THIS SHAPE HAS COST A RUN. 245 died on
    `story_reviews`, 253 on a column that did not exist, and 255 on
    `contracts` -- a query that could only ever have contributed nothing,
    because `timeout_for` looks for `verify_timeout_s` and
    `kernel.contracts` has no such column.

    The leg's role is privilege-free BY DESIGN
    (`kernel_execution_pod_leg_role.sql` says so in capitals), so a new
    read here is a deployment failure that only appears in the cluster.
    """
    from kernel.harness import claim
    from kernel.harness.selfcheck import checks_for

    touched = _tables_named(checks.run_check, claim.prior_run, checks_for)
    denied = touched & LEG_MAY_NOT_READ
    assert not denied, (
        f"run_check reads {sorted(denied)}, which agentos_leg cannot: "
        f"the call fails in the pod and nowhere else")
    assert touched, "the scan found no tables at all; the regex broke"


def test_the_budget_needs_no_contract_row():
    """`timeout_for` consults the criterion's `spec.timeout_s` and then
    the contract's `verify_timeout_s`, which is not a column on
    `kernel.contracts`. Reading the row and passing nothing are the same
    answer, so the row is not read."""
    from kernel.verifier.budget import DEFAULT_S, timeout_for

    assert timeout_for({}, {"spec": {"timeout_s": 1200}}) == 1200
    assert timeout_for({}, {"spec": {}}) == DEFAULT_S


# -- the declared budget has to survive into the criterion -------------

def test_a_declared_budget_reaches_the_criterion_in_either_spelling():
    """RUN 255 WROTE IT NESTED AND THE PLATFORM DROPPED IT. The schema
    asks for `timeout_s` at the top of a check; the executor wrote
    `spec: {timeout_s: 1200}`, which is where the value ends up and
    where every reader looks for it. Only the documented spelling was
    honoured, in silence.

    That is worse than a refusal. The approved card showed a 1200s
    budget, the stored criterion carried none, and `run_check` would
    have started a 13-minute suite on the 300s default and killed it at
    five -- the tool that exists to run a long check destroying the run
    it was asked to measure.
    """
    from kernel.verifier.budget import DEFAULT_S, timeout_for
    from kernel.worker.contracts import normalise

    top = normalise([{"run": "pytest", "label": "goal", "timeout_s": 1200}])
    nested = normalise([{"run": "pytest", "label": "goal", "kind": "command",
                         "spec": {"timeout_s": 1200}}])
    assert timeout_for({}, top[0]) == 1200
    assert timeout_for({}, nested[0]) == 1200, (
        "a budget written where the plan card shows it is dropped")

    plain = normalise([{"run": "pytest", "label": "guard"}])
    assert timeout_for({}, plain[0]) == DEFAULT_S


def test_a_budget_nobody_can_read_falls_back_rather_than_raising():
    from kernel.verifier.budget import DEFAULT_S, timeout_for
    from kernel.worker.contracts import normalise

    for junk in ("soon", None, "", {}):
        got = normalise([{"run": "pytest", "label": "goal",
                          "spec": {"timeout_s": junk}}])
        assert timeout_for({}, got[0]) == DEFAULT_S, junk
