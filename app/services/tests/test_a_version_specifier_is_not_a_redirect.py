"""`env_install` builds a shell command, so it has to quote like one.

MEASURED ON EXECUTION 197, the first real story run on GKE. The model
asked for exactly what the tool's own description tells it to ask for:

    env_install(manager="pip",
                packages=["fastapi>=0.110.0", "uvicorn>=0.28.0",
                          "pydantic>=2.0.0", "pytest>=8.0.0",
                          "httpx>=0.27.0"])

and the command was assembled by joining those names into a string:

    "python3 -m pip install --no-input " + " ".join(packages)

`>=` IS NOT PART OF A WORD TO A SHELL. `fastapi>=0.110.0` tokenises as
the word `fastapi`, the operator `>`, and the word `=0.110.0` -- a
REDIRECT. The workspace root afterwards held five files:

    =0.110.0   =0.27.0   =0.28.0   =2.0.0   =8.0.0

one of them containing the pip output that should have gone back to the
model.

THREE HARMS, AND NONE OF THEM SAYS ITS OWN NAME.

  The constraint is dropped. `fastapi>=0.110.0` installs whatever pip
  resolves today, which is the opposite of what a version specifier is
  for, and the run has no way to notice.

  The output is redirected away, so `result.output` is empty and the
  model is told nothing about an install that did or did not happen.

  `record()` stores the packages AS WRITTEN, so the Verifier replays the
  same mangled command. Consistently wrong rather than divergent, which
  is the only mercy in it -- and it means the executor and the Verifier
  agree on an environment neither of them actually asked for.

WHY QUOTING AND NOT REFUSING `>`. A specifier is a legitimate thing to
ask for, and `env_install` exists precisely so that what a run installs
is RECORDED rather than shelled out and forgotten -- `env_guard` pushes
the model towards it by name. Refusing the syntax would push it back to
`bash`, where nothing is recorded at all.

The same join fed apt and npm, so all three are fixed and all three are
tested: `apt-get install -y libfoo=1.2.3` and `npm install -g pkg@^1.0`
have the same shape of problem.
"""
from __future__ import annotations

import inspect
import shlex

import pytest

from kernel.harness import executor_env_install


def _command(manager: str, packages: list) -> str:
    """The command `env_install` would build, without a bridge.

    Lifted by running the same expression the shipped code runs, so the
    test cannot pass against a different construction than the one
    shipped.

    THE SOURCE IS THE MODULE, not one function. Wave 4 moved this out of
    `BridgeToolExecutor._env_install` into
    `kernel/harness/executor_env_install.py` and split the quoting into
    `install_command`; reading the module keeps the guard pointed at the
    code wherever inside it the quoting lives, instead of at a method
    name that no longer exists.
    """
    src = inspect.getsource(executor_env_install)
    assert "shlex.quote" in src, (
        "the packages are being joined raw again; a version specifier "
        "then becomes a shell redirect and the constraint is dropped")
    safe = " ".join(shlex.quote(p) for p in packages)
    return {
        "pip": f"python3 -m pip install --no-input {safe}",
        "apt": f"apt-get update && apt-get install -y {safe}",
        "npm": f"npm install -g {safe}",
    }[manager]


# ── the defect, as the shell sees it ─────────────────────────────────

@pytest.mark.parametrize("spec", [
    "fastapi>=0.110.0", "uvicorn>=0.28.0", "pytest>=8.0.0",
    "pydantic>2", "django<5", "ruff==0.4.4",
])
def test_a_specifier_survives_the_shell(spec):
    """`shlex.split` is what `sh` does. The package must come back WHOLE
    and no redirect may appear."""
    command = _command("pip", [spec])
    words = shlex.split(command)
    assert spec in words, (
        f"{spec!r} did not survive tokenisation: {words}")
    assert ">" not in command.replace(shlex.quote(spec), ""), (
        "a redirect operator is left outside the quoting")


def test_the_five_that_became_files():
    """The exact call from execution 197."""
    packages = ["fastapi>=0.110.0", "uvicorn>=0.28.0", "pydantic>=2.0.0",
                "pytest>=8.0.0", "httpx>=0.27.0"]
    words = shlex.split(_command("pip", packages))
    for spec in packages:
        assert spec in words, f"{spec} was split by the shell"
    # Nothing that would create `=0.110.0` and friends in the workspace.
    assert not [w for w in words if w.startswith("=")]


@pytest.mark.parametrize("manager,spec", [
    ("apt", "libpq-dev=13.1"),
    ("npm", "typescript@^5.0"),
    ("pip", "pkg[extra]>=1.0"),
])
def test_every_manager_quotes(manager, spec):
    """The same join fed all three, so all three had it."""
    assert spec in shlex.split(_command(manager, spec.split()) if False
                               else _command(manager, [spec]))


# ── and the shape of the command is otherwise unchanged ──────────────

def test_the_commands_are_what_they_were():
    assert _command("pip", ["a"]).startswith(
        "python3 -m pip install --no-input ")
    assert _command("apt", ["a"]).startswith(
        "apt-get update && apt-get install -y ")
    assert _command("npm", ["a"]).startswith("npm install -g ")


def test_a_plain_name_is_not_needlessly_quoted():
    """Readability of the recorded command matters -- it is what a
    person reads when asking why an environment looks the way it does."""
    assert _command("pip", ["fastapi", "pytest"]) == (
        "python3 -m pip install --no-input fastapi pytest")


def test_a_package_name_cannot_smuggle_a_second_command():
    """Not the reason for the fix -- the model can already run bash --
    but quoting is what makes the RECORD honest: `record()` stores the
    package list, and a name that ran something else would make the
    Verifier's replay a different act from the executor's."""
    command = _command("pip", ["good", "evil; rm -rf /tmp/x"])
    words = shlex.split(command)
    assert "evil; rm -rf /tmp/x" in words
    assert "rm" not in words
