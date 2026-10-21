"""What a pod manifest needs from the environment, supplied once.

WHY THIS EXISTS. `pod_manifest` now derives a per-execution workspace
token (kernel/workspace/podfs_auth.py) from AGENT_TOKEN_SIGNING_KEY, and
refuses rather than inventing one when the key is absent. That refusal is
correct -- a pod built without it comes up 2/2 and then fails every
workspace call, which is a hung execution rather than a named missing
secret -- but it means every test that builds a manifest for a spec with
an execution id needs a key.

ONE DECLARATION, NOT SIX. The alternative was an autouse fixture copied
into test_k8s_spec, test_pod_bridge, test_pod_workspace_compose,
test_pod_workspace_is_reachable, test_execution_sizes and
test_failure_survives_the_pod. Six copies of one fact is the bug class
this whole refactor keeps finding; the seventh file to build a manifest
would have been the one that forgot.

IT IS NOT A SECRET AND IT IS NOT A DEFAULT. The value is visibly fake and
lives only in the test process. Nothing in `kernel/` reads a default for
this key -- `derive()` raises TokenMissing -- and that behaviour has its
own test, which deletes the variable rather than relying on its absence
here. If this fixture ever starts hiding a real gap, that is the test
that will say so.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Obviously fake, and the same on every run so a derived token is stable
# across a test session (some tests assert two call sites agree).
TEST_SIGNING_KEY = "test-signing-key-not-a-secret"


@pytest.fixture(autouse=True)
def _a_signing_key_for_derived_tokens(monkeypatch):
    """The conductor has one from `conductor-secrets`; tests need one too.

    `monkeypatch` so it is undone per test, and so a test that wants to
    prove the refusal can `monkeypatch.delenv` it back out.
    """
    monkeypatch.setenv("AGENT_TOKEN_SIGNING_KEY", TEST_SIGNING_KEY)
