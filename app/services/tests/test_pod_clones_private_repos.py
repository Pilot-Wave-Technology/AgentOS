"""A pod can authenticate a clone. Five dispatches could not.

THE FAILURE, in the order it was actually understood:

    pod exec-6 reached Failed before becoming ready
      -> workspace-compose exited 1
        -> fatal: could not read Username for 'https://github.com'

which is git asking a terminal for a password because the credential
helper was never built. `_credential_helper` returns "" unless FOUR
things hold, and on Kubernetes two of them did not:

    the source is an https github.com url        held
    a bearer file was written                    held
    GIT_CREDENTIAL_URL is set                    NO -- only E0 set it
    spec.story_id is non-empty                   NO -- never carried

Either one alone is fatal and both were silent. The first is closed by
deriving the URL (kernel/workspace/broker.py); the second by carrying
the story into the pod, because the broker scopes its token to a story
and a pod has no database to look one up in.

WHAT IS TESTED HERE IS THE PLUMBING, NOT THE CLONE. Whether GitHub
accepts the token is GitHub's business and needs a network; whether the
helper is BUILT AT ALL is ours, and that is what was broken.
"""
from __future__ import annotations

import pytest

from kernel.conductor.spec_builder import build_spec
from kernel.drivers import k8s_workspace
from kernel.spec import ExecutionSpec
from kernel.workspace import bindings, broker, podinit
from kernel.workspace.driver import WorkspaceSpec
from kernel.workspace.mounts import Mount

BROKER = "https://cp.example/internal/git-credential"
PRIVATE = "https://github.com/acme/private-thing"


# ── the URL is derived, not declared a second time ───────────────────

def test_the_broker_url_comes_from_the_control_panel(monkeypatch):
    monkeypatch.delenv("GIT_CREDENTIAL_URL", raising=False)
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example")
    assert broker.endpoint(broker.GIT_CREDENTIAL, "GIT_CREDENTIAL_URL") == BROKER


def test_a_trailing_slash_does_not_double(monkeypatch):
    monkeypatch.delenv("GIT_CREDENTIAL_URL", raising=False)
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example/")
    assert broker.endpoint(broker.GIT_CREDENTIAL, "GIT_CREDENTIAL_URL") == BROKER


def test_an_explicit_variable_still_wins(monkeypatch):
    # E0 points at host.docker.internal and is a working configuration.
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example")
    monkeypatch.setenv("GIT_CREDENTIAL_URL", "http://local:8000/x")
    assert broker.endpoint(broker.GIT_CREDENTIAL,
                           "GIT_CREDENTIAL_URL") == "http://local:8000/x"


def test_no_panel_is_empty_and_not_an_error(monkeypatch):
    # A workspace of public repos and corpus origins needs no broker.
    # A refusal here could not name the repo it was refusing for; the
    # callers that need one say so with the mount in hand.
    monkeypatch.delenv("GIT_CREDENTIAL_URL", raising=False)
    monkeypatch.delenv("CONTROL_PANEL_URL", raising=False)
    assert broker.endpoint(broker.GIT_CREDENTIAL, "GIT_CREDENTIAL_URL") == ""


# ── the story reaches the pod ────────────────────────────────────────

def test_the_spec_carries_the_story():
    spec = build_spec({"id": 1, "kind": "errand", "domain": "ops",
                       "goal_id": "G", "story_id": "f8b7487d"}, [], {})
    assert spec.story_id == "f8b7487d"


def test_a_null_story_does_not_become_the_word_none():
    # An errand proposed before its story exists has the column and a
    # NULL in it. `str(None)` would send the broker "None" and the
    # scoping lookup would fail in a way that reads like a bad grant.
    spec = build_spec({"id": 1, "kind": "errand", "domain": "ops",
                       "goal_id": "G", "story_id": None}, [], {})
    assert spec.story_id == ""


def test_the_dispatch_query_asks_for_it():
    # The pod is composed from the spec and nothing else; if the query
    # does not select the column, every other link here is dead wood.
    import inspect

    from kernel.conductor import sandbox_spec
    assert "e.story_id" in inspect.getsource(sandbox_spec.spec_for_dispatch)


def test_the_init_container_is_told():
    spec = ExecutionSpec(image="x", story_id="f8b7487d",
                         workspace_mounts=(Mount(at="repos/a", binding="git",
                                                 source=PRIVATE, ref="",
                                                 writable=True, kind="repo"),))
    container = k8s_workspace.init_container(
        spec, "img", {"name": "ws", "mountPath": "/workspace"}, 65531)
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env[k8s_workspace.STORY_ENV] == "f8b7487d"


def test_podinit_passes_it_into_the_workspace_spec(monkeypatch):
    seen = {}

    class _Composite:
        def acquire(self, spec):
            seen["story_id"] = spec.story_id
            return spec.root

    monkeypatch.setattr("kernel.workspace.composite.CompositeDriver",
                        _Composite)
    monkeypatch.setattr("kernel.workspace.object_fetch.register", lambda: None)
    podinit.compose("/tmp/podinit-check", "[]", "6", "f8b7487d")
    assert seen["story_id"] == "f8b7487d"


# ── the four conditions, together ────────────────────────────────────

@pytest.mark.parametrize("story,url,expect", [
    ("f8b7487d", True, True),
    ("", True, False),            # the gap podinit left
    ("f8b7487d", False, False),   # the gap the env var left
])
def test_the_helper_is_built_only_when_both_halves_are_present(
        monkeypatch, tmp_path, story, url, expect):
    monkeypatch.delenv("GIT_CREDENTIAL_URL", raising=False)
    if url:
        monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example")
    else:
        monkeypatch.delenv("CONTROL_PANEL_URL", raising=False)
    bearer = tmp_path / "git-broker-bearer"
    bearer.write_text("t", encoding="utf-8")
    # `required=` is passed by the caller now; the stub takes it and
    # ignores it, because this test is about the OTHER two halves.
    monkeypatch.setattr(bindings, "write_bearer",
                        lambda required=False: str(bearer))

    helper = bindings._credential_helper(
        WorkspaceSpec(execution_id=1, root="/w", source=PRIVATE,
                      story_id=story))
    assert bool(helper) is expect
    if expect:
        # The bearer travels as a PATH, never a value -- this string is
        # persisted into .git/config, which the sandbox can read.
        assert str(bearer) in helper and "story_id=f8b7487d" in helper
