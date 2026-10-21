"""A green preflight on a known-bad installation is the only real failure.

WHY THIS TEST, AND WHY IT STUBS THE CLOUD.

`scripts/infra/env_preflight.sh` is the artifact the OSS release is
gated on: a wrong document plus a truthful preflight is a recoverable
first experience, and a beautiful document plus silent failures is
not. But a checker is exactly the kind of program that passes
everything, and its failure mode is silence -- a preflight that never
reports anything looks identical to an installation that is correctly
provisioned.

So the thing worth testing is not that it runs. It is that against a
deliberately broken installation it names EVERY missing resource, and
that it distinguishes "I asked and the answer is no" from "I could not
ask" -- the three-outcome rule in preflight/report.sh, which exists
because four separate defects in one build had the same shape: a
question that could not fail visibly answered "no".

`gcloud` and `kubectl` are stubbed on PATH. That keeps the test
hermetic and, more usefully, lets it manufacture the two answers a
real project almost never gives on demand: NOT_FOUND for everything,
and PERMISSION_DENIED for everything.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
PREFLIGHT = REPO / "scripts" / "infra" / "env_preflight.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="env_preflight.sh is a shell program; without bash there is "
           "nothing to run")


def _stub(dirpath: pathlib.Path, name: str, body: str) -> None:
    p = dirpath / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8",
                 newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: pathlib.Path, gcloud_body: str,
         kubectl_body: str = 'exit 1\n',
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir, "gcloud", gcloud_body)
    _stub(bin_dir, "kubectl", kubectl_body)

    env = {
        "PROJECT": "stub-project",
        "REGION": "us-central1",
        "AGENTOS_SINGLE_ENV": "1",
        # A working python must NOT be on PATH for the database section
        # here: reaching a real database is not what this test is for,
        # and `unknown` is the correct answer when the tool is absent.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        **(extra_env or {}),
    }
    # UTF-8, STRICT, NAMED. `text=True` alone decodes with the platform's
    # code page, which on Windows never fails -- so a report sliced
    # mid-character passed there and failed only on the Linux build.
    return subprocess.run(["bash", str(PREFLIGHT), "agentos"],
                          capture_output=True, encoding="utf-8",
                          errors="strict", env=env)


# Everything absent. `gcloud` answers the way it really does for a
# resource that is not there, on stderr, with the string the checker
# matches on.
ALL_MISSING = 'echo "NOT_FOUND: resource was not found" >&2\nexit 1\n'

# Everything unreadable. A DIFFERENT outcome, and the distinction is
# the whole design: the operator must not go looking for a secret that
# exists and that they simply cannot read.
ALL_DENIED = 'echo "PERMISSION_DENIED: caller lacks permission" >&2\nexit 1\n'


# The resources a single-environment install must have, by the exact
# name `names.sh` derives. Naming them here rather than deriving them
# is deliberate: if a rename in names.sh silently changed what the
# preflight asks for, deriving would hide it and this list will not.
EXPECTED_NAMES = [
    "database-url",
    "database-url-migrate",
    "database-url-conductor",
    "execution-db-password",
    "execution-sqlproxy-key",
    "openclaw-gateway-token",
    "credential-encryption-key",
    "agent-token-signing-key",
    "gemini-api-key",
    "anthropic-api-key",
    "firebase-service-account",
    "github-app-key",
    "agentos-artifacts-stub-project",
]

# Every secret READS fine, and every value is prerequisites.sh's
# one-space placeholder. The quiet case: a readable version that holds
# nothing.
ALL_BLANK = ('if [ "$1 $2 $3" = "secrets versions access" ]; then '
             'printf " "; exit 0; fi\n'
             'echo "NOT_FOUND: resource was not found" >&2\nexit 1\n')

REQUIRED_VALUES = [
    "gemini-api-key",
    "anthropic-api-key",
    "firebase-service-account",
    "github-app-key",
]


@pytest.mark.parametrize("name", REQUIRED_VALUES)
def test_a_placeholder_required_secret_is_reported_blank(tmp_path, name):
    """A whitespace placeholder is not a value.

    It passes an existence check and fails at login or on the first
    agent turn. The report must call it BLANK and say how to load it.
    """
    r = _run(tmp_path, ALL_BLANK)
    assert r.returncode != 0, r.stdout
    assert f"{name} is BLANK" in r.stdout, r.stdout
    assert f"set-secret.sh {name}" in r.stdout, r.stdout


def test_a_completely_unprovisioned_project_fails(tmp_path):
    r = _run(tmp_path, ALL_MISSING)
    assert r.returncode != 0, r.stdout


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_missing_resource_is_named(tmp_path, name):
    """By name, so the operator can act without reading the source."""
    out = _run(tmp_path, ALL_MISSING).stdout
    assert name in out, out


def test_a_missing_secret_carries_the_reason_it_matters(tmp_path):
    """The name alone is a chore; the consequence is what gets it fixed.

    `openclaw-gateway-token` is the example the whole three-outcome
    rule came from: absent, every kernel tool call returns
    E_KERNEL_UNREACHABLE, which reads as a network fault and sends the
    operator to the wrong place entirely.
    """
    out = _run(tmp_path, ALL_MISSING).stdout
    assert "E_KERNEL_UNREACHABLE" in out, out


def test_could_not_ask_is_reported_differently_from_is_not_there(tmp_path):
    """The distinction four separate defects were caused by losing.

    PERMISSION_DENIED must not render as MISS. An operator told a
    secret is missing creates a second one; an operator told it cannot
    be read fixes an IAM binding. The first of those makes the problem
    worse.
    """
    denied = _run(tmp_path, ALL_DENIED)
    missing = _run(tmp_path, ALL_MISSING)

    # The marker is `[ ?? ]` and the words are "could not determine".
    # Asserting on those rather than on the word UNKNOWN is deliberate:
    # they are what an operator actually reads, and a rewording that
    # loses them is the regression worth catching.
    assert "[ ?? ]" in denied.stdout, denied.stdout
    assert "could not determine" in denied.stdout, denied.stdout
    assert denied.returncode != 0, denied.stdout

    # And it is genuinely a different report, not the same text twice:
    # everything undetermined, nothing claimed missing.
    assert "[MISS]" not in denied.stdout, denied.stdout
    assert "[MISS]" in missing.stdout, missing.stdout
    assert "undetermined" in denied.stdout, denied.stdout


def test_a_key_the_build_may_not_read_does_not_fail_the_deploy(tmp_path):
    """The build account runs this preflight and is not allowed to read the
    Firebase or GitHub App key -- nothing in the build needs them, and
    prerequisites.sh checked their content as a person who can. Reporting
    that as `[ ?? ]` failed every deploy at the execution-plane step.
    It is shown, as not checked here, and it is not counted."""
    out = _run(tmp_path, ALL_DENIED).stdout
    for name in ("firebase-service-account", "github-app-key"):
        line = next(l for l in out.splitlines() if f"secret     {name}" in l)
        assert line.lstrip().startswith("[ -- ]"), line
        assert "prerequisites.sh checks its content" in line, line


# ── which cluster the in-cluster checks ask ─────────────────────────
#
# They used to ask whatever kubectl context was CURRENT. On a fresh
# machine that is none (four `??` about a missing login); on a machine
# left pointing at another cluster it is THE WRONG CLUSTER, answering
# `ok`. Now the preflight fetches credentials for its own cluster into a
# private kubeconfig. gcloud records the call; kubectl records which
# kubeconfig it was handed and answers "not found" for everything.
RECORDING_GCLOUD = (
    'if [ "$1 $2 $3" = "container clusters get-credentials" ]; then '
    'echo "$@" >> "$HOME/get_credentials"; exit 0; fi\n'
    'echo "NOT_FOUND: resource was not found" >&2\nexit 1\n')
RECORDING_KUBECTL = (
    'echo "${KUBECONFIG-}" >> "$HOME/kubeconfig_seen"\n'
    'echo "Error from server (NotFound): not found" >&2\nexit 1\n')


def _line(out: str, what: str) -> str:
    return next(l for l in out.splitlines() if what in l)


def test_the_in_cluster_checks_ask_the_cluster_they_are_about(tmp_path):
    r = _run(tmp_path, RECORDING_GCLOUD, RECORDING_KUBECTL)
    asked = (tmp_path / "get_credentials").read_text().split()
    assert asked[3] == "agentos", asked
    assert "--region=us-central1" in asked and "--project=stub-project" in asked
    seen = set((tmp_path / "kubeconfig_seen").read_text().split())
    assert seen and all("agentos-preflight-kube" in s for s in seen), seen
    # Bound, the namespace is a FACT -- missing -- not a login problem.
    assert _line(r.stdout, "namespace  agentos").lstrip().startswith("[MISS]")


def test_a_cluster_that_is_not_there_is_missing_not_undetermined(tmp_path):
    out = _run(tmp_path, ALL_MISSING).stdout
    for what in ("namespace  agentos", "k8s-secret conductor-secrets"):
        line = _line(out, what)
        assert line.lstrip().startswith("[MISS]"), line
        assert "does not exist" in line, line


def test_credentials_it_cannot_get_are_undetermined_with_the_reason(tmp_path):
    line = _line(_run(tmp_path, ALL_DENIED).stdout, "namespace  agentos")
    assert line.lstrip().startswith("[ ?? ]"), line
    assert "cannot get credentials" in line and "PERMISSION_DENIED" in line


def test_inside_a_pod_it_uses_the_in_cluster_account(tmp_path):
    _run(tmp_path, RECORDING_GCLOUD, RECORDING_KUBECTL,
         {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    assert not (tmp_path / "get_credentials").exists()


def test_a_missing_resource_prints_the_command_that_fixes_it(tmp_path):
    """`fix:` lines. A checker that reports without remedying is a
    checker people stop running."""
    out = _run(tmp_path, ALL_MISSING).stdout
    assert "fix:" in out, out
    assert "gcloud storage buckets create" in out, out


def test_it_asks_for_the_unsuffixed_names_in_a_single_env_install(tmp_path):
    """The bug that kept this from shipping in Phase 2.

    With the suffix applied, the preflight asks for
    `agentos-database-url` and reports a CORRECT installation as
    missing. These two assertions are what make the published wrapper
    safe to hand to a stranger.
    """
    out = _run(tmp_path, ALL_MISSING).stdout
    assert "agentos-database-url" not in out, out
    assert "database-url-agentos" not in out, out
    assert "database-url" in out, out
