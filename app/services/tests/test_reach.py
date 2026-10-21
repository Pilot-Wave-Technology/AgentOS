"""Ec4a/Ec4b — which hosts a run may reach, and how a credential gets in.

`errand-ops/profile.toml` said *"No web tools: an errand gets no egress
unless someone adds it here deliberately"*, and it was false. The sandbox
runs with `network_mode: container:{self_id}` — it SHARES the worker's
network namespace — so it has full egress, and it has `bash`, so nothing
about the tool surface changes that.

`tools_web.fetch` refuses private, loopback and link-local addresses, which
stops credential theft from the metadata endpoint. It permits every PUBLIC
host, which is a different question and the one that matters:
`smtp.sendgrid.net` is a perfectly ordinary public host. **That is why "run
the job but do not send the email" had no answer** — three mechanisms
should have caught it and all three miss.

WHAT IS AND IS NOT CLOSED HERE. `web_fetch` now follows the story's grants.
`bash` still reaches whatever the namespace does, because closing that
means taking the sandbox out of the worker's namespace and moving the
bridge with it — a change to the transport every executor uses, which is
named in the progress log rather than pretended away.

No database needed for most of this; the grant derivation has its own file.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from kernel.workspace import credentials as cred
from kernel.workspace.egress import (BASE_HOSTS, allowed_hosts, host_of,
                                     is_never_allowed, matches, refusal)


# ── the allowlist ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("https://github.com/o/n.git", "github.com"),
    ("http://Example.COM:8080/path", "example.com"),
    ("github.com", "github.com"),
    ("github.com:443", "github.com"),
    ("gs://bucket/object", "bucket"),
    ("github.com.", "github.com"),
    ("", ""),
    (None, ""),
])
def test_the_host_is_extracted_the_same_way_everywhere(text, expected):
    """One reading of "which host is this", or the allowlist and the
    refusal disagree about what was asked for."""
    assert host_of(text) == expected


def test_a_grant_covers_the_subdomains_one_repo_needs():
    """A grant on `github.com` covers `api.github.com` and
    `codeload.github.com`. One repository is not usable without them, and
    forcing three registrations to clone one repo is the friction that
    gets an allowlist replaced with `*`."""
    allowed = {"github.com"}
    for host in ("github.com", "api.github.com", "codeload.github.com"):
        assert matches(host, allowed), host


def test_a_lookalike_domain_is_not_covered():
    """THE REASON IT IS A DOT-BOUNDARY TEST rather than `endswith`.
    `github.com.evil.test` ends with nothing the grant named."""
    allowed = {"github.com"}
    for host in ("github.com.evil.test", "evilgithub.com", "notgithub.com",
                 "github.como"):
        assert not matches(host, allowed), host


def test_an_ungranted_host_is_refused_however_ordinary_it_looks():
    """The email case, stated as a test. `smtp.sendgrid.net` is a
    perfectly ordinary public host and the SSRF guard has no reason to
    refuse it."""
    allowed = {"github.com"}
    for host in ("smtp.sendgrid.net", "hooks.slack.com",
                 "api.mailgun.net", "evil.test"):
        assert not matches(host, allowed), host


def test_package_registries_are_reachable_without_a_grant():
    """An errand cannot install anything otherwise, and `env_install` is
    on its surface. Deliberately tiny — every entry is a hole in
    default-deny, and publishing to one is irreversible regardless."""
    for host in BASE_HOSTS:
        assert matches(host, BASE_HOSTS), host
    assert matches("pypi.org", set(BASE_HOSTS))


def test_the_metadata_endpoint_is_never_reachable_by_any_grant():
    """The FLOOR under the allowlist. `tools_web` refuses the link-local
    ADDRESS; this refuses the NAME, before DNS gets a chance to turn one
    into the other on a GCP VM."""
    for host in ("169.254.169.254", "metadata.google.internal", "metadata",
                 "http://metadata.google.internal/computeMetadata/v1/"):
        assert is_never_allowed(host), host
    assert not is_never_allowed("metadata.example.com")


def test_the_refusal_says_where_reach_comes_from():
    """"blocked" reads as a bug and gets retried. The executor has to
    learn that reach follows grants, and what it does hold."""
    said = refusal("smtp.sendgrid.net", {"github.com", *BASE_HOSTS})
    assert "smtp.sendgrid.net" in said
    assert "github.com" in said
    assert "ask_human" in said
    assert "grant" in said.lower()


# ── web_fetch enforces it ────────────────────────────────────────────────

def _fetch(url, allowed):
    from kernel.harness.tools_worker import web_fetch
    return web_fetch({"url": url}, "tc-1", allowed)


def test_web_fetch_refuses_an_ungranted_host_before_connecting():
    got = _fetch("https://smtp.sendgrid.net/v3/mail/send", {"github.com"})
    assert got.exit_code == 1
    assert "E_HOST_NOT_GRANTED" in got.error


def test_web_fetch_refuses_the_metadata_name_whatever_was_granted():
    got = _fetch("http://metadata.google.internal/computeMetadata/v1/",
                 {"metadata.google.internal"})
    assert got.exit_code == 1
    assert "never reachable" in got.error


def test_an_unknown_allowlist_falls_back_to_the_ssrf_guard_alone():
    """`None` means the caller could not say — the console's ad-hoc path
    and the null-executor tests. Empty and None are different answers,
    and conflating them would break every path that never wanted this."""
    got = _fetch("http://127.0.0.1:8000/", None)
    assert got.exit_code == 1
    # Refused by the SSRF guard, NOT by the allowlist.
    assert "E_HOST_NOT_GRANTED" not in got.error


def test_an_empty_allowlist_still_permits_the_package_registries():
    """An errand with no grants can still install. It cannot reach
    anything anybody would send data to."""
    from kernel.workspace.egress import BASE_HOSTS as base

    assert matches("pypi.org", base)
    got = _fetch("https://evil.test/x", frozenset(base))
    assert "E_HOST_NOT_GRANTED" in got.error


# ── the credential seam ──────────────────────────────────────────────────

def test_the_pointer_is_allowlisted_and_the_secret_is_not():
    """`AGENTOS_CREDENTIALS` is a PATH, which is why it can be in an
    allowlist that exists to stop credentials being forwarded."""
    import sys
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "kernel" / "bridge"))
    import tools_core

    os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-travel"
    os.environ[cred.ENV_POINTER] = "/workspace/.agentos/credentials"
    try:
        env = tools_core._safe_env()
        assert cred.ENV_POINTER in env
        for secret in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                       "DATABASE_URL", "OPENCLAW_AUTH_TOKEN"):
            assert secret not in env, f"{secret} reached the sandbox"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop(cred.ENV_POINTER, None)


def test_a_lease_round_trips_and_revoking_is_deleting(tmp_path):
    """Revocation has to work MID-RUN, which an environment variable
    cannot do: a process keeps the value it started with."""
    assert cred.read_lease(str(tmp_path), "gcp-1") is None
    cred.write_lease(str(tmp_path), "gcp-1", {"token": "x", "ttl": 900})
    assert cred.read_lease(str(tmp_path), "gcp-1")["token"] == "x"
    assert cred.revoke(str(tmp_path), "gcp-1") is True
    # NONE, NOT AN ERROR. A caller that treats a missing lease as a crash
    # turns revocation into an outage.
    assert cred.read_lease(str(tmp_path), "gcp-1") is None
    assert cred.revoke(str(tmp_path), "gcp-1") is False


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "a/b", "", "x", "..", "GCP-1", "a" * 65,
    "lease;rm -rf /",
])
def test_a_lease_id_that_is_a_path_is_refused(bad):
    """A lease id becomes a filename. Anything else is a traversal
    wearing a lease's clothes."""
    with pytest.raises(cred.BadLease):
        cred.lease_path("/workspace", bad)


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX modes; Windows carries no equivalent")
def test_a_lease_is_owner_readable_only(tmp_path):
    """The sandbox runs as its own uid and the Verifier as `nobody`. A
    world-readable credential in a shared workspace is available to a
    process that was never granted it."""
    path = cred.write_lease(str(tmp_path), "gcp-1", {"token": "x"})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_the_sandbox_is_told_a_container_path_not_a_host_one(tmp_path):
    """Handing it the host path names a directory it cannot open, and
    that reads as a missing credential rather than a misconfiguration."""
    env = cred.sandbox_env("/workspace")
    assert env[cred.ENV_POINTER].startswith("/workspace")
    assert str(tmp_path) not in env[cred.ENV_POINTER]


