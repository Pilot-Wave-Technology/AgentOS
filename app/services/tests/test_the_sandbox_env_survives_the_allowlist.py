"""What the pod sets on the sandbox is what the shell gets.

TWO FIXES THAT LOOKED APPLIED AND CHANGED NOTHING.

`k8s_spec.pod_manifest` sets the sandbox container's environment, and
`tools_core._safe_env` decides what a `bash` tool call actually receives
-- an ALLOWLIST, written to stop a leaked credential being forwarded.
Two variables were added to the container and never added to the
allowlist, so both were silently dropped:

    HTTP_PROXY / HTTPS_PROXY / NO_PROXY
        The namespace is default-deny egress and the only route out is
        the squid proxy. Without these `pip install` does not fail, it
        HANGS -- measured at twenty minutes on NCAL5 run 193, holding
        the bridge's tool lock until every later call timed out and the
        executor reported the bridge as unresponsive and parked.

    GIT_CONFIG_COUNT / KEY_0 / VALUE_0
        safe.directory. The init container clones as the worker's uid
        and the sandbox runs as 65532, so without it every `git` command
        in the checkout dies of "dubious ownership".

AND THE VERIFICATION WAS DONE THE WRONG WAY. `git status` was checked by
hand with `kubectl exec`, which inherits the container's environment
directly and never passes through `_safe_env` at all. It worked there
and would have failed for the model. A check that does not travel the
path the subject travels is not a check.

So this test compares the two lists directly. A variable added to the
sandbox container from now on either reaches the shell or fails here
with its own name in the message.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import re
import sys

# `tools_core` is written to run INSIDE the sandbox, where the shim puts
# `kernel/bridge` on sys.path and its siblings import bare (`import
# errors`). Reproduce that here rather than rewrite the module for the
# test -- the module under test should be the one that ships.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]
                       / "kernel" / "bridge"))

from kernel.bridge.tools_core import _safe_env       # noqa: E402
from kernel.drivers import k8s_spec                  # noqa: E402

# Set on the container for the CONTAINER's own sake rather than for a
# shell: the bridge process reads these itself before any tool runs.
_NOT_FOR_THE_SHELL: frozenset = frozenset()


def _sandbox_env_names() -> set:
    """Every env key `pod_manifest` puts on the sandbox container."""
    src = inspect.getsource(k8s_spec)
    block = src[src.index('"name": SANDBOX,'):]
    block = block[:block.index('"securityContext"')]
    names = set(re.findall(r'\{"name": "([A-Za-z_][A-Za-z0-9_]*)"', block))
    names |= {e["name"] for e in k8s_spec._proxy_env("http://p:3128")}
    return names


def _allowlist() -> set:
    src = inspect.getsource(_safe_env)
    body = src[src.index("if k in ("):]
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', body))


def test_every_variable_the_sandbox_is_given_reaches_the_shell():
    """THE GUARD. Both of today's fixes failed exactly here."""
    missing = sorted(_sandbox_env_names() - _allowlist() - _NOT_FOR_THE_SHELL)
    assert not missing, (
        f"these are set on the sandbox container and dropped by "
        f"_safe_env, so a tool call never sees them: {missing}. Add them "
        f"to the allowlist if they are not credentials, or to "
        f"_NOT_FOR_THE_SHELL with a reason if the shell must not have them.")


def test_the_proxy_reaches_a_shell(monkeypatch):
    """Behaviour, not spelling: without this, pip hangs rather than fails."""
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    env = _safe_env()
    assert env.get("HTTP_PROXY") == "http://egress-proxy:3128"
    assert env.get("HTTPS_PROXY") == "http://egress-proxy:3128"
    assert env.get("NO_PROXY") == "localhost"


def test_git_safe_directory_reaches_a_shell(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    env = _safe_env()
    assert env.get("GIT_CONFIG_KEY_0") == "safe.directory"
    assert env.get("GIT_CONFIG_VALUE_0") == "*"


def test_the_allowlist_is_still_an_allowlist(monkeypatch):
    """It exists to stop a leaked credential being forwarded, and
    widening it for a proxy address must not widen it for a token."""
    for name in ("DATABASE_URL", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                 "OPENCLAW_AUTH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_KEY"):
        monkeypatch.setenv(name, "leaked")
    env = _safe_env()
    assert not [k for k in env if k in (
        "DATABASE_URL", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
        "OPENCLAW_AUTH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_KEY")]
