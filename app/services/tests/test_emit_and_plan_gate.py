"""Ec2a/Ec2c — the plan gate, and what `emit_artifact` refuses.

Runs anywhere: the plan gate is a set, and every refusal below happens
before `emit_artifact` touches the database. The parts that need real SQL
are in `test_artifact_manifest_sql.py`.

NOTE ON WHY THESE CAN IMPORT AT ALL. Until Ec2c, `from
kernel.harness.models.types import ToolResult` ran `models/__init__`, which
imported both provider SDKs — absent on a dev machine. So every worker-side
tool module was unimportable here and its guards read source with `ast`
instead, which checks that code was WRITTEN and never that it works. That is
how `web_fetch` stayed dispatched to a function that did not exist. The
adapter imports are lazy now, so a tool can be called and asserted against.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from kernel.harness.emit import (MAX_BYTES, ROLES, emit_artifact,
                                 media_type_for, read_in_scope)
from kernel.harness.executor import _OPEN_BEFORE_PLAN, needs_approved_plan
from kernel.tools.catalog import CATALOG


class _Envelope:
    def __init__(self, root):
        self.workspace_path = root


def _call(root, **args):
    """`emit_artifact` with a connection that would explode if reached.

    Every case here must refuse before any SQL, and a None connection makes
    "refused early" and "wrote nothing" the same assertion.
    """
    return asyncio.run(emit_artifact(None, _Envelope(root), 1, args, "tc-1"))


# ── Ec2a: the plan gate, inverted ────────────────────────────────────────

def test_the_gate_is_a_permit_list_so_a_new_tool_is_gated():
    """THE FIX, and the reason the four holes existed. This was a list of
    tools that NEEDED a plan, so anything nobody thought about was ungated
    — `env_setup`, `env_install` and `publish_artifact` all ran before any
    human saw the intent. A permit list fails the other way: an omission is
    a needless refusal that says its own name on the first call."""
    for invented in ("deploy_to_prod", "send_email", "delete_bucket", ""):
        assert needs_approved_plan(invented), invented


@pytest.mark.parametrize("tool", [
    "env_setup", "env_install", "web_fetch", "publish_artifact",
])
def test_the_four_holes_are_closed(tool):
    """Ec2a's named four. All three of the first are mutations that reach
    past the workspace; `web_fetch` is gated on REACH rather than change —
    it only GETs, but the internet is not what the run was granted, and a
    plan is where an executor says which sources it needs."""
    assert needs_approved_plan(tool)


@pytest.mark.parametrize("tool", [
    "write", "edit", "bash", "bash_background", "story_commit",
    "subagent", "propose_completion", "emit_artifact",
])
def test_what_was_already_gated_still_is(tool):
    """Inverting the set must not have dropped anything it used to hold."""
    assert needs_approved_plan(tool)


@pytest.mark.parametrize("tool", sorted(_OPEN_BEFORE_PLAN))
def test_everything_permitted_only_inspects_or_asks(tool):
    """What earns a place: it reads something already granted, or it talks
    to the person whose approval is being waited on. A tool here that
    CHANGES anything is a hole with a comment over it."""
    assert tool in CATALOG, f"{tool} is permitted and does not exist"
    if tool in ("ask_human", "register_plan"):
        return                      # talk to the approver; change nothing
    assert CATALOG[tool]["_effect"] == "contained", (
        f"{tool} is permitted before a plan and reaches outside the "
        f"workspace")


def test_an_executor_can_still_look_around_enough_to_plan():
    """The other failure. A gate that refuses `read` and `list_dir` leaves
    the model unable to write a sensible plan, and it will register a
    guessed one — which a human then approves, because it looks fine."""
    for tool in ("read", "glob", "grep", "list_dir", "skill",
                 "goal_knowledge", "artifact_read", "ask_human",
                 "register_plan"):
        assert not needs_approved_plan(tool), tool


def test_the_refusal_names_what_is_still_open():
    """A refusal that only says no teaches the model to retry. It has to
    say what it CAN do, and `register_plan` is the way out."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/harness/executor.py", encoding="utf-8").read()
    gate = src[src.index("E_PLAN_NOT_APPROVED"):][:700]
    assert "register_plan" in gate
    assert "_OPEN_BEFORE_PLAN" in gate, (
        "the refusal lists tools by hand; it will drift from the set")


# ── Ec2c: what emit refuses, and why ─────────────────────────────────────

def test_a_role_outside_the_closed_set_is_refused(tmp_path):
    """The database CHECK would catch it too, but as a constraint
    violation — the model needs the list of roles, not a stack trace."""
    (tmp_path / "r.md").write_text("x", encoding="utf-8")
    out = _call(str(tmp_path), path="r.md", role="summary", summary="s")
    assert out.exit_code == 1
    assert "E_BAD_ARGS" in out.error
    for role in ROLES:
        assert role in out.error, "the refusal must list the valid roles"


