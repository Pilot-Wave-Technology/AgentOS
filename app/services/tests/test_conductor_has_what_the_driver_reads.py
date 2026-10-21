"""The conductor is given every setting its driver reads.

THE DEFECT, which is the third in one day from the same family and the
most expensive, because the fix for the previous one made it invisible.

`_credential_helper` needs a broker URL. That URL used to come from
`GIT_CREDENTIAL_URL`, which only `infra/e0/docker-compose.yml` set, so
it was empty on Kubernetes. The fix derived it from `CONTROL_PANEL_URL`
-- a fact "every container that needs a broker already has".

It did not have it. `KubernetesDriver.__init__` reads
`os.environ.get("CONTROL_PANEL_URL") or ""`, and `deploy/k8s/
conductor.yaml` never set it: not in the env block, not in the
`conductor-secrets` bundle the deploy script builds. The conductor
passed an empty string into every pod manifest, the compose container
got no `CONTROL_PANEL_URL`, and executions 6, 7 and 8 all died in
`workspace-compose` with git asking for a username.

WHAT MAKES THIS FAMILY HARD. Each variable is read in one place, written
in another, and the two are different KINDS of file -- Python and YAML,
or Python and a shell script -- so no tool and no review reads both at
once. The previous guard (`test_local_only_configuration.py`) asks
whether the local compose file is the only writer; it cannot see this
one, because `CONTROL_PANEL_URL` has seven writers. All seven configure
Cloud Run services. None configures the conductor.

So this asks a narrower and more direct question: for every environment
name the DRIVERS read, is the conductor given it? The drivers are the
right scope because they are the code that only ever runs in the
conductor -- a variable they read and nobody sets is unambiguously a
deployment hole, with no "but some other process provides it".
"""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
DRIVERS = REPO / "kernel" / "drivers"
MANIFEST = REPO / "deploy" / "k8s" / "conductor.yaml"
DEPLOY = REPO / "scripts" / "infra" / "deploy_execution_plane.sh"

# Read by a driver and deliberately not given to the conductor.
NOT_SUPPLIED = {
    "AGENTOS_WORKSPACE_SECRET":
        "falls back to the literal name of the secret the deploy script "
        "creates ('conductor-secrets'), so the default IS the deployed "
        "answer rather than a hopeful guess. Setting it would be a "
        "second declaration of one name.",
    "DOCKER_HOST":
        "the Docker driver's socket. The conductor on Kubernetes never "
        "constructs that driver.",
    "HOSTNAME":
        "provided by the container runtime, not by us.",
    "AGENTOS_K8S_TIMEOUT_S":
        "an escape hatch, not a setting. kernel/drivers/k8s_call.py "
        "names 30s in code and says why; this exists only so a "
        "saturated control plane can be given more room without a "
        "build. Pinning it in the manifest would make the reviewed "
        "default the one thing nobody reads.",
}


def _env_read_by_drivers() -> dict:
    """{name: files}, for every environment read under kernel/drivers."""
    found: dict = {}
    for path in DRIVERS.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = _name_of(node)
            if name:
                found.setdefault(name, set()).add(path.name)
    return found


def _name_of(node) -> str:
    if isinstance(node, ast.Subscript) and \
            isinstance(node.value, ast.Attribute) and \
            node.value.attr == "environ" and \
            isinstance(node.slice, ast.Constant) and \
            isinstance(node.slice.value, str):
        return node.slice.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
            node.func.attr in ("get", "getenv"):
        owner = getattr(node.func.value, "attr", "") or \
            getattr(node.func.value, "id", "")
        if owner in ("environ", "os") and node.args and \
                isinstance(node.args[0], ast.Constant) and \
                isinstance(node.args[0].value, str):
            return node.args[0].value
    return ""


def _supplied() -> set:
    """Names the conductor's pod actually receives.

    Two sources, because the deployment has two: the manifest's own env
    block, and the `conductor-secrets` Secret the deploy script builds
    and the manifest pulls in wholesale with `envFrom`. A check that
    read only the manifest would report every secret as missing.
    """
    manifest = MANIFEST.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    names = set(re.findall(r"^\s*-\s*name:\s*([A-Z][A-Z0-9_]*)\s*$",
                           manifest, re.M))
    names |= set(re.findall(r"--from-literal=([A-Z][A-Z0-9_]*)=", deploy))
    return names


def test_the_scan_reads_both_halves():
    # Either half matching nothing would declare the other half complete.
    supplied = _supplied()
    assert "AGENTOS_K8S_NAMESPACE" in supplied, "the manifest was not read"
    assert "DATABASE_URL" in supplied, "the secret bundle was not read"
    assert len(_env_read_by_drivers()) >= 8


def test_every_setting_a_driver_reads_is_supplied():
    reads = _env_read_by_drivers()
    missing = {n: sorted(f) for n, f in reads.items()
               if n not in _supplied() and n not in NOT_SUPPLIED}
    assert not missing, (
        f"{sorted(missing)}: read by the conductor's drivers and set "
        f"nowhere in deploy/k8s/conductor.yaml or the conductor-secrets "
        f"bundle, so they are empty in production. CONTROL_PANEL_URL was "
        f"exactly this and cost executions 6, 7 and 8. Add it to the "
        f"manifest, or to NOT_SUPPLIED with the reason its absence is "
        f"correct. Read at: {missing}")


def test_the_exemptions_are_still_read():
    # An exemption for a variable nobody reads any more is a note that
    # has outlived its subject, and the list stops being reviewable.
    stale = set(NOT_SUPPLIED) - set(_env_read_by_drivers())
    assert not stale, f"{sorted(stale)} are no longer read; drop them"


def test_the_control_panel_url_specifically():
    """Named, because deriving the broker from it is what made its
    absence fatal rather than cosmetic."""
    assert "CONTROL_PANEL_URL" in _supplied(), (
        "the conductor cannot tell an execution pod where the git "
        "credential broker is, so every private clone will fail")
