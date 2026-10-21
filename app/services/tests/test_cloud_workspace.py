"""Ec4d — a cloud grant delivers a lease, and the workspace says so.

FOUND ON A LIVE RUN. KER25 errand 91cdcd6e held a GCP project and an
identity, and its workspace looked like this:

    /workspace/exec-10663/
      WORKSPACE.md          <- no mention of cloud, anywhere
      baa405ff-496          <- empty directory, raw resource id
      c6566f47-187          <- empty directory, raw resource id
      repos/...

Two separate faults with one cause: cloud kinds were added to the
grantable list without being taught to the mount layer.

  * THEY MOUNTED AS EMPTY DIRECTORIES. A credential is not a tree, so
    `copyin` had nothing to copy and the executor got two folders named
    by UUID with nothing in them and nothing explaining them. This is
    the same "granted but not materialised" shape the DOC_KINDS arm was
    written to fix, recurring for a new kind.

  * THE WORKSPACE NEVER SAID WHAT THE RUN COULD REACH. A grant delivers
    a LEASE, which appears in no mount list, so the only way to know was
    to read an environment variable the prompt happens to mention. The
    errand wrote a report leaning entirely on the repository -- the only
    thing its workspace admitted existed.

The fix is not to invent a mount for a credential. It is to stop
pretending one exists, and to state the reach in the file the model
actually reads.
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

LEASE = {"account": "cf-invoker@proj-a.iam.gserviceaccount.com",
         "scope": "proj-a", "region": "us-central1",
         "granted_via": "sa-inv", "expires_at": "2026-01-01T00:00:00Z"}


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── a credential is not a tree ───────────────────────────────────────────

def test_cloud_kinds_produce_no_mount():
    from kernel.workspace.mounts import mount_for

    for kind in ("gcp_environment", "gcp_test_sa", "aws_account",
                 "aws_test_role", "azure_subscription",
                 "azure_test_identity"):
        m = mount_for(kind, "whatever", resource_id="r1")
        assert m.at == "", f"{kind} mounts at {m.at!r}"
        assert m.binding == "none", f"{kind} has binding {m.binding!r}"


def test_ordinary_kinds_still_mount():
    """The exclusion must be narrow. A document is still a file."""
    from kernel.workspace.mounts import mount_for

    assert mount_for("document", "gs://b/spec.md").at == "docs/spec.md"
    assert mount_for("repo", "https://x/o/r").binding == "git"


def test_the_query_excludes_cloud_kinds_from_the_registry():
    """The SQL path is the one that actually ran and produced the empty
    directories -- `mount_for` alone was not enough. And the list comes
    from clouds.py, so a new cloud is not a second place to remember."""
    src = _read("kernel/workspace/mounts.py")
    body = src[src.index("FROM story_resources sr"):]
    body = body[:body.index("ORDER BY")] if "ORDER BY" in body else body
    assert "<> ALL($3::text[])" in body, (
        "cloud kinds are not excluded from the mount query")
    assert "ALL_IDENTITY_KINDS" in src and "ALL_ENVIRONMENT_KINDS" in src


# ── the workspace states the reach ───────────────────────────────────────

def test_the_workspace_names_the_project_and_the_identity():
    from kernel.workspace.briefing import cloud_section as _cloud_section

    text = _cloud_section(LEASE)
    assert "Cloud reach" in text
    assert LEASE["account"] in text
    assert LEASE["scope"] in text
    assert LEASE["granted_via"] in text, (
        "the grant is not named, so nobody can answer 'why could this "
        "run reach that project'")


def test_it_never_writes_the_token():
    """The whole point of the lease being a file is that the secret is
    not in anything written, printed or committed -- and WORKSPACE.md is
    all three."""
    from kernel.workspace.credentials import holds_secret
    from kernel.workspace.briefing import cloud_section as _cloud_section

    leaky = dict(LEASE, access_token="ya29.a0ARrdaM-EXAMPLE-0123456789abcdef")
    text = _cloud_section(leaky)
    assert "ya29." not in text
    assert not holds_secret(text)


def test_a_run_without_a_grant_sees_nothing_about_credentials():
    """Almost every run has no cloud reach. Telling them all about
    credentials would make the exception look like the rule."""
    from kernel.workspace.briefing import cloud_section as _cloud_section

    assert _cloud_section(None) == ""
    assert _cloud_section({}) == ""


def test_it_carries_the_rule_that_matters_most():
    """A permission error is the ANSWER. A run that goes looking for
    another route after being refused is doing exactly what the grant
    exists to prevent -- and the workspace is where it will be read."""
    from kernel.workspace.briefing import cloud_section as _cloud_section

    text = _cloud_section(LEASE).lower()
    assert "only cloud identity" in text
    assert "report it and stop" in text
    assert "never print" in text and "artifact" in text


def test_the_section_is_written_into_the_file():
    """A helper nothing calls is the shape that let `scrub` sit unwired
    for a whole build.

    The builder MOVED to kernel/workspace/briefing.py, and the move was
    a fix: the conductor's copy sat after the early return that hands a
    pod its own composition, so no execution on Kubernetes ever had a
    WORKSPACE.md at all. Both substrates call the one builder now.
    """
    src = _read("kernel/workspace/briefing.py")
    body = src[src.index("def write_workspace_md"):]
    assert "cloud_section(lease)" in body
    # And the lease must actually reach the writer, from the conductor.
    assert "write_workspace_md(path, mounts, lease)" in \
        _read("kernel/worker/runner.py")


def test_the_pod_writes_one_too():
    """The half that did not exist. On Kubernetes the conductor never
    sees the tree, so the write happens in podinit or nowhere -- and it
    was nowhere. The lease is conductor-only and the pod passes none,
    which renders no cloud section: correct, because a pod composing its
    own workspace has not been granted an identity at that point."""
    src = _read("kernel/workspace/podinit.py")
    assert "write_workspace_md(root, mounts)" in src


def test_the_lease_keys_the_worker_reads_are_the_ones_issue_returns():
    """THE BUG THIS CAUGHT THE HARD WAY. After the multi-cloud rename
    (`sa_email`->`account`, `project_id`->`scope`) this logging line kept
    the old keys, raised KeyError on a real errand, and the caller read
    it as "workspace not ready" -- so the run retried for ever,
    re-minting a credential each time. A logging statement took down the
    thing it was reporting on.

    Asserted against `issue`'s docstring rather than a literal list, so
    the next rename has to move both."""
    import re

    issue_src = _read("kernel/credentials/issue.py")
    declared = set(re.findall(r'"(\w+)"', issue_src[
        issue_src.index('out = {"lease_id"'):issue_src.index("return out")]))
    runner_src = _read("kernel/worker/runner.py")
    used = set(re.findall(r'lease\["(\w+)"\]', runner_src))
    missing = used - declared
    assert not missing, (
        f"runner reads lease keys that issue() does not return: {missing}")