@pytest.mark.parametrize("secret", [
    "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
    "github_pat_11ABCDEFG0abcdefghijklmnop",
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "ya29.a0AfH6SMBabcdefghijklmnopqrstuvwxyz",
    "-----BEGIN RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.x",
])
def test_a_credential_shaped_string_never_reaches_the_journal(secret):
    """A LAST LINE, not the design. The design is that the secret lives in
    a file nothing prints — but a journal is durable, and a credential in
    it outlives every rotation."""
    text = f"the command printed {secret} to stdout"
    assert cred.holds_secret(text)
    scrubbed = cred.scrub(text)
    assert secret not in scrubbed
    assert cred.REDACTED in scrubbed


@pytest.mark.parametrize("innocent", [
    "sk-", "AKIA", "ya29", "the token was rejected",
    "Authorization: Bearer <redacted>",
    "ghp_short",
    "commit sha 0123456789abcdef0123456789abcdef01234567",
])
def test_ordinary_output_is_not_mangled(innocent):
    """Over-scrubbing makes a journal unreadable and teaches people to
    ignore the redaction marker."""
    assert cred.scrub(innocent) == innocent


def test_the_seam_mints_nothing():
    """Stated as a test because the file is easy to mistake for the
    credential system. It decides where a credential LANDS and what the
    journal may see; minting is per provider and needs cloud access E0
    does not have."""
    import inspect

    src = inspect.getsource(cred)
    for minting in ("google.auth", "boto3", "sts", "generateAccessToken"):
        assert minting not in src, f"{minting} — this module must not mint"
