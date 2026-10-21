"""The environment replay installs packages, not characters.

MEASURED ON EXECUTION 202, and it had broken every verification on this
substrate. `env_deltas.packages` is a jsonb column and `record()` writes
it with `json.dumps`. Read back through a plain `fetch`, asyncpg hands it
over as a STRING -- and `list.extend` on a string extends by CHARACTER.

So the replay asked pip to install:

    ['[', '"', 'f', 'a', 's', 't', 'a', 'p', 'i', '>', '=', '0', ...]

and pip answered exactly as it should:

    ERROR: Invalid requirement: '[': Expected package name at the start
    of dependency specifier

The verdict that reached a person was "the executor's recorded
environment could not be reproduced" -- true, and silent about a JSON
string nobody parsed. Four rounds of diagnosis went into manifests,
shared volumes, Job semantics and bucket IAM before the Job was kept
alive long enough to print this.

AND THE SECOND FAULT IN THE SAME JOB: the capture sidecar could not
reach the metadata server, so `storage.Client()` could not determine its
project and stored nothing --

    capture: OSError: Project was not passed and could not be determined
             from the environment.

Either one alone was enough to fail every replay, which is why fixing
one would have proved nothing. Both are covered here.
"""
from __future__ import annotations

import json

import pytest

from kernel.verifier.environment import _packages_of, _pip_packages


def _delta(packages, manager="pip", reproducible=True):
    return {"manager": manager, "reproducible": reproducible,
            "packages": packages}


# ── the defect ───────────────────────────────────────────────────────

def test_a_json_string_is_parsed_not_iterated():
    """THE BUG. jsonb comes back as text through a plain fetch."""
    raw = json.dumps(["fastapi>=0.115.0", "uvicorn>=0.30.0"])
    assert _pip_packages([_delta(raw)]) == ["fastapi>=0.115.0",
                                            "uvicorn>=0.30.0"]


def test_the_exact_list_from_execution_202():
    raw = json.dumps(["fastapi>=0.115.0", "uvicorn>=0.30.0",
                      "pydantic>=2.0.0", "pytest>=8.0.0", "httpx>=0.27.0"])
    got = _pip_packages([_delta(raw)])
    assert len(got) == 5, f"iterated by character again: {got[:6]}"
    assert all(len(p) > 1 for p in got)
    assert not any(p in "[]\", " for p in got), (
        "punctuation in the package list means the string was iterated")


def test_a_real_list_still_works():
    """asyncpg with a jsonb codec registered hands back a list, and the
    unit tests have always passed one. Both shapes must work or the fix
    trades one environment's bug for another's."""
    assert _pip_packages([_delta(["fastapi", "pytest"])]) == ["fastapi",
                                                              "pytest"]


@pytest.mark.parametrize("raw", ["[]", "", "   ", None, []])
def test_nothing_is_nothing(raw):
    assert _packages_of(_delta(raw)) == []


def test_a_bare_name_that_is_not_json_is_still_a_package():
    """Defensive: a row written before the column was jsonb."""
    assert _packages_of(_delta("fastapi")) == ["fastapi"]


def test_only_reproducible_pip_deltas_are_replayed():
    """apt and npm mutate the image and cannot be carried from a clean
    base -- `build_deps` counts them as skipped, and replaying them
    would claim an environment the Verifier does not have."""
    deltas = [_delta(json.dumps(["fastapi"])),
              _delta(json.dumps(["curl"]), manager="apt"),
              _delta(json.dumps(["ruff"]), reproducible=False)]
    assert _pip_packages(deltas) == ["fastapi"]


# ── and the identity the capture sidecar needs ───────────────────────

def test_the_metadata_server_never_goes_through_the_proxy():
    """`storage.Client()` takes no arguments ON PURPOSE -- gcs.py argues
    that a caller passing explicit credentials carries a secret the
    kernel deliberately does not. The ambient identity IS the design, and
    it comes from 169.254.169.254. Route that through squid and the SDK
    reports "Project was not passed", three layers from the cause."""
    from kernel.drivers.k8s_egress import NO_PROXY

    assert "169.254.169.254" in NO_PROXY
    assert "metadata.google.internal" in NO_PROXY


def test_every_container_gets_the_same_skip_list():
    """Three spellings existed, they disagreed on `::1`, the cluster
    suffix and the pod CIDR, and all three omitted the metadata server.
    Nobody could say which was right -- which is the tell."""
    from kernel.drivers.k8s_egress import NO_PROXY, proxy_env
    from kernel.drivers.k8s_spec import _proxy_env
    from kernel.drivers.k8s_workspace import proxy_env as workspace_env
    from kernel.drivers.k8s_ephemeral import fetch_env
    from kernel.drivers.k8s_egress import VIA_PROXY

    for build in (_proxy_env, workspace_env, proxy_env):
        env = {e["name"]: e["value"] for e in build("http://p:3128")}
        assert env["NO_PROXY"] == NO_PROXY, f"{build.__module__} differs"
        assert env["no_proxy"] == NO_PROXY


def test_an_absent_proxy_sets_nothing():
    """E0 and the fixtures have no proxy and need no skip list."""
    from kernel.drivers.k8s_egress import proxy_env

    assert proxy_env("") == []


def test_in_cluster_traffic_is_still_excluded():
    """The conductor reaching a pod's bridge, and podfs answering, are
    in-cluster: the pod CIDR belongs here and only one of the three old
    lists had it."""
    from kernel.drivers.k8s_egress import NO_PROXY

    for name in ("localhost", "127.0.0.1", ".svc", ".cluster.local",
                 "10.0.0.0/8"):
        assert name in NO_PROXY
