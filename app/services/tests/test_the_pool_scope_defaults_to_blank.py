"""One option decides pool scoping, and blank must mean what `next` is.

WHY THIS TEST EXISTS. The isolation between two environments sharing a
cluster used to be spelled as `if env == "next"` in three scripts --
a sentence only this deployment can write. Somebody standing up their
own production from a clone is not `next` and is not `dev`, so every
one of those branches took the wrong side for them.

Replacing them with one option, AGENTOS_POOL_SCOPE, defaulting to
blank, makes the general case the default. But a default is a place a
regression hides: `dev` used to reach its behaviour through the `else`
branch, and a blank default silently moved it to the other side --
unscoped pods on next's nodes, and execution pods logging in as the
SHARED `agentos_leg`. Neither fails; both are wrong.

So both directions are pinned here:

  * blank  -> exactly what `next` did before, byte for byte
  * named  -> exactly what `dev` did before

and the build files are checked to be passing what they must.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "scripts" / "infra" / "deploy_execution_plane.sh"
POOLS = ROOT / "scripts" / "infra" / "gke_standard_pools.sh"

# bash, guarded: the CI test image is not guaranteed to carry every
# interpreter, and a check that cannot run must skip rather than pass.
needs_bash = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash is not on PATH")


def _extract(path: pathlib.Path, first_line: str, last_line: str) -> str:
    """Lift one self-contained decision out of a script, verbatim.

    The scripts call gcloud at the top level, so they cannot be sourced.
    Taking the exact text of the block and running THAT is the closest
    thing to testing the script itself.
    """
    text = path.read_text(encoding="utf-8")
    i = text.index(first_line)
    j = text.index(last_line, i) + len(last_line)
    return text[i:j]


def _run(snippet: str, env_name: str, scope: str | None, echo: str) -> str:
    setup = f'set -u\nENV_NAME={env_name!r}\n'
    if scope is not None:
        setup += f'AGENTOS_POOL_SCOPE={scope!r}\n'
    out = subprocess.run(["bash", "-c", setup + snippet + f'\necho "{echo}"'],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


# ── the leg login ────────────────────────────────────────────────────

# The inner `fi` closes the scope test; the outer one closes the
# override test. Stopping at the first lifts an unbalanced block.
LEG_BLOCK = _extract(DEPLOY, '  LEG_ROLE="${AGENTOS_LEG_ROLE:-}"', '    fi\n  fi')


@needs_bash
@pytest.mark.parametrize("env_name,scope,expected", [
    # THE NEXT CASE. next's build passes no scope at all, so this is
    # the state its pipeline is actually in.
    ("next", None, "agentos_leg"),
    ("next", "",   "agentos_leg"),
    # THE DEV CASE, which only holds because cloudbuild-dev.yaml now
    # says so -- see test_dev_declares_its_scope below.
    ("dev", "dev", "agentos_leg_dev"),
    # A CLEAN-ROOM DEPLOYMENT. One environment, no scope, and the bare
    # name is right: there is nobody to share the credential with.
    ("acme", None, "agentos_leg"),
    ("acme", "acme", "agentos_leg_acme"),
])
def test_the_leg_login_follows_the_scope(env_name, scope, expected):
    assert _run(LEG_BLOCK, env_name, scope, "$LEG_ROLE") == expected


# ── whether unscoped pools are an acceptable answer ──────────────────

SCOPE_BLOCK = _extract(DEPLOY, 'if [ -z "${AGENTOS_POOL_SCOPE-}" ]; then\n  SCOPE_MAY_BE_OFF',
                       'fi\n')


@needs_bash
@pytest.mark.parametrize("env_name,scope,expected", [
    # next's pools predate the label. "No" is a true answer there.
    ("next", None, "yes"),
    # dev NAMED a scope, so its pools were built with both labels and
    # "no" would be a broken question -- the deploy must refuse rather
    # than fall back onto next's nodes.
    ("dev", "dev", "no"),
    ("acme", None, "yes"),
])
def test_unscoped_is_only_acceptable_when_unscoped_was_asked_for(
        env_name, scope, expected):
    assert _run(SCOPE_BLOCK, env_name, scope, "$SCOPE_MAY_BE_OFF") == expected


# ── the build files must pass what the defaults no longer supply ─────

def _plane_step_env(path: pathlib.Path) -> str:
    """The env: block of the deploy-plane step, as text."""
    text = path.read_text(encoding="utf-8")
    i = text.index("- id: 'deploy-plane'")
    j = text.index("args:", i)
    return text[i:j]


def test_dev_declares_its_scope():
    """dev shares a cluster, so blank would put its pods on next's nodes."""
    assert "AGENTOS_POOL_SCOPE" in _plane_step_env(ROOT / "cloudbuild-dev.yaml")


def test_feature_declares_its_scope():
    """feature shares the cluster too; blank would land it on next's nodes."""
    assert "AGENTOS_POOL_SCOPE" in _plane_step_env(ROOT / "cloudbuild-feature.yaml")


def test_feature_runs_the_same_steps_as_dev():
    """cloudbuild-feature.yaml is dev's file with the names changed. A step
    added to dev and not here is the drift that made the old feature file
    a pipeline with no execution plane. publish-ledger-client is the one
    deliberate omission: only dev cuts PyPI releases."""
    def ids(name):
        return re.findall(r"^  - id: '([^']+)'",
                          (ROOT / name).read_text(encoding="utf-8"), re.M)
    dev = [i.replace("-dev", "-feature") for i in ids("cloudbuild-dev.yaml")
           if i != "publish-ledger-client"]
    assert ids("cloudbuild-feature.yaml") == dev


def test_next_declares_no_scope():
    """The one environment this change must not touch.

    next's pools carry no agentos.io/env label and its pods emit no env
    selector. Setting a scope for it would make the deploy demand a
    label its nodes do not have, and every conductor pod would go
    Pending -- which reports nothing on its own.

    The distribution's one environment is the same case. Its plane is
    deployed by the `execution-plane` step of `cloudbuild.yaml`, which
    runs `execution-plane.sh`: neither may set a scope, and the script's
    own default must be blank.
    """
    if IS_DISTRIBUTION:
        import yaml
        steps = yaml.safe_load(
            (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))["steps"]
        plane = [s for s in steps if s.get("id") == "execution-plane"][0]
        assert "AGENTOS_POOL_SCOPE" not in "\n".join(map(str, plane["args"]))
        script = (ROOT / "execution-plane.sh").read_text(encoding="utf-8")
        assert 'AGENTOS_POOL_SCOPE="${AGENTOS_POOL_SCOPE-}"' in script
        return
    assert "AGENTOS_POOL_SCOPE" not in _plane_step_env(ROOT / "cloudbuild-next.yaml")


def test_no_script_special_cases_next_any_more():
    """The branches this change replaced, gone rather than shadowed."""
    for path in (DEPLOY, POOLS):
        body = path.read_text(encoding="utf-8")
        # Comments still discuss next by name, and should. Code must not.
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("#"))
        offenders = re.findall(r'^\s*(?:next\)|.*\[\s*"\$ENV_NAME"\s*[!=]=\s*"next"\s*\]).*$',
                               code, re.M)
        # SMALL_MIN is the one legitimate remaining use: next's warm
        # node is a cost decision about next, not a scoping rule.
        offenders = [o for o in offenders if "SMALL_MIN" not in o]
        assert not offenders, f"{path.name} still branches on next: {offenders}"
