"""An unreachable artifact store costs range reads, never the leg.

MEASURED ON EXECUTION 222, and the shape is worth keeping because the
correct behaviour was already written and simply out of reach.

Since E7, a tool output over 32 KB is STORED rather than truncated, so
the model gets head+tail plus an id it can read ranges from. The spill
caught exactly one exception -- `ArtifactTooLarge` -- and the `else`
branch below it, which truncates and says so, was unreachable for every
other failure. `ARTIFACT_STORE=gcs` was copied into the execution pod
from the conductor; `storage.Client()` takes its token AND its project
from the metadata server; and the execution pod is denied that server
deliberately, because its sandbox shares the pod's network namespace.

So `storage_client()` raised OSError, the exception left `run_tool` and
`run_leg`, and the leg died -- on `git log -p`, whose five most recent
commits in that repository total 133 KB of patch, one of them 95 KB on
its own. Six generations died the same way. The model even narrowed to
`git log -p -n 5` and was still four times over the threshold. It never
reached the file the story asked for.

TWO FIXES, BOTH NEEDED. The pod must have a store it can reach (the
filesystem binding, rooted on its workspace), AND a store that cannot be
reached must degrade to truncation. Either alone leaves the other
failure live: a bucket outage would still kill legs, and a narrowed
`except` would still cost every range read in the fleet.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the leg survives a store that is not there ─────────────────────────

def test_the_spill_catches_more_than_the_refusal():
    """`ArtifactTooLarge` was the only named exception, and the store has
    many other ways to be unavailable: no credential, no project, no
    network, a 403, a bucket that does not exist."""
    tree = ast.parse(_src("kernel/harness/loop_tool.py"))
    handlers = [h for node in ast.walk(tree)
                if isinstance(node, ast.Try)
                for h in node.handlers
                if "aput" in ast.unparse(node.body)]
    assert handlers, "the artifact spill is no longer wrapped in a try"
    caught = {ast.unparse(h.type) if h.type else "bare" for h in handlers}
    assert "Exception" in caught, (
        f"the spill catches only {caught}; any other store failure leaves "
        f"run_tool, leaves run_leg, and kills the leg")


def test_a_failed_store_falls_through_to_the_truncation_that_exists():
    """The `else` branch was already correct. It was simply unreachable
    for anything but a refusal."""
    body = _src("kernel/harness/loop_tool.py")
    i = body.index("artifact_id = await aput(raw)")
    window = body[i:i + 3000]

    assert "if artifact_id:" in window and "else:" in window, (
        "nothing distinguishes a stored output from an unstored one")
    assert "MAX_TOOL_OUTPUT_SIZE]" in window, (
        "the unstored path no longer truncates, so a leg either stores "
        "the output or carries all of it into the model's context")


def test_the_model_is_told_which_kind_of_failure_it_was():
    """"Too large to store" tells a model to run a SMALLER command. That
    is right when the artifact was refused and wrong when the store was
    unreachable -- there it would shrink a command that was fine."""
    body = _src("kernel/harness/loop_tool.py")

    assert "cut_because" in body, (
        "one message covers both failures, so an unreachable store reads "
        "to the model as its own command being too big")
    assert "unreachable" in body


def test_the_rule_is_the_one_this_file_already_states():
    """"A measurement must never cost a leg" is in the module docstring,
    applied to the orientation floor. Storing output is the same kind of
    thing: an optimisation over truncating."""
    body = _src("kernel/harness/loop_tool.py")

    assert "must never cost a leg" in body


# ── and the pod gets a store it can actually reach ─────────────────────

def test_the_pod_does_not_inherit_a_bucket_it_cannot_authenticate_to():
    """The execution pod is denied the metadata server on purpose: its
    sandbox shares the pod's network namespace, so a token minted there
    would be reachable by model-authored code."""
    body = _src("kernel/conductor/spec_builder.py")

    assert 'env["ARTIFACT_STORE"] = "filesystem"' in body, (
        "the pod still runs whatever binding the conductor runs, and the "
        "conductor's is gcs")
    assert 'env.pop(key, None)' in body and "ARTIFACT_BUCKET" in body, (
        "a bucket name the pod cannot use is left in its environment")


def test_the_pod_store_is_rooted_on_something_that_exists():
    """`store.UnknownBinding` warns about exactly the silent version of
    this: the filesystem binding defaulting to `/artifacts` inside a
    container about to be destroyed, giving "a journal full of
    result_refs pointing at nothing"."""
    body = _src("kernel/conductor/spec_builder.py")

    assert 'env["ARTIFACT_ROOT"]' in body, (
        "the pod takes the default root, which is not a mounted path")
    i = body.index('env["ARTIFACT_ROOT"]')
    assert "AGENTOS_WORKSPACE_ROOT" in body[i:i + 300], (
        "the artifact root is not derived from the workspace the pod "
        "actually has mounted")


def test_the_limit_is_written_down_rather_than_discovered():
    """Run-scoped is a real reduction from a bucket, and the next person
    to read a dangling result_ref should find it said so here."""
    body = _src("kernel/conductor/spec_builder.py")

    assert "RUN-SCOPED" in body


# ── and the executor is told not to ask for 133 KB in the first place ──

def test_the_prompt_teaches_a_narrower_first_question():
    """The model's instinct was right -- the tree was nearly empty, so it
    asked history what had been there. It picked the most expensive
    possible way to ask, and nothing had ever told it otherwise."""
    body = _src("profiles/story-coding/prompt.md")

    assert "git log --oneline" in body and "git log --stat" in body, (
        "nothing teaches the executor to ask history for a summary first")
    assert "32 KB" in body, (
        "the prompt never says output is bounded, so the model cannot "
        "know that a long command loses it information")


def test_the_profile_that_reads_logs_for_a_living_is_told_too():
    """SCOPE, and the first pass got it backwards by putting this only
    in story-coding. An errand's own opening line is "read a log and say
    what is failing" -- reading enormous files IS the job here, and this
    profile has `bash` and had no guidance at all."""
    body = _src("profiles/errand-ops/prompt.md")

    assert "32 KB" in body, (
        "errand-ops never says output is bounded, and it is the profile "
        "most likely to produce a huge one")
    assert "grep -n" in body and "tail -" in body, (
        "the advice is abstract; an errand needs the narrowing commands "
        "for a LOG, which is what it actually reads")


def test_every_profile_that_can_run_a_command_carries_the_warning():
    """The rule rather than the two instances: a surface with `bash` can
    produce a 133 KB output, and a profile that never says so leaves its
    model to find the cut by losing something."""
    import tomllib

    for toml in sorted((ROOT / "profiles").glob("*/profile.toml")):
        spec = tomllib.loads(toml.read_text(encoding="utf-8"))
        tools = set(spec.get("tools_add") or []) | set(spec.get("tools") or [])
        if "bash" not in tools:
            continue
        prompt = toml.parent / "prompt.md"
        assert "32 KB" in prompt.read_text(encoding="utf-8"), (
            f"{toml.parent.name} can run commands and is never told its "
            f"output is bounded")


def test_the_model_is_told_how_to_recover_a_cut_output():
    """E7 stores the whole thing and hands back an id. If nothing says
    so, the model re-runs the command to see the rest -- which is the
    behaviour E7 exists to remove. Said INLINE, in the output itself,
    which is the one place it cannot be missed."""
    body = _src("kernel/artifacts/bounding.py")

    assert "artifact_read" in body, (
        "a cut output does not name the tool that reads the rest")
    assert "Do NOT re-run" in body