def test_a_missing_summary_is_refused(tmp_path):
    """NOT COSMETIC. A list of hashes answers nothing, and the executor is
    the only party that knows what it just produced — asking later means
    asking a leg that no longer exists."""
    (tmp_path / "r.md").write_text("x", encoding="utf-8")
    out = _call(str(tmp_path), path="r.md", role="report")
    assert out.exit_code == 1 and "summary" in out.error


def test_a_missing_path_or_role_is_refused(tmp_path):
    for args in ({"role": "report", "summary": "s"},
                 {"path": "r.md", "summary": "s"}):
        out = _call(str(tmp_path), **args)
        assert out.exit_code == 1 and "E_BAD_ARGS" in out.error


def test_a_path_outside_the_workspace_is_refused(tmp_path):
    """The same containment stage 1 applies to `write`. Without it an
    errand could emit `/etc/passwd` as a report and make it durable."""
    for escape in ("../secrets.md", "/etc/passwd", "sub/../../out.md"):
        out = _call(str(tmp_path), path=escape, role="report", summary="s")
        assert out.exit_code == 1, escape
        assert "E_ENVELOPE_VIOLATION" in out.error or "E_NO_FILE" in out.error


def test_a_sibling_execution_tree_is_not_inside_this_one():
    """`/workspace/exec-4` must not accept `/workspace/exec-42`. A prefix
    check without the separator is how one run reads another's tree."""
    data, problem = read_in_scope("/workspace/exec-4",
                                  "../exec-42/report.md")
    assert not data and "E_ENVELOPE_VIOLATION" in problem


def test_a_file_that_is_not_there_says_so(tmp_path):
    out = _call(str(tmp_path), path="nope.md", role="report", summary="s")
    assert out.exit_code == 1 and "E_NO_FILE" in out.error


def test_something_too_large_is_refused_not_absorbed(tmp_path):
    """Hashing happens in worker memory. A payload this size is a repo or a
    bucket, not an answer."""
    big = tmp_path / "big.bin"
    with open(big, "wb") as fh:
        fh.write(b"\0" * (MAX_BYTES + 1))
    out = _call(str(tmp_path), path="big.bin", role="dataset", summary="s")
    assert out.exit_code == 1 and "E_TOO_LARGE" in out.error


@pytest.mark.parametrize("name,expected", [
    ("report.md", "text/markdown"), ("rows.csv", "text/csv"),
    ("data.json", "application/json"), ("chart.png", "image/png"),
    ("notes", "text/plain"), ("script.PY", "text/x-python"),
])
def test_the_media_type_is_guessed_from_the_name(name, expected):
    """A hint for a reader, not content negotiation — which is why the
    fallback is `text/plain` rather than something clever."""
    assert media_type_for(name) == expected


# ── the tool is wired, not just written ──────────────────────────────────

def test_emit_artifact_is_declared_routed_and_reachable():
    """`web_fetch` had a routing entry and no function for months. The
    generic binding test cannot see this one — it is dispatched by an
    explicit branch, not through `tools_worker` — so it is named here."""
    from kernel.tools.catalog import PENDING, tools_by_side

    assert "emit_artifact" in CATALOG
    assert "emit_artifact" not in PENDING
    assert "emit_artifact" in tools_by_side("worker")
    assert CATALOG["emit_artifact"]["_effect"] == "reversible"
    assert callable(emit_artifact)


def test_the_executor_dispatches_it():
    """The import must be spelled out where the worker tools are routed.

    ACROSS THE EXECUTOR FAMILY, because wave 4 moved the if-chain from
    `executor.py` to `executor_worker_dispatch.py`. A guard pinned to
    the old path would have passed against a file the dispatch had left.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((root / "kernel/harness").glob("executor*.py")))
    assert 'from kernel.harness.emit import emit_artifact' in src


def test_the_required_arguments_match_what_the_tool_enforces():
    """The schema's `required` and the function's refusals are two
    statements of one rule. A model trusts the schema; the tool enforces
    itself. When they disagree the model is refused for obeying."""
    schema = CATALOG["emit_artifact"]["parameters"]
    assert set(schema["required"]) == {"path", "role", "summary"}
    assert set(schema["properties"]["role"]["enum"]) == set(ROLES)


def test_importing_the_models_package_does_not_need_a_provider_sdk():
    """The change that made every test above possible. If somebody moves
    the adapter imports back to module scope, worker-side tools become
    untestable on a laptop again and the guards silently regress to
    reading source."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/harness/models/__init__.py",
                  encoding="utf-8").read()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and line[:1] not in " \t":
            assert "anthropic" not in stripped and "gemini" not in stripped, (
                f"module-scope import pulls in a provider SDK: {stripped}")
    assert os.path.isfile(root / "kernel/harness/models/anthropic.py")
