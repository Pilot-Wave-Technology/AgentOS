"""A pod that ADOPTS its workspace must still write the git bearer.

WHAT WENT WRONG, ON NEXT, 2026-10-06. `GitDriver.acquire` has two
paths. The clone path builds a credential helper, and building one is
the only thing that ever called `write_bearer`. The adopt path -- taken
whenever the workspace is already on disk, which is every resume after
a lost sandbox -- returned the path immediately.

So a resumed pod came up with `credential.helper` already persisted in
`.git/config`, pointing at a bearer file nothing had written. The helper
is a POINTER by design (see `git_bearer`'s docstring), which is what
makes this failure silent: the config looks correct, and only the read
of the missing file fails, inside curl, inside git.

AND IT FAILS AT THE END. Execution 407 cloned fine (public remote),
edited, committed and passed its checks across eleven park/resume
generations, and discovered only at `git push` that it had no
credential -- by which time the model had started hunting the
filesystem for one.

The second half of the fix is here too: `write_bearer(required=True)`
raises rather than returning None, so a pod that cannot publish refuses
its workspace instead of accepting it and finding out last.
"""

from __future__ import annotations

import os

import pytest

from kernel.workspace import git_bearer
from kernel.workspace.bindings import GitDriver
from kernel.workspace.composite import CompositeDriver
from kernel.workspace.driver import WorkspaceSpec
from kernel.workspace.mounts import Mount

TOKEN = "sekret-gateway-bearer-do-not-publish"
GITHUB = "https://github.com/acme/repo.git"


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    rt = tmp_path / "runtime"
    monkeypatch.setattr(git_bearer, "RUNTIME_DIR", str(rt))
    monkeypatch.setattr(git_bearer, "_written", None)
    monkeypatch.setenv("GIT_CREDENTIAL_URL",
                       "http://control-panel.invalid/internal/git-credential")
    return rt


def _already_cloned(tmp_path, source=GITHUB):
    """A workspace with a `.git` in it -- what a resume finds."""
    root = tmp_path / "workspace" / "repos" / "acme__repo"
    (root / ".git").mkdir(parents=True)
    return WorkspaceSpec(execution_id=407, root=str(root),
                         story_id="story-407", source=source)


def test_adopting_a_workspace_writes_the_bearer(runtime, monkeypatch,
                                                tmp_path):
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", TOKEN)
    spec = _already_cloned(tmp_path)

    assert GitDriver().acquire(spec) == spec.root

    path = git_bearer.bearer_path()
    assert os.path.isfile(path), (
        "the adopt path returned without writing the bearer -- every "
        "resumed pod is then unable to fetch or push, and only finds "
        "out at publish")
    assert open(path, encoding="utf-8").read() == TOKEN


def test_adopting_refuses_when_the_token_is_missing(runtime, monkeypatch,
                                                    tmp_path):
    """Refuse at admission, not at publish."""
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    spec = _already_cloned(tmp_path)

    with pytest.raises(git_bearer.BearerUnavailable) as exc:
        GitDriver().acquire(spec)
    assert "OPENCLAW_AUTH_TOKEN" in str(exc.value), (
        "the refusal should name the variable and where it comes from; "
        "this message is what someone reads at 2am")


def test_a_local_origin_adopts_without_a_bearer(runtime, monkeypatch,
                                                tmp_path):
    """The quiet case stays quiet: no remote to authenticate to."""
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    spec = _already_cloned(tmp_path, source=str(tmp_path / "corpus"))

    assert GitDriver().acquire(spec) == spec.root
    assert not os.path.exists(git_bearer.bearer_path())


def test_write_bearer_separates_not_needed_from_needed_and_absent(
        runtime, monkeypatch):
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    assert git_bearer.write_bearer() is None
    with pytest.raises(git_bearer.BearerUnavailable):
        git_bearer.write_bearer(required=True)


# -- the multi-repo shape, which is where this gets interesting -------


def _adopted(root, at, source):
    """A mount already on disk, as a resumed pod finds it."""
    (root / at / ".git").mkdir(parents=True)
    return Mount(at=at, binding="git", source=source, ref="feat/x",
                 writable=True, kind="repo")


def _composite_spec(tmp_path, *mounts):
    return WorkspaceSpec(execution_id=407, root=str(tmp_path / "ws"),
                         story_id="story-407", mounts=tuple(mounts))


def test_a_later_github_mount_still_gets_the_bearer(runtime, monkeypatch,
                                                    tmp_path):
    """ORDER MUST NOT DECIDE IT.

    `write_bearer` short-circuits on a module global, and a corpus
    mount returns before setting it. If the check that decides whether
    to write lived on that global rather than on each mount source, a
    workspace whose FIRST repo is local would leave every github repo
    after it without a credential -- a failure that depends on mount
    order, which is the kind nobody reproduces.
    """
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", TOKEN)
    root = tmp_path / "ws"
    spec = _composite_spec(
        tmp_path,
        _adopted(root, "repos/local__corpus", str(tmp_path / "corpus")),
        _adopted(root, "repos/acme__app", GITHUB),
    )

    CompositeDriver().acquire(spec)

    assert os.path.isfile(git_bearer.bearer_path()), (
        "the second mount is a private github repo and the pod holds no "
        "credential for it")


def test_every_adopted_github_mount_is_covered(runtime, monkeypatch,
                                               tmp_path):
    """Three repos and two lanes of one of them, all adopted."""
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", TOKEN)
    root = tmp_path / "ws"
    spec = _composite_spec(
        tmp_path,
        _adopted(root, "repos/acme__app", GITHUB),
        _adopted(root, "repos/acme__app@release", GITHUB),
        _adopted(root, "repos/acme__lib",
                 "https://github.com/acme/lib.git"),
    )

    CompositeDriver().acquire(spec)
    assert open(git_bearer.bearer_path(), encoding="utf-8").read() == TOKEN


def test_the_refusal_names_the_mount_it_happened_on(runtime, monkeypatch,
                                                    tmp_path, caplog):
    """A multi-repo workspace saying only "no credential" says nothing.

    `BearerUnavailable` is a `WorkspaceError` so `CompositeDriver`
    logs WHICH mount refused before re-raising.
    """
    monkeypatch.delenv("OPENCLAW_AUTH_TOKEN", raising=False)
    root = tmp_path / "ws"
    spec = _composite_spec(
        tmp_path,
        _adopted(root, "repos/local__corpus", str(tmp_path / "corpus")),
        _adopted(root, "repos/acme__app", GITHUB),
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(git_bearer.BearerUnavailable):
            CompositeDriver().acquire(spec)
    assert "acme__app" in caplog.text
