"""`names.sh` must keep naming our environments exactly as it does today.

WHY THIS TEST EXISTS BEFORE THE CHANGE IT GUARDS.

`scripts/infra/names.sh` is the single derivation for every name the
execution plane is addressed by -- the namespace, the database, the
artifact bucket, four service accounts, the sandbox repository and the
Cloud SQL instance. Thirteen files source it.

Phase 3.0 of the OSS release teaches it a second shape: a
single-environment install, whose resources carry no environment
suffix because there is no second environment to tell them apart from.
That is a change to the one file where a mistake is invisible and
expensive -- two scripts quietly agreeing on a name that nothing
created, which surfaces as a Job whose init container exited 1.

So the safety property is pinned here first, and it is absolute: with
`AGENTOS_SINGLE_ENV` unset, every name must be byte-identical to what
it is today, for every environment we run. Anything else is a change
to live infrastructure disguised as a refactor.

The expected values below were read from the running file on
2026-10-19, not reasoned about.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
NAMES = REPO / "scripts" / "infra" / "names.sh"

# THE VARIABLES WORTH PINNING: every one that names a resource somebody
# has to have created. `KSA`/`COND_KSA`/`VERIFY_KSA`/`CAPTURE_KSA` and
# the two role ids are namespaced or project-wide constants that carry
# no environment, so a suffix change cannot reach them -- they are
# included anyway, because "cannot reach them" is exactly the kind of
# claim this test is here to stop being an assumption.
PINNED = (
    "ENV_NAME", "NAMESPACE", "CLUSTER",
    "KSA", "COND_KSA", "VERIFY_KSA", "CAPTURE_KSA", "VERIFIER_CONTROL_KSA",
    "GSA", "COND_GSA", "VERIFY_GSA", "CAPTURE_GSA",
    "ROLE_READ", "ROLE_WRITE",
    "ARTIFACT_BUCKET", "SANDBOX_BASE_REPO",
    "DATABASE", "CLOUDSQL_INSTANCE",
)

PROJECT = "test-project-123"
REGION = "us-central1"

EXPECTED = {
    "next": {
        "ENV_NAME": "next",
        "NAMESPACE": "agentos-staging",
        "CLUSTER": "agentos",
        "KSA": "agentos-execution",
        "COND_KSA": "agentos-conductor",
        "VERIFY_KSA": "agentos-verifier",
        "CAPTURE_KSA": "agentos-capture",
        "VERIFIER_CONTROL_KSA": "agentos-verifier-control",
        "GSA": f"agentos-exec-next@{PROJECT}.iam.gserviceaccount.com",
        "COND_GSA": f"agentos-cond-next@{PROJECT}.iam.gserviceaccount.com",
        "VERIFY_GSA": f"agentos-verify-next@{PROJECT}.iam.gserviceaccount.com",
        "CAPTURE_GSA": f"agentos-capture-next@{PROJECT}.iam.gserviceaccount.com",
        "ROLE_READ": "agentosArtifactRead",
        "ROLE_WRITE": "agentosArtifactWrite",
        "ARTIFACT_BUCKET": "agentos-staging-artifacts",
        "SANDBOX_BASE_REPO":
            f"{REGION}-docker.pkg.dev/{PROJECT}/agentos-staging/sandbox-base",
        "DATABASE": "control_panel_next",
        "CLOUDSQL_INSTANCE": f"{PROJECT}:us-central1:control-panel-db",
    },
}
for _env in ("dev", "feature"):
    EXPECTED[_env] = {
        k: v.replace("next", _env) if "next" in v else v
        for k, v in EXPECTED["next"].items()
    }


def _names(env: str, extra_env: dict | None = None) -> dict[str, str]:
    """Source names.sh in a real shell and read the variables back.

    Sourcing it rather than re-implementing it is the whole point: a
    Python copy of the derivation would be a second place for the two
    to disagree, which is the defect names.sh was written to remove.
    """
    script = (
        f'. "{NAMES.as_posix()}"\n'
        f'agentos_names {env} || exit 1\n'
        + "".join(f'printf "%s=%s\\n" {v} "${v}"\n' for v in PINNED)
    )
    env_vars = {"PROJECT": PROJECT, "REGION": REGION, "PATH": "/usr/bin:/bin"}
    env_vars.update(extra_env or {})
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, env=env_vars)
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if line)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="names.sh is a shell file; without bash there is nothing to read")


@pytest.mark.parametrize("env", sorted(EXPECTED))
def test_our_environments_are_named_exactly_as_they_are_today(env):
    """Byte-identical, every variable, for every environment we run."""
    assert _names(env) == EXPECTED[env]


def test_a_bad_environment_name_is_still_refused():
    """The grammar check is the reason the file takes an argument at all.

    A name becomes a Kubernetes namespace, a Postgres database name, a
    GCP secret suffix and part of a service-account address, and the
    intersection of those four grammars is lowercase, digits and
    hyphens. A name that violates it fails somewhere downstream with a
    message about none of this.
    """
    script = f'. "{NAMES.as_posix()}"\nagentos_names "Bad_Name"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True,
                         env={"PROJECT": PROJECT, "PATH": "/usr/bin:/bin"})
    assert out.returncode != 0
    assert "lowercase" in out.stderr


def test_every_pinned_variable_is_actually_set():
    """A typo in PINNED would make this test pass by comparing nothing.

    `printf` on an unset variable prints an empty string rather than
    failing, so a misspelled name here would quietly pin "" to "" for
    every environment and the whole file above would assert nothing.
    """
    got = _names("next")
    assert set(got) == set(PINNED)
    assert all(v for v in got.values()), got


# ── the second shape ───────────────────────────────────────────────────
#
# These are not arbitrary: every value is what the PUBLISHED
# `prerequisites.sh` actually creates. That is the whole point of the
# single-environment shape, and asserting it here is what stops the two
# halves drifting apart again -- the previous state of the world was
# that nothing compared them and the public install simply had none of
# the resources the execution plane asked for.
SINGLE = {
    "ENV_NAME": "agentos",
    "NAMESPACE": "agentos",
    "CLUSTER": "agentos",
    "KSA": "agentos-execution",
    "COND_KSA": "agentos-conductor",
    "VERIFY_KSA": "agentos-verifier",
    "CAPTURE_KSA": "agentos-capture",
    "VERIFIER_CONTROL_KSA": "agentos-verifier-control",
    "GSA": f"agentos-exec@{PROJECT}.iam.gserviceaccount.com",
    "COND_GSA": f"agentos-cond@{PROJECT}.iam.gserviceaccount.com",
    "VERIFY_GSA": f"agentos-verify@{PROJECT}.iam.gserviceaccount.com",
    "CAPTURE_GSA": f"agentos-capture@{PROJECT}.iam.gserviceaccount.com",
    "ROLE_READ": "agentosArtifactRead",
    "ROLE_WRITE": "agentosArtifactWrite",
    "ARTIFACT_BUCKET": f"agentos-artifacts-{PROJECT}",
    "SANDBOX_BASE_REPO":
        f"{REGION}-docker.pkg.dev/{PROJECT}/agentos/sandbox-base",
    "DATABASE": "control_panel",
    "CLOUDSQL_INSTANCE": f"{PROJECT}:us-central1:agentos-db",
}


def test_a_single_environment_install_carries_no_suffix():
    assert _names("agentos", {"AGENTOS_SINGLE_ENV": "1"}) == SINGLE


def test_the_database_matches_what_prerequisites_creates():
    """`control_panel` and `<prefix>-db`, which is what the public
    `prerequisites.sh` makes. These two lines are the ones that decide
    whether the conductor can open a connection at all."""
    got = _names("agentos", {"AGENTOS_SINGLE_ENV": "1"})
    assert got["DATABASE"] == "control_panel"
    assert got["CLOUDSQL_INSTANCE"].endswith(":agentos-db")


def test_the_artifact_bucket_is_project_qualified_when_unsuffixed():
    """A bucket name is global across all of Google Cloud.

    `agentos-artifacts` is a name a stranger may already own, and
    creating it fails with a 409 whose message does not say the owner
    is someone else -- so it reads as a permissions problem on your own
    project. The multi-environment names get away without the project
    only because they happened to be free.
    """
    got = _names("agentos", {"AGENTOS_SINGLE_ENV": "1"})
    assert got["ARTIFACT_BUCKET"] == f"agentos-artifacts-{PROJECT}"
    assert PROJECT in got["ARTIFACT_BUCKET"]


def test_the_sql_instance_can_be_overridden_in_either_shape():
    """Neither side should have to patch names.sh to name its instance."""
    for extra in ({"AGENTOS_SQL_INSTANCE_NAME": "my-sql"},
                  {"AGENTOS_SQL_INSTANCE_NAME": "my-sql",
                   "AGENTOS_SINGLE_ENV": "1"}):
        got = _names("next", extra)
        assert got["CLOUDSQL_INSTANCE"] == f"{PROJECT}:us-central1:my-sql"


def _secret(base: str, extra_env: dict | None = None) -> str:
    script = (f'. "{NAMES.as_posix()}"\n'
              f'agentos_names next || exit 1\n'
              f'agentos_secret {base}\n')
    env_vars = {"PROJECT": PROJECT, "PATH": "/usr/bin:/bin"}
    env_vars.update(extra_env or {})
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, env=env_vars)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.parametrize("base,expected", [
    # The DSN secrets are PREFIXED and the key secrets are SUFFIXED.
    # That is how they were created and renaming live secrets is not
    # this change; the point is that the inconsistency now lives in one
    # function instead of being reproduced by every caller.
    ("database-url", "next-database-url"),
    ("database-url-migrate", "next-database-url-migrate"),
    ("gemini-api-key", "gemini-api-key-next"),
    ("credential-encryption-key", "credential-encryption-key-next"),
    # `anthropic-api-key` is deliberately absent here: it goes through
    # the legacy map and is covered by EVERY_SECRET_NEXT below, which
    # asserts the name that actually exists rather than the one the
    # suffix rule alone would produce.
])
def test_secret_names_match_the_ones_that_exist(base, expected):
    assert _secret(base) == expected


@pytest.mark.parametrize("base", [
    "database-url", "database-url-migrate", "gemini-api-key",
    "anthropic-api-key", "credential-encryption-key",
])
def test_a_single_environment_secret_is_just_its_name(base):
    """Which is exactly what the published prerequisites.sh creates."""
    assert _secret(base, {"AGENTOS_SINGLE_ENV": "1"}) == base


# ── every secret the scripts actually ask for ──────────────────────────
#
# THE LIST IS THE TEST. `agentos_secret` replaced a literal in four
# scripts, and the only way that is safe is if each resolved name is
# byte-identical to the literal it replaced. One was not:
# `database-url-conductor` is PREFIXED like the other two DSNs, and
# matching is on the whole name rather than a prefix, so leaving it out
# of AGENTOS_SECRETS_PREFIXED produced `database-url-conductor-next` --
# a secret that does not exist, which would have failed the conductor
# deploy on all three of our environments with a message about a
# missing secret rather than about this change.
EVERY_SECRET_NEXT = {
    "database-url": "next-database-url",
    "database-url-migrate": "next-database-url-migrate",
    "database-url-conductor": "next-database-url-conductor",
    "execution-db-password": "execution-db-password-next",
    "execution-sqlproxy-key": "execution-sqlproxy-key-next",
    "openclaw-gateway-token": "openclaw-gateway-token-next",
    "credential-encryption-key": "credential-encryption-key-next",
    "agent-token-signing-key": "agent-token-signing-key-next",
    "gemini-api-key": "gemini-api-key-next",
    "control-panel-url": "control-panel-url-next",
    # Canonical name in, legacy name out -- see agentos_legacy_secret.
    "anthropic-api-key": "claude-api-key-next",
}


@pytest.mark.parametrize("base,expected", sorted(EVERY_SECRET_NEXT.items()))
def test_every_secret_a_script_asks_for_still_resolves_the_same(base, expected):
    assert _secret(base) == expected


def test_the_legacy_map_does_not_follow_us_into_a_stranger_project():
    """A single-environment install gets today's name, not our history.

    `anthropic-api-key` is what the published prerequisites.sh creates.
    Applying our rename history there would ask for `claude-api-key`,
    a secret that has never existed in that project.
    """
    assert _secret("anthropic-api-key", {"AGENTOS_SINGLE_ENV": "1"}) \
        == "anthropic-api-key"
