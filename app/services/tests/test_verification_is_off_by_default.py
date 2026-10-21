"""Verification is inert unless a platform admin switches it on.

WHY A ROW AND NOT AN ENVIRONMENT VARIABLE. Four capabilities in this
system were off because nobody had set a variable, and not one of them
said so:

    AGENTOS_ASSIST       the assessor, the investigator, the answerer
                         and the scout -- dark since their Deployments
                         existed
    AGENTOS_HUMAN_DONE   nobody was ever asked to accept a story
    BUILD_SHA            every log line claimed the same build
    driver.name          an attribute no driver defines, behind which
                         three reapers returned [] on every tick

Each was invisible the same way: a feature that is off looks exactly
like a feature with nothing to do. A row can be read back by the
product, changed without a redeploy, and attributed to a person.

WHAT OFF MEANS. `propose_completion` measures the claim against the
plan's OWN checks, publishes the work, and parks for a person to accept
or ask for more -- the path errands have always taken. One executor pod
per run, created and reaped. No verification pod, no prepare Job, no
criterion Job. `VERIFYING` stays in the state machine with nothing
entering it, so a run already in flight when the switch moves drains
rather than stranding.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION, shipped_privileges

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """Source with comments and docstrings gone.

    FIFTH TIME IN THIS SUITE. An absence assertion matched the comment
    EXPLAINING the absence -- here, a comment quoting the errand-only
    wording it was replacing. A test that reads its subject's prose
    fails the moment somebody documents the fix.
    """
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
    return ast.unparse(tree)


class _Conn:
    def __init__(self, value=None, boom=False):
        self._value, self._boom = value, boom

    async def fetchval(self, sql, *a):
        if self._boom:
            raise RuntimeError("settings unavailable")
        return self._value


# ── the switch ─────────────────────────────────────────────────────────

def test_off_when_nobody_has_turned_it_on():
    from kernel.conductor.verification_switch import enabled

    assert asyncio.run(enabled(_Conn(None))) is False
    assert asyncio.run(enabled(_Conn(False))) is False


def test_on_only_when_the_row_says_so():
    from kernel.conductor.verification_switch import enabled

    assert asyncio.run(enabled(_Conn(True))) is True


def test_an_unreadable_switch_is_off_and_does_not_raise():
    """Verification CREATES things -- a claim, a pod, prepare Jobs. An
    outage in the settings table must not spend disk and pods across the
    fleet at the moment the database is least able to say why. And a
    switch that could fail a leg would be worse than the problem it
    solves."""
    from kernel.conductor.verification_switch import enabled

    assert asyncio.run(enabled(_Conn(boom=True))) is False


def test_the_default_is_a_fact_in_the_database():
    """An absent row and a row saying false are the same answer today,
    and stop being the same the moment a second reader has a different
    fallback.

    The distribution writes the row in its own migration, 0006, because
    the squashed baseline is schema-only and carries no rows."""
    sql = _src("migrations/0006_platform_settings_defaults.sql"
               if IS_DISTRIBUTION else
               "migrations/platform_verification_switch.sql")

    assert "'verification_enabled', false" in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql


def test_the_leg_can_read_it():
    """`worker/loop.py` consults the switch at `propose_completion`, and
    after the cutover that runs INSIDE the execution pod as
    `agentos_leg`. A switch the leg cannot read fails at the one point
    it is asked -- the same shape as `choice.for_execution` falling
    through to `goals`, which `agentos_leg` also cannot read."""
    if IS_DISTRIBUTION:
        for role in ("agentos_leg", "agentos_worker"):
            assert "SELECT" in shipped_privileges(
                "public.platform_settings", role), (
                f"{role} cannot read the verification switch")
        return
    sql = _src("migrations/platform_verification_switch.sql")

    assert "GRANT SELECT ON platform_settings TO agentos_leg;" in sql
    assert "GRANT SELECT ON platform_settings TO agentos_worker;" in sql


def test_the_kernel_may_read_the_switch_and_never_write_it():
    """Flipping a platform switch is a person's act, through the control
    panel, under `require_admin`."""
    if IS_DISTRIBUTION:
        for role in ("agentos_leg", "agentos_worker"):
            writes = shipped_privileges("public.platform_settings", role) & {
                "INSERT", "UPDATE", "DELETE", "ALL", "ALL PRIVILEGES"}
            assert not writes, f"{role} may write the switch: {writes}"
        return
    sql = _src("migrations/platform_verification_switch.sql")

    for verb in ("INSERT ON platform_settings", "UPDATE ON platform_settings",
                 "ALL ON platform_settings"):
        assert f"GRANT {verb}" not in sql


# ── nothing reaches VERIFYING ──────────────────────────────────────────

def test_the_entry_to_verifying_is_gated():
    """ONE DECISION POINT, so VERIFYING is unreachable rather than
    reachable and then abandoned."""
    tree = ast.parse(_src("kernel/worker/loop.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_take_leg":
            continue
        body = ast.unparse(node)
        assert "verification_enabled" in body, (
            "_take_leg moves runs to VERIFYING without consulting the "
            "switch, so turning verification off does nothing")
        return
    raise AssertionError("_take_leg is gone; re-point this check")


def test_the_profile_still_wins_where_it_says_no():
    """The switch may only ever turn verification OFF. An errand has
    `has_verifier = false` in its own toml and must not acquire one
    because a platform admin turned the switch on."""
    body = _src("kernel/worker/loop.py")

    assert "profile.has_verifier" in body and "verification_enabled" in body
    i = body.index("verification_enabled(conn)")
    window = body[max(0, i - 400):i]
    assert "has_verifier" in window, (
        "the switch is consulted without the profile's own answer, so it "
        "could turn verification on for a profile that does not want it")


def test_no_volume_is_provisioned_for_work_that_will_not_verify():
    """`_overlap` builds the environment while the model still works.
    With nothing reaching VERIFYING, every claim and pod it provisions
    is a 10GiB disk created for a verification that never comes -- one
    per story, against the 120-claim ceiling."""
    tree = ast.parse(_src("kernel/verifier/rebuild.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_overlap":
            continue
        assert "verification_enabled" in ast.unparse(node), (
            "_overlap provisions a verification volume regardless of the "
            "switch, so a story that will never verify still costs a "
            "claim and a pod")
        return
    raise AssertionError("_overlap is gone; re-point this check")


def test_verifying_remains_a_state():
    """It is unreachable, not removed. A run already in flight when the
    switch moves has to drain through the states it is already in."""
    body = _src("kernel/verifier/adjudicator.py")

    assert "'VERIFYING'" in body, (
        "the verifier no longer scans for VERIFYING, so a run in flight "
        "when the switch moved is stranded")


# ── and nothing is lost on the way out ─────────────────────────────────

def test_the_unverified_path_publishes_before_it_finishes():
    """`publish_execution` had ONE caller -- the verified path. Every run
    finishing unverified was accepted with its commits still local to
    the sandbox, which is the failure `finish_passed_claim` records: the
    engine merged a remote branch that "held NOTHING, and announced a
    landing that never happened". Harmless while this path served only
    errands; it loses work the moment it is the default."""
    tree = ast.parse(_src("kernel/worker/loop.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_finish_unverified":
            continue
        body = ast.unparse(node)
        assert "publish_execution" in body, (
            "a run that finishes unverified is never pushed")
        at_publish = body.index("publish_execution")
        # THE GATE, WHICHEVER IT IS. This anchored on
        # `standing_approval` -- the grant branch that used to finish a
        # run here -- and that branch is gone: the path always parks
        # now. The property is unchanged and is about ORDER: a run must
        # be PUSHED before anybody is asked to accept it, or they are
        # accepting work that is not there.
        # THE CALL, not the name. `open_request` is imported locally at
        # the top of the function, so matching the bare name found the
        # import and reported the acceptance as happening first.
        at_grant = body.index("await open_request(")
        assert at_publish < at_grant, (
            "the acceptance is opened before the work is pushed, so "
            "somebody is asked to accept a run with nothing in it")
        return
    raise AssertionError("_finish_unverified is gone; re-point this")


def test_neither_self_check_execs_into_the_sandbox():
    """Execution 214's bug cannot recur here, because the call it lived
    in is gone. `driver.exec` resolves the host itself when not given
    one -- `read_namespaced_pod` against an API `agentos-execution` has
    no Role to call, refused through the egress proxy with 403 -- and
    the worker used that to re-run command criteria in the executor's
    sandbox.

    Nothing re-runs them now: an artifact check is a question about the
    manifest, and a command check belongs to the Verifier. So the guard
    is the absence of the exec, which is stronger than getting its
    arguments right."""
    for rel in ("kernel/worker/selfcheck.py", "kernel/harness/selfcheck.py"):
        body = _code(rel)
        assert "driver.exec" not in body and ".exec(" not in body, (
            f"{rel} executes a check again; from inside the execution "
            f"pod an unaddressed exec is a 403, and every command "
            f"criterion would come back undecided")


def test_the_worker_does_not_run_the_checks_a_second_time():
    """They ran twice: in the pod at `propose_completion`, then again
    worker-side through `driver.exec` into the SAME sandbox, seconds
    later, against the SAME tree. Never independent -- the sandbox is
    the executor's either way -- so it bought nothing and cost a full
    repeat of the suite, which now may legitimately take an hour."""
    body = _code("kernel/worker/selfcheck.py")

    assert "driver.exec" not in body, (
        "the worker still executes the checks itself")
    assert "criterion_results" in body, (
        "the worker neither runs the checks nor reads what the executor "
        "recorded, so nothing measures the claim")


def test_the_person_is_asked_and_the_question_fits_the_work():
    """This path served errands only, and said so. A story finishing
    here would be described to the person as an errand."""
    body = _code("kernel/worker/loop.py")

    assert "acceptance" in body
    assert "the errand says it is finished" not in body, (
        "the acceptance question is hardcoded to errands, and stories "
        "finish on this path now")


def test_an_errand_still_measures_the_artifacts_it_owed():
    """Its checks are "almost always artifacts", and the self-check is
    what measures them now that no verifier will. Measured where the
    checks run, which is the executor."""
    assert "artifact_kind" in _src("kernel/harness/selfcheck.py")


# ── the operator surface ───────────────────────────────────────────────

def test_the_toggle_is_platform_admin_only():
    body = _src("app/routers/admin_controls.py")

    i = body.index('"/settings/controls/verification"')
    window = body[i:i + 900]
    assert "require_admin" in window, (
        "the verification switch is not behind require_admin")


def test_every_change_is_recorded_with_its_transition():
    """"Somebody set it to off" and "somebody turned it off" are
    different facts when two admins act a second apart."""
    body = _src("app/routers/admin_controls.py")

    assert "execution.verification_changed" in body
    assert '"was"' in body and '"now"' in body


def test_a_form_posting_the_string_false_does_not_turn_it_on():
    """Form bodies carry strings, and `bool("false")` is True."""
    from app.routers.admin_controls import _flag_from

    class _Req:
        def __init__(self, payload):
            self._p = payload

        async def json(self):
            return self._p

    for raw, want in (("false", False), ("0", False), ("off", False),
                      ("true", True), ("1", True), (True, True),
                      (False, False), (None, False)):
        got = asyncio.run(_flag_from(_Req({"enabled": raw}), "enabled"))
        assert got is want, f"{raw!r} read as {got}"


@pytest.mark.parametrize("claim", [
    "Status: INERT BY DEFAULT",
    "VERIFYING",
])
def test_the_document_says_it_is_off(claim):
    """The doc is what somebody reads before trusting a verdict; it has
    to lead with the fact that none of it is running."""
    if IS_DISTRIBUTION:
        # docs/ does not ship; the distribution's execution guide carries
        # the same claim in its own words.
        said = {"Status: INERT BY DEFAULT": "Verification is **off by default**"}
        assert said.get(claim, claim) in _src("HOW_A_STORY_EXECUTES.md")
        return
    assert claim in _src("docs/VERIFICATION.md")


def test_the_switch_has_a_rendered_control_and_not_only_a_route():
    """An endpoint with nothing rendering it is a switch only somebody
    holding a curl command can reach, which is not what "turnable from
    the control panel" means. Both directions, on the same page as
    Freeze System, which shares its gate."""
    page = _src("app/templates/admin/controls.html")

    assert "verificationOn()" in page and "verificationOff()" in page
    assert "/settings/controls/verification" in page
    assert "verification_on" in page, (
        "the control renders without reading the current value, so it "
        "cannot show which way the switch is set")


def test_the_buttons_are_hidden_from_a_non_admin():
    """`require_admin` refuses the POST either way; a button that only
    ever produces a 403 is a bug report waiting to be filed."""
    page = _src("app/templates/admin/controls.html")

    i = page.index("verificationOff()")
    j = page.index("verificationOn()", i)
    assert "can_edit" in page[i - 600:j + 200], (
        "the toggle buttons are rendered without the can_edit gate")


def test_the_page_says_which_way_the_switch_is_set():
    """The failure this whole switch exists to prevent is a capability
    being off with nothing saying so."""
    page = _src("app/templates/admin/controls.html")

    assert "Verification off" in page and "Verification on" in page


def test_an_unreadable_switch_renders_as_off():
    """Same direction as the kernel's own fallback. A settings-table
    outage must not have the panel claim verification is running."""
    body = _src("app/routers/admin_controls.py")

    i = body.index("_verification_state")
    window = body[i:i + 1400]
    assert "except Exception" in window and '"verification_on": False' in window


def test_the_poll_does_not_pay_for_it():
    """`_panel_data` is called by the 10s HTMX fragment, and its own
    docstring records two dead queries removed for running there."""
    tree = ast.parse(_src("app/routers/admin_controls.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_panel_data":
            assert "platform_settings" not in ast.unparse(node), (
                "the verification switch is read on the 10s fragment poll, "
                "for a value the fragment does not render")
            return
    raise AssertionError("_panel_data is gone; re-point this check")
