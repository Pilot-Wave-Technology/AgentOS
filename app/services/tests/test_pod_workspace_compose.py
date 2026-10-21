"""The workspace is built inside the pod that will use it.

P3/D1. The conductor cannot compose a Kubernetes workspace: the claim is
ReadWriteOncePod and belongs to the execution's own pod, so the access
mode exists to make the API server refuse a second writer. Composition
moves into an initContainer, which calls the SAME
`CompositeDriver.acquire` the conductor calls on Docker -- a pod-side
reimplementation would be a second declaration of what a workspace is.
"""
import json

import pytest

from kernel.conductor.spec_builder import build_spec
from kernel.drivers import k8s_workspace
from kernel.drivers.k8s_spec import pod_manifest
from kernel.workspace import podinit
from kernel.workspace.mounts import Mount

ROW = {"id": 7, "kind": "errand", "domain": "ops", "goal_id": "G"}
MOUNT = Mount(at="repos/o__r", binding="git",
              source="https://example.invalid/o/r.git",
              ref="feat/G-abc", writable=True, kind="branch")


def _spec(mounts=(MOUNT,)):
    return build_spec(ROW, [], {"OPENCLAW_AUTH_TOKEN": "t",
                                "GEMINI_API_KEY": "secret"},
                      mounts=mounts)


def _pod(spec):
    return pod_manifest(spec, "exec-7", namespace="ns", pvc_name="ws-exec-7",
                        service_account="sa", bridge_image="registry/k:1")


def _compose_init(pod):
    got = [c for c in pod["spec"]["initContainers"]
           if c["name"] == "workspace-compose"]
    return got[0] if got else None


# ── the mount list survives the crossing ─────────────────────────────

def test_the_mount_list_travels_with_the_spec():
    # The pod has no database and must not: `mounts_for` reads four
    # tables and joins git_nodes to resolve the engine's feature branch.
    assert _spec().workspace_mounts == (MOUNT,)


def test_what_is_serialised_is_what_is_read_back():
    # Two processes, one contract. A field added on one side and missing
    # on the other is the drift this pairing exists to prevent.
    raw = k8s_workspace.serialise((MOUNT,))
    back = podinit.mounts_from(raw)
    assert len(back) == 1
    for field in ("at", "binding", "source", "ref", "writable", "kind"):
        assert getattr(back[0], field) == getattr(MOUNT, field)


def test_the_engines_branch_survives_the_crossing():
    # The whole point of carrying `ref`: the executor must land on
    # feat/<goal>-<story>, not the repo's default branch.
    back = podinit.mounts_from(k8s_workspace.serialise((MOUNT,)))
    assert back[0].ref == "feat/G-abc"
    assert back[0].writable is True


def test_an_unreadable_mount_list_is_not_an_empty_one():
    # Composing nothing would start a run against a workspace it was not
    # granted, which is the silent-wrong-answer version of this failure.
    with pytest.raises(podinit.ComposeFailed):
        podinit.mounts_from("{not json")


def test_no_mounts_is_a_legitimate_empty_workspace():
    assert podinit.mounts_from("") == ()
    assert podinit.mounts_from("[]") == ()


# ── the init container ───────────────────────────────────────────────

def test_the_workspace_is_composed_before_anything_runs():
    init = _compose_init(_pod(_spec()))
    assert init is not None
    assert init["command"] == ["python", "-m", "kernel.workspace.podinit"]


def test_the_bridge_is_delivered_before_the_workspace_is_composed():
    # Init containers run in sequence and each must succeed before the
    # pod is Ready; the order is what makes a failed clone a pod that
    # never starts rather than a run against a half-built tree.
    #
    # `workspace-setup` JOINED THE LIST IN P3 and its position is the
    # whole of its correctness: it installs what the committed
    # manifests declare, so before `workspace-compose` there is no
    # tree to read and it would silently install nothing. Plan 12.5 is
    # why it exists at all -- LEG_MAX_S is 2700s, and a thirty-minute
    # env_setup inside a leg leaves fifteen minutes for the work.
    #
    # STILL AN EXACT LIST, not an ordering assertion over a subset.
    # An init container is a thing that must succeed before the pod
    # starts, so one added without anybody deciding to is a new way
    # for every story to fail at once. Adding to this list should be a
    # deliberate edit.
    names = [c["name"] for c in _pod(_spec())["spec"]["initContainers"]]
    assert names == ["bridge-deliver", "workspace-compose",
                     "workspace-setup"]


def test_a_run_with_no_grants_gets_no_compose_container():
    # An errand with no mounts has an empty workspace by design; a
    # container that clones nothing can still fail.
    assert _compose_init(_pod(_spec(mounts=()))) is None


