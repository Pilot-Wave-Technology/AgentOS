"""The LLM weightage machinery is gone, and nothing it held up fell over.

WHAT WENT: a Gemini job that read a goal's description and wrote a
number on it, a watchdog that noticed when the job had not run, a model
purpose, and a secret. A goal's weight is now `1 + items completed in
it this period`, computed from the taximeter view at score time:
explainable, stable between runs, and free.

WHAT STAYED, and these matter more than the removals: the task-runner
JOB, which `document_render` and `goal_resources` still use, and the
`goals.weightage` columns, which nothing reads now but which are not
worth a migration to drop.

THE DASHBOARD REFRESH MOVED rather than went. It ran on every web
instance, so every instance rebuilt every materialized view -- the work
done N times, with N copies contending for the same locks. The
scheduler registry's own test is "does running two of them do the work
twice, or half each", and this does it twice.
"""
from __future__ import annotations

import pathlib

from app.services.tests.shipped_tree import IS_DISTRIBUTION

_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    p = _ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── what went ───────────────────────────────────────────────────────

def test_the_weightage_modules_are_gone():
    for rel in ("app/services/taximeter/weightage_watchdog.py",
                "task_runner/tasks/goal_weightage_recompute.py"):
        assert not (_ROOT / rel).exists(), f"{rel} is still here"


def test_nothing_imports_them():
    """A deleted module that something still imports is a crash at
    startup, not a tidy-up."""
    offenders = []
    for path in list((_ROOT / "app").rglob("*.py")) + \
                list((_ROOT / "task_runner").rglob("*.py")):
        if "tests" in path.parts:
            continue
        # COMMENTS ARE NOT IMPORTS. A line explaining WHY something was
        # retired is worth keeping -- it is the answer to "where did the
        # goal weight come from before?" -- and a test that forbids the
        # name outright would delete the explanation along with the code.
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8", errors="ignore").split("\n")
            if not line.lstrip().startswith("#"))
        if "weightage_watchdog" in code or "goal_weightage_recompute" in code:
            offenders.append(path.relative_to(_ROOT).as_posix())
    assert not offenders, f"still referenced by: {offenders}"


def test_the_task_runner_registry_has_no_goal_weight_handler():
    src = _read("task_runner/tasks/__init__.py")
    assert "goal_weightage_recompute" not in src
    assert "document_render" in src, "the job's other handler went with it"


def test_the_misc_model_purposes_have_no_goal_weight_entry():
    src = _read("app/services/misc_models.py")
    assert "goal_weightage_recompute" not in src
    assert "goal_document" in src or "document" in src, \
        "the other purposes went too"


def test_the_task_runner_no_longer_asks_for_a_model_key():
    """The only handler that called a model is gone, so the secret can
    come off the deploy."""
    assert "GEMINI_API_KEY" not in _read("task_runner/config.py")
    if IS_DISTRIBUTION:
        # Terraform builds the task-runner job here; its secrets are the
        # ones the job's filter lets through.
        tf = _read("terraform/modules/cloud_run/main.tf")
        i = tf.find('resource "google_cloud_run_v2_job" "task_runner"')
        assert i > 0, "the task-runner job moved"
        window = tf[i:]
        assert "GEMINI_API_KEY" not in window, \
            "the task-runner job is still given a Gemini key"
        return
    tr_block = _read("cloudbuild-feature.yaml")
    i = tr_block.find("${_TASK_RUNNER_JOB}")
    assert i > 0, "the task-runner deploy step moved"
    window = tr_block[i:i + 1200]
    assert "GEMINI_API_KEY" not in window, \
        "the task-runner job is still given a Gemini key"


# ── what stayed ─────────────────────────────────────────────────────

def test_the_task_runner_job_itself_survives():
    """`document_render` and `goal_resources` use it. Removing the job
    with its last LLM handler would have taken them out."""
    if IS_DISTRIBUTION:
        # The distribution provisions the job in terraform, not the build.
        assert 'resource "google_cloud_run_v2_job" "task_runner"' in _read(
            "terraform/modules/cloud_run/main.tf"), "the job is no longer created"
        assert "TASK_RUNNER_JOB_NAME" in _read("terraform/locals.tf"), (
            "the job exists and the control panel is not told its name")
    else:
        assert "TASK_RUNNER_JOB_NAME" in _read("cloudbuild-feature.yaml")
    assert (_ROOT / "task_runner/tasks/document_render.py").exists()


def test_the_trigger_survives_with_one_caller_fewer():
    src = _read("app/services/task_runner_trigger.py")
    assert src, "the trigger was removed with the watchdog that needed it"
    assert "weightage" not in src.lower() or "retired" in src.lower()


# ── the refresh loop has exactly one home ───────────────────────────

def test_the_dashboard_refresh_is_in_the_scheduler_registry():
    src = _read("app/services/scheduler/registry.py")
    assert "dashboard_refresh" in src, "the loop is registered nowhere"


def test_main_no_longer_starts_it_as_well():
    """REGISTERED TWICE IS WORSE THAN REGISTERED NOWHERE: the scheduler
    leader and every web instance would both run it, which is the
    duplication the move was for."""
    src = _read("app/main.py")
    assert "create_task(_dashboard_refresh_loop" not in src
    assert "async def _dashboard_refresh_loop" not in src


def test_the_loop_module_exists_and_is_importable():
    from app.services.dashboard_refresh import run_loop

    assert callable(run_loop)


def test_one_failing_view_does_not_stop_the_others():
    """`asyncio.gather` propagates the first exception, so one broken
    view used to abort the whole refresh -- leaving every other view
    stale AND skipping the timestamp that says how old the data is."""
    import inspect

    from app.services.dashboard_metrics import _refresh_one

    src = inspect.getsource(_refresh_one)
    assert "except Exception" in src, \
        "one view's failure still takes the whole refresh with it"
    assert "view_name" in src.split("except Exception")[1], \
        "the failure is swallowed without naming the view"


def test_the_v2_view_is_still_registered_for_refresh():
    """It was added in S3; a retirement pass is exactly where something
    like this gets dropped by accident."""
    from app.services.dashboard_metrics import _MAT_VIEWS_BASE

    assert "mv_taximeter_v2" in _MAT_VIEWS_BASE
