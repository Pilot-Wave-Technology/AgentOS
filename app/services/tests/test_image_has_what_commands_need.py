"""Every binary a composed command names is in the image that runs it.

THE DEFECT. `_credential_helper` builds a shell function that calls
`curl`. The runtime image installed curl in the BUILDER stage only, to
fetch Deno, and the final stage copied the Deno binary across under the
comment "no installer, no tarballs, no curl". So every clone of a
private repository inside an execution pod died with

    f() { ...curl... }; f get: 1: curl: not found
    fatal: could not read Username for 'https://github.com'

WHY NO SCAN OF THE CODE WOULD HAVE FOUND IT. We do not call curl. GIT
calls curl, because we asked git to, by writing the command into
`.git/config`. A search for `subprocess.run(["curl"...])` returns
nothing and always would have: the only binaries this codebase invokes
directly are git, bash and rm.

So the category that matters is not "what does our Python execute" but
"what do the command strings we compose name, for someone else to
execute". There are four such hand-offs and they are all listed below.
Each is a string this repository builds and another program runs -- git,
or the kubelet -- which is precisely where a missing dependency is
invisible to the tooling and fatal at run time.

THE SANDBOX IS NOT CHECKED HERE, deliberately. `sandbox_command()` runs
in the CONTRACT's image, which this repository does not build and cannot
guarantee. That it needs python is real and worth knowing; asserting it
against our Dockerfile would be asserting the wrong thing about the
wrong image.
"""
from __future__ import annotations

import pathlib
import re

from kernel.drivers import k8s_bridge, k8s_workspace

REPO = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "Dockerfile"

# What debian bookworm slim (python:3.12-slim) ships without being
# asked. Listed rather than probed: the check must give the same answer
# on a developer's Windows machine as in CI, and `shutil.which` would
# answer about the wrong computer entirely.
SLIM = {
    "sh", "bash", "cat", "cp", "mv", "rm", "ls", "mkdir", "chmod", "chown",
    "tar", "gzip", "sed", "grep", "find", "env", "echo", "ln", "touch",
    "uname", "sleep", "true", "false", "python", "python3", "pip",
}

# Not programs at all. `exit` is the shell itself, and a check that
# demanded the image install it would be reporting a fake defect --
# which is the fastest way to get a check deleted.
BUILTINS = {"exit", "cd", "export", "set", "unset", "read", "eval", "test",
            "return", "shift", "trap", "wait", "local", "exec"}


def _installed_in_base() -> set:
    """Packages the BASE stage installs, which both later stages inherit."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    base = text.split("FROM base AS builder")[0]
    installs = re.findall(r"apt-get install[^\n]*((?:\n[^\n]*\\)*\n[^\n]*)",
                          base)
    names = set()
    for block in installs:
        for word in re.split(r"[\s\\]+", block):
            if word and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", word) and \
                    not word.startswith("-"):
                names.add(word)
    return names


def _binaries(command) -> set:
    """The programs a command list or shell string names."""
    if not command:
        return set()
    if command[0] in ("sh", "bash") and "-c" in command:
        script = command[command.index("-c") + 1]
        pieces = re.split(r"[;|&]+", script)
        return {p.split()[0] for p in pieces if p.split()} | {command[0]}
    return {command[0]}


def _helper_binaries(helper: str) -> set:
    """The programs a `credential.helper` string names.

    It is a shell function body, so this reads the words that start a
    command: after `{`, after `;`, and inside `$( )`.
    """
    body = helper.lstrip("!")
    pieces = re.split(r"[;{}|&]+|\$\(", body)
    out = set()
    for piece in pieces:
        words = piece.split()
        if words and not words[0].startswith(("f()", "f", "t=")):
            out.add(words[0])
        # `t=$(cat FILE)` -- the assignment is not the command
        for word in words:
            if word.startswith("t=$("):
                out.add(word[4:])
    return {w for w in out if re.fullmatch(r"[a-z][a-z0-9_-]*", w)}


def _available() -> set:
    return SLIM | BUILTINS | _installed_in_base() | {"deno", "uvicorn"}


# ── the composed commands, each named with who runs it ───────────────

def test_the_credential_helper_is_runnable_where_git_runs_it():
    """GIT runs this, inside the execution pod, which is this image."""
    import os
    import tempfile

    from kernel.workspace import git_bearer
    from kernel.workspace.bindings import _credential_helper
    from kernel.workspace.driver import WorkspaceSpec

    runtime = tempfile.mkdtemp()
    old_dir, old_written = git_bearer.RUNTIME_DIR, git_bearer._written
    old_token = os.environ.get("OPENCLAW_AUTH_TOKEN")
    old_url = os.environ.get("GIT_CREDENTIAL_URL")
    try:
        git_bearer.RUNTIME_DIR, git_bearer._written = runtime, None
        os.environ["OPENCLAW_AUTH_TOKEN"] = "t"
        os.environ["GIT_CREDENTIAL_URL"] = "http://broker.invalid/x"
        helper = _credential_helper(WorkspaceSpec(
            execution_id=1, root="/w", story_id="s1",
            source="https://github.com/acme/repo.git"))
    finally:
        git_bearer.RUNTIME_DIR, git_bearer._written = old_dir, old_written
        for key, value in (("OPENCLAW_AUTH_TOKEN", old_token),
                           ("GIT_CREDENTIAL_URL", old_url)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert helper, "no helper was built; this check would pass vacuously"
    needed = _helper_binaries(helper)
    assert "curl" in needed, (
        "the helper no longer calls curl -- if that is deliberate, this "
        "assertion should name whatever replaced it")
    missing = needed - _available()
    assert not missing, (
        f"the credential helper calls {sorted(missing)}, which the image "
        f"does not install. git runs this string inside the execution "
        f"pod, so the failure arrives as `could not read Username for "
        f"'https://github.com'` -- a credential failure wearing a "
        f"terminal's clothes.")


def test_the_init_containers_are_runnable():
    """The KUBELET runs these, in this image."""
    for command in (
        k8s_bridge.init_container("img", 65531)["command"],
        ["python", "-m", "kernel.workspace.podinit"],
        k8s_workspace.worker_command(),
    ):
        missing = _binaries(command) - _available()
        assert not missing, f"{command[:2]} needs {sorted(missing)}"


# ── the check can fail ───────────────────────────────────────────────

def test_the_dockerfile_scan_finds_the_packages():
    # A regex that matches nothing declares every binary available.
    installed = _installed_in_base()
    assert "git" in installed and "curl" in installed, installed


def test_a_binary_the_image_lacks_is_caught():
    assert _binaries(["sh", "-c", "jq . < x"]) - _available() == {"jq"}