def test_the_composer_is_given_the_mounts_it_must_establish():
    init = _compose_init(_pod(_spec()))
    env = {e["name"]: e["value"] for e in init["env"]}
    assert json.loads(env[podinit.MOUNTS_ENV])[0]["ref"] == "feat/G-abc"


def test_the_clone_credential_is_a_reference_and_never_a_value():
    """The bearer reaches the composer WITHOUT passing through the spec.

    An earlier version of this asserted the token arrived as a plain
    value off `spec.env`, and it failed -- correctly.
    `assert_no_credentials` refuses any spec env key ending in _TOKEN,
    _KEY, _SECRET or _PASSWORD, so the bearer was absent by design and
    not by accident. Carrying it that way would have made that guard a
    formality and put the value in every `kubectl describe` that prints
    a pod spec.

    So Kubernetes delivers it: a secretKeyRef, resolved into the one
    container that clones.
    """
    pod = pod_manifest(_spec(), "exec-7", namespace="ns",
                       pvc_name="ws-exec-7", service_account="sa",
                       bridge_image="registry/k:1",
                       workspace_secret="conductor-secrets",
                       control_panel_url="https://cp.example")
    init = _compose_init(pod)
    by_name = {e["name"]: e for e in init["env"]}

    bearer = by_name["OPENCLAW_AUTH_TOKEN"]
    assert "value" not in bearer, "the token must not be a literal"
    assert bearer["valueFrom"]["secretKeyRef"]["name"] == "conductor-secrets"

    # The broker's address is not a secret and travels as a value.
    assert by_name["CONTROL_PANEL_URL"]["value"] == "https://cp.example"
    # And a model key has no business in a container that runs a clone.
    assert "GEMINI_API_KEY" not in by_name


def test_no_secret_configured_still_composes_public_sources():
    # The corpus and E0 shape. A workspace of public repos needs no
    # bearer, and refusing would break the substrate that has none.
    init = _compose_init(_pod(_spec()))
    assert "OPENCLAW_AUTH_TOKEN" not in {e["name"] for e in init["env"]}


def test_the_composer_is_in_autopilot_ratio():
    gib = k8s_workspace.INIT_MEMORY_MB / 1024
    assert 1.0 <= gib / float(k8s_workspace.INIT_CPUS) <= 6.5


# ── the bearer is shared with the worker and not the sandbox ─────────

def test_the_runtime_dir_reaches_the_worker():
    # `GitDriver` persists `credential.helper` pointing into it, so every
    # later fetch and push resolves the bearer from this volume.
    pod = _pod(_spec())
    worker = [c for c in pod["spec"]["containers"] if c["name"] == "worker"][0]
    paths = [m["mountPath"] for m in worker["volumeMounts"]]
    assert k8s_workspace.RUNTIME_DIR in paths


def test_the_sandbox_never_sees_the_runtime_dir():
    # git_bearer.py keeps the bearer outside /workspace precisely so a
    # sandbox that can read its own tree cannot read the credential. In a
    # pod that becomes a volume boundary, which is stronger than a path
    # convention -- and this is the test that keeps it one.
    pod = _pod(_spec())
    sandbox = [c for c in pod["spec"]["containers"]
               if c["name"] == "sandbox"][0]
    paths = [m["mountPath"] for m in sandbox["volumeMounts"]]
    assert k8s_workspace.RUNTIME_DIR not in paths


def test_the_composer_shares_the_runtime_dir_with_the_worker():
    init = _compose_init(_pod(_spec()))
    paths = [m["mountPath"] for m in init["volumeMounts"]]
    assert k8s_workspace.RUNTIME_DIR in paths
    assert "/workspace" in paths


# ── completeness is decided where the tree is ────────────────────────

def test_a_missing_checkout_is_reported_as_incomplete(tmp_path):
    # `acquire` returning is not evidence: a clone can fail per-mount and
    # leave the directory behind. A half-built tree is the same lie as no
    # tree.
    assert podinit.incomplete(str(tmp_path), (MOUNT,)) == ["repos/o__r"]


def test_a_real_checkout_counts_as_complete(tmp_path):
    (tmp_path / "repos" / "o__r" / ".git").mkdir(parents=True)
    assert podinit.incomplete(str(tmp_path), (MOUNT,)) == []


def test_a_document_mount_is_not_expected_to_be_a_checkout(tmp_path):
    doc = Mount(at="docs/spec.md", binding="copyin", source="gs://b/k",
                writable=False, kind="document")
    assert podinit.incomplete(str(tmp_path), (doc,)) == []
