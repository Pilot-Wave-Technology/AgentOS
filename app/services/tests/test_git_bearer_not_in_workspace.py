"""The bearer the sandbox must not be able to read.

WHAT WENT WRONG. `_credential_helper` interpolated OPENCLAW_AUTH_TOKEN
into the helper command, and the caller persisted that command with
`git config credential.helper` -- putting the shared gateway bearer,
verbatim, into `<workspace>/<mount>/.git/config` at mode 0644. The
sandbox mounts that tree and runs as root, so model-authored code could
`cat` it. That bearer opens `/internal/git-credential`, which takes its
`story_id` from the query string and answers with an installation token
carrying `contents:write` and `workflows:write` -- for any story.

WHY THESE TESTS AND NOT AN INSPECTION. The property is not "the helper
looks right", it is "the token is not in the file". Only the second one
stays true when somebody edits the helper string, so that is what is
asserted here -- against the real config file a real clone writes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from kernel.workspace import git_bearer
from kernel.workspace.bindings import _credential_helper
from kernel.workspace.driver import WorkspaceSpec

TOKEN = "sekret-gateway-bearer-do-not-publish"
BROKER = "http://control-panel.invalid/internal/git-credential"


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """A runtime dir outside any workspace, and a token to write."""
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(git_bearer, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(git_bearer, "_written", None)
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_CREDENTIAL_URL", BROKER)
    return runtime


def _spec():
    return WorkspaceSpec(execution_id=1, root="/workspace/exec-1",
                         story_id="story-1",
                         source="https://github.com/acme/repo.git")


def test_the_helper_carries_a_path_and_never_the_token(wired):
    helper = _credential_helper(_spec())
    assert helper, "a github https source should get a helper"
    assert TOKEN not in helper, (
        "the bearer is interpolated into the helper again -- this string is "
        "persisted into .git/config, which the sandbox reads as root")
    assert git_bearer.bearer_path() in helper
    assert "$t" in helper, "the helper should read the file at run time"


def test_a_real_clone_leaves_no_token_in_git_config(wired, tmp_path):
    """The end-to-end property, against the file git actually writes."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    work = tmp_path / "workspace" / "repo"
    work.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)

    helper = _credential_helper(_spec())
    subprocess.run(["git", "config", "credential.helper", helper],
                   cwd=str(work), check=True)

    config = (work / ".git" / "config").read_text(encoding="utf-8")
    assert TOKEN not in config, (
        "the gateway bearer is on disk inside the workspace; the sandbox "
        "mounts this tree")
    # The filename, not the full path: git escapes backslashes when it
    # writes a value, so a literal Windows path never compares equal.
    # Production runs this in a Linux container where the path is
    # /run/agentos/..., but the assertion should not depend on that.
    assert git_bearer.BEARER_NAME in config, (
        "the helper no longer points at the bearer file")


def test_the_token_file_is_not_inside_the_workspace(wired, tmp_path):
    path = git_bearer.write_bearer()
    assert path is not None
    workspace_root = str(tmp_path / "workspace")
    assert git_bearer.is_outside_workspace(path, workspace_root), (
        "the bearer landed under the workspace root, which is the one "
        "directory the sandbox can read")


def test_the_token_file_is_written_0600_in_a_0700_directory(wired):
    path = git_bearer.write_bearer()
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700


def test_a_local_origin_needs_no_token_and_says_nothing(monkeypatch,
                                                        tmp_path):
    """A local corpus origin must still clone, quietly and bare.

    This test used to pass `_spec()` -- a GITHUB source -- while its
    name and docstring described a corpus one, and asserted "". It
    therefore asserted the opposite of what it claimed: that a private
    GitHub clone proceeds without a credential. It does not; it fails
    at `git push`, which is the last thing a story does. The local
    origin it was always meant to cover is below, and the GitHub case
    is the next test.
    """
    monkeypatch.setattr(git_bearer, "RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(git_bearer, "_written", None)
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CREDENTIAL_URL", BROKER)
    local = WorkspaceSpec(execution_id=1, root="/workspace/exec-1",
                          story_id="story-1",
                          source=str(tmp_path / "corpus"))
    assert _credential_helper(local) == ""


def test_a_github_source_without_a_token_refuses_up_front(monkeypatch,
                                                          tmp_path):
    """Admission, not publish. See kernel/workspace/git_bearer.py."""
    monkeypatch.setattr(git_bearer, "RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(git_bearer, "_written", None)
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CREDENTIAL_URL", BROKER)
    with pytest.raises(git_bearer.BearerUnavailable):
        _credential_helper(_spec())


def test_writing_twice_is_one_file(wired):
    first = git_bearer.write_bearer()
    second = git_bearer.write_bearer()
    assert first == second
    with tempfile.TemporaryDirectory():
        assert os.path.isfile(first)
        assert open(first, encoding="utf-8").read() == TOKEN
