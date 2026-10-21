"""`build_sha` was the string "unset" on every line of every service.

WHAT IT COST. `obs/fields.process_fields` puts `build_sha` on every
structured log line, and it reads `BUILD_SHA` from the environment.
Nothing set it -- not the conductor's Deployment, not the verifier's,
not the execution pod's allowlist -- so the field was there, populated,
and populated with a constant. Every log line in the fleet claimed to
come from the same build.

The cost is not cosmetic. During the execution-plane work the question
after every push was "is the fix live yet?", and the only evidence
available was a rollout timestamp read beside a push time -- which is a
guess, and was wrong at least once. `heartbeat.py` has a query built to
answer exactly this (`count(DISTINCT build_sha) > 1`, to catch two
builds serving at once mid-rollout) and it explicitly skips rows where
the value is 'unset', so it could never fire.

AND THE POD MATTERS MOST. An execution pod OUTLIVES a conductor
rollout -- that is the whole point of moving legs into it -- so a story
can genuinely be running last build's kernel while the conductor runs
this one. That is the one case where the field is not a convenience,
and it is the one the allowlist had to carry.

DERIVED FROM THE IMAGE TAG, not passed as its own variable: two things
that can disagree eventually do, and the image is what is actually
running.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

MANIFEST = ROOT / "deploy" / "k8s" / "conductor.yaml"
SCRIPT = ROOT / "scripts" / "infra" / "deploy_execution_plane.sh"


def test_both_deployments_carry_the_build():
    """Two Deployments in one file, and a fix applied to one of them is
    the shape of half this project's bugs."""
    text = MANIFEST.read_text(encoding="utf-8")

    assert text.count("- name: BUILD_SHA") == 2, (
        f"expected BUILD_SHA on both the conductor and the verifier, "
        f"found {text.count('- name: BUILD_SHA')}")
    assert text.count('value: "${BUILD_SHA}"') == 2


def test_the_deploy_derives_it_from_the_image():
    """Not a second variable a caller has to remember. `${IMAGE##*:}`
    takes what follows the last colon, which is the tag in `repo:tag`
    and the hex in `repo@sha256:hex`."""
    text = SCRIPT.read_text(encoding="utf-8")

    assert "BUILD_SHA=\"${BUILD_SHA:-${IMAGE##*:}}\"" in text, (
        "the deploy no longer derives BUILD_SHA from the image it is "
        "deploying, so the two can now disagree")
    assert "export BUILD_SHA" in text, (
        "BUILD_SHA is computed but not exported, so render.py cannot "
        "see it and the deploy fails -- or worse, does not")


def test_the_render_refuses_a_manifest_that_would_say_unset():
    """THE PROPERTY THAT KEEPS THIS FIXED. `render.py` fails on an unset
    variable rather than substituting an empty string, so the manifest
    referencing `${BUILD_SHA}` is what makes a deploy that forgot it
    impossible. Without this the check above is a comment."""
    import subprocess
    import sys

    env = {"IMAGE": "r/i:abc123", "NAMESPACE": "ns", "PROXY_IMAGE": "p",
           "PROJECT": "pr", "ARTIFACT_BUCKET": "b", "CLOUDSQL_INSTANCE": "c",
           "CONTROL_PANEL_URL": "u", "SANDBOX_BASE_IMAGE": "s",
           "SANDBOX_BASE_DIGEST": "d", "EXECUTION_SQL_INSTANCE": "e",
           "CLOUDSQL_IP": "1.2.3.4",
           "SYSTEMROOT": "C:\\\\Windows", "PATH": ""}
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "infra" / "render.py"),
         str(MANIFEST)],
        capture_output=True, text=True, env=env, cwd=str(ROOT))

    assert out.returncode != 0, (
        "render.py accepted a manifest with BUILD_SHA unset, so a deploy "
        "can silently go back to logging 'unset' on every line")
    assert "BUILD_SHA" in (out.stderr + out.stdout)


def test_the_execution_pod_carries_it_too():
    """The pod's entire environment is this allowlist. A leg's log lines
    are the ones where the build matters most, because the pod outlives
    the rollout that replaced the conductor."""
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert "BUILD_SHA" in POD_ENV_ALLOWLIST


def test_the_field_is_still_read_from_that_name():
    """The guard against the two halves drifting: a manifest setting a
    variable nothing reads is the same failure with more steps."""
    src = (ROOT / "kernel" / "obs" / "fields.py").read_text(encoding="utf-8")

    assert 'os.environ.get("BUILD_SHA")' in src, (
        "obs/fields.py no longer reads BUILD_SHA, so the manifests set a "
        "variable nothing consumes and the logs still say 'unset'")
