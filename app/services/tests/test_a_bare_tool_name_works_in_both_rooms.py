"""`pytest`, not `.venv/bin/pytest` — and now it is true.

THE FULL ROOT CAUSE OF EXECUTION 219, which took three passes to reach.

`setups.REGISTRY` declares, per manifest, the directories whose
executables a check may call bare, and says so:

    `bins` are the in-tree directories whose executables the checks may
    call bare (`pytest`, not `.venv/bin/pytest`).

ONE ROOM HONOURED IT. The Verifier turns `bins` into
`mounts_env[at]["prepend"]` and `criterion_env` puts the target mount's
first. The executor discovered the same setups through the same
registry -- `envsetup.py` imports `from_dir`/`from_pod` from it -- ran
their commands, and threw the bins away: `_tool_bash` has one fixed
`_safe_env()`.

AND THE PROMPT TAUGHT THE FORM THAT ONLY WORKED THERE. story-coding's
own worked example read

    right:  cd repos/owner__name && .venv/bin/pytest tests

so the model did exactly as instructed, the check passed in its sandbox
where the install lands in the tree, and failed in the verification pod
where the environment lives on the volume so that it survives the tree
being replaced between rounds:

    `cd repos/... && .venv/bin/pytest test_app.py`: command not found

Three rounds of `error` and the claim returned to the executor to fix
work that was never broken.

THREE PARTS HAD TO AGREE and none of them could be fixed alone. The
sandbox has to resolve the bare name; the prompt has to ask for it; and
the Verifier already required it. Telling the model to write `pytest`
while `pytest` did not resolve would have been asking for a command it
could not test.
"""
from __future__ import annotations

import pathlib

from kernel.worker import leg_env

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _prompt(name: str) -> str:
    return (ROOT / "profiles" / name / "prompt.md").read_text(
        encoding="utf-8")


# ── the sandbox resolves it ────────────────────────────────────────────

def test_the_registrys_bins_become_a_sandbox_path():
    got = leg_env.from_setups([{"at": "repos/a", "bins": (".venv/bin",)}])

    assert got["repos/a"]["prepend"] == ["repos/a/.venv/bin"]


def test_the_target_mount_goes_first():
    """`criterion_env`'s rule, reused rather than re-derived: one global
    PATH let the first repository's `.venv` shadow the second's, and the
    `cd <mount> &&` prefix names which is meant."""
    env = leg_env.from_setups([
        {"at": "repos/a", "bins": (".venv/bin",)},
        {"at": "repos/b", "bins": ("node_modules/.bin",)}])

    a = leg_env.with_mount_path("cd repos/a && pytest", env)
    b = leg_env.with_mount_path("cd repos/b && vitest run", env)

    assert a.index("repos/a/.venv/bin") < a.index("repos/b/node_modules")
    assert b.index("repos/b/node_modules") < b.index("repos/a/.venv/bin")


def test_siblings_stay_on_the_path_behind_it():
    """A repository with no environment of its own may legitimately be
    served by a sibling's -- `criterion_env` says so."""
    env = leg_env.from_setups([
        {"at": "repos/a", "bins": (".venv/bin",)},
        {"at": "repos/b", "bins": ("node_modules/.bin",)}])

    got = leg_env.with_mount_path("cd repos/a && pytest", env)

    assert "repos/b/node_modules/.bin" in got


def test_the_prepends_are_absolute_against_the_workspace_root():
    """The bins are workspace-relative and the command has just `cd`-ed
    into the mount, so a relative PATH entry would resolve one level too
    deep."""
    env = leg_env.from_setups([{"at": "repos/a", "bins": (".venv/bin",)}])

    got = leg_env.with_mount_path("cd repos/a && pytest", env)

    assert "AGENTOS_WORKSPACE_ROOT" in got
    assert "/repos/a/.venv/bin" in got


def test_nothing_discovered_changes_nothing():
    """A workspace with no committed setup gets the command it always
    got. This buys a PATH entry; it must never be the thing that breaks
    a shell."""
    assert leg_env.with_mount_path("echo hi", {}) == "echo hi"
    assert leg_env.with_mount_path("", {"repos/a": {"prepend": ["x"]}}) == ""


def test_the_executor_applies_it_to_bash():
    """Worker-side, because the shim runs INSIDE the sandbox and has no
    idea what a mount is."""
    import ast
    import inspect

    from kernel.harness import executor

    src = inspect.getsource(executor)
    assert "with_mount_path" in ast.unparse(ast.parse(src)), (
        "the executor no longer puts the mount's tools on PATH, so a "
        "bare tool name stops resolving in the sandbox and the model "
        "goes back to writing `.venv/bin/pytest`")


# ── the prompt asks for it ─────────────────────────────────────────────

def test_story_coding_teaches_the_portable_form():
    """It taught the other one, in a worked example labelled `right:`.
    That is why the model wrote it."""
    text = _prompt("story-coding")

    assert "right:  cd repos/owner__name && pytest tests" in text
    assert "right:  cd repos/owner__name && .venv/bin/pytest" not in text, (
        "story-coding's worked example teaches the sandbox-only form "
        "again -- the model will write it, and verification will not "
        "find it")


def test_no_profile_offers_an_env_relative_binary_as_the_example():
    """The rule, across every profile that authors checks. Prose may
    still MENTION `.venv/bin/pytest` -- the story-coding text explains
    at length why not to write it -- so this pins the instruction, not
    the string."""
    for name in ("story-coding", "errand-ops"):
        text = _prompt(name)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("right:"):
                continue
            assert ".venv/bin" not in stripped, (
                f"{name} offers `{stripped}` as the right form")
            assert "node_modules/.bin" not in stripped, (
                f"{name} offers `{stripped}` as the right form")


def test_the_cd_prefix_rule_survived():
    """The half the prompt already had right, and the half that makes
    the bare name unambiguous. Losing it while fixing the other would
    trade one silent misroute for another."""
    text = _prompt("story-coding")

    assert "cd <mount> && " in text
    assert "PATH" in text


def test_the_assistant_still_writes_prose_and_never_a_command():
    """The separation that keeps this from recurring one level up: the
    assistant cannot see the tree, so it says what done MEANS and the
    executor -- which can -- writes the runnable form."""
    text = (ROOT / "kernel" / "assistant" / "AGENTS.md").read_text(
        encoding="utf-8")

    assert "Never a shell command" in text
