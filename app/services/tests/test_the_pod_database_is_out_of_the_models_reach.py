"""D1: the execution pod gets a database, and the model cannot reach it.

The leg moves into the pod in D2, and `leg_lock` has to move with it —
an advisory lock dies with its connection, which is the property that
makes a crashed worker release a run instead of stranding it. So the pod
needs a connection of its own, and every assertion here is about the
same question: what would model-authored code in the sandbox have to do
to use it?

A manifest is data, so none of this needs a cluster. What it CANNOT
prove is the half that only a cluster can answer — that the proxy
actually reaches the instance through squid, and that the socket it
creates is connectable by the worker's uid. Those are named in the D1
report, not asserted here as if they had been checked.
"""

from __future__ import annotations

import pytest

from kernel.drivers import k8s_sqlproxy, k8s_workspace
from kernel.drivers.k8s_spec import (SANDBOX, SANDBOX_UID, WORKER, WORKER_UID,
                                     pod_manifest)
from kernel.spec import ExecutionSpec

SQL = k8s_sqlproxy.Connection(instance="proj:us-central1:db",
                              key_secret="execution-sqlproxy-key",
                              db_secret="execution-db")


def _pod(sql=SQL, **kw):
    base = dict(image="agentos:1", execution_id=7)
    base.update(kw)
    return pod_manifest(ExecutionSpec(**base), "exec-7", namespace="ns",
                        pvc_name="ws-7", service_account="agentos-execution",
                        bridge_image="registry/kernel:test",
                        egress_proxy="http://egress-proxy:3128", sql=sql)


def _container(pod, name):
    for key in ("initContainers", "containers"):
        for container in pod["spec"].get(key) or []:
            if container["name"] == name:
                return container
    raise AssertionError(f"no {name} container in the pod")


def _mounted(container, volume):
    return any(m["name"] == volume for m in container.get("volumeMounts", []))


def test_the_proxy_is_a_native_sidecar_holding_the_key():
    """`restartPolicy: Always` in initContainers, and first.

    The conductor's own sidecar gives the reasons and they bite harder
    here: it becomes ready BEFORE the worker, so nothing races the socket
    into existence, and it is terminated AFTER, so a leg finishing its
    transaction during teardown is not cut off by its own proxy.
    """
    pod = _pod()
    first = pod["spec"]["initContainers"][0]
    assert first["name"] == k8s_sqlproxy.CONTAINER
    assert first["restartPolicy"] == "Always"
    assert _mounted(first, k8s_sqlproxy.KEY_VOLUME)
    assert f"--credentials-file={k8s_sqlproxy.KEY_DIR}/" \
        f"{k8s_sqlproxy.KEY_ITEM}" in first["args"]


def test_the_key_is_mounted_by_exactly_one_container():
    """Containers cannot read each other's mounts, and that IS the
    boundary: `serviceAccountName` is a POD field, so Kubernetes offers
    no per-container identity to use instead. A second container naming
    this volume would be the whole design undone in one line."""
    pod = _pod()
    holders = [c["name"]
               for key in ("initContainers", "containers")
               for c in pod["spec"][key]
               if _mounted(c, k8s_sqlproxy.KEY_VOLUME)]
    assert holders == [k8s_sqlproxy.CONTAINER]


def test_the_sandbox_mounts_neither_the_key_nor_the_runtime_volume():
    """The runtime volume is where the socket lives, and it is already
    how the git broker bearer is kept away from the model. The sandbox
    mounting it would make the socket reachable by `bash`."""
    sandbox = _container(_pod(), SANDBOX)
    assert not _mounted(sandbox, k8s_sqlproxy.KEY_VOLUME)
    assert not _mounted(sandbox, k8s_workspace.RUNTIME_VOLUME)
    # And the worker does mount it, or the connection reaches nobody.
    assert _mounted(_container(_pod(), WORKER), k8s_workspace.RUNTIME_VOLUME)


def test_the_proxy_listens_on_a_socket_and_never_on_a_port():
    """THE LOAD-BEARING ASSERTION.

    Containers in a pod share a network namespace, so `--port=5432` --
    which is correct in the conductor's pod, because there is no sandbox
    in it -- would put the database on loopback where the model's own
    `bash` can dial it. A filesystem socket is not reachable, because the
    sandbox does not mount the volume it sits on.

    `--auto-iam-authn` is refused for the matching reason: on a TCP
    listener it makes reaching the port sufficient to be authenticated.
    """
    args = _container(_pod(), k8s_sqlproxy.CONTAINER)["args"]
    socket = [a for a in args if a.startswith("--unix-socket")]
    assert socket == [f"--unix-socket={k8s_workspace.RUNTIME_DIR}"]
    assert not [a for a in args if a.startswith(("--port", "--address"))]
    assert not [a for a in args if a.startswith("--auto-iam-authn")]
    # And the host the worker is handed is under the same volume, or the
    # socket is somewhere the worker is not looking.
    host = k8s_sqlproxy.socket_host(SQL.instance)
    assert host.startswith(k8s_workspace.RUNTIME_DIR + "/")


def test_only_the_worker_is_told_how_to_open_the_connection():
    """The password arrives by `secretKeyRef` -- no value in the
    manifest, the spec, or a `kubectl describe` -- and only in the
    container that will hold the lock. `shareProcessNamespace: False` is
    what stops the sandbox reading it out of /proc, so assert that too:
    the env list and the namespace are only correct together."""
    pod = _pod()
    worker = {e["name"] for e in _container(pod, WORKER)["env"]}
    sandbox = {e["name"] for e in _container(pod, SANDBOX)["env"]}
    wanted = {k8s_sqlproxy.HOST_ENV} | {v for v, _ in k8s_sqlproxy.DB_KEYS}
    assert wanted <= worker
    assert not (wanted & sandbox)
    assert pod["spec"]["shareProcessNamespace"] is False
    password = next(e for e in _container(pod, WORKER)["env"]
                    if e["name"] == "AGENTOS_LEG_DB_PASSWORD")
    assert "value" not in password
    assert password["valueFrom"]["secretKeyRef"]["name"] == SQL.db_secret


def test_the_two_uids_are_unchanged_and_the_proxy_is_the_workers():
    """The two-uid boundary is invariant I10 and D1 does not touch it.
    The proxy takes the WORKER's uid because it creates the socket the
    worker has to open -- and the contract's uid when a contract
    declares one, or the socket belongs to somebody neither container
    is."""
    assert (SANDBOX_UID, WORKER_UID) == (65532, 65531)
    pod = _pod()
    assert _container(pod, SANDBOX)["securityContext"]["runAsUser"] \
        == SANDBOX_UID
    assert _container(pod, WORKER)["securityContext"]["runAsUser"] \
        == WORKER_UID
    proxy = _container(pod, k8s_sqlproxy.CONTAINER)["securityContext"]
    assert proxy["runAsUser"] == WORKER_UID
    assert proxy["runAsNonRoot"] is True
    assert proxy["allowPrivilegeEscalation"] is False
    declared = _pod(user="1000")
    assert _container(declared, k8s_sqlproxy.CONTAINER)[
        "securityContext"]["runAsUser"] == 1000
    assert _container(declared, WORKER)["securityContext"]["runAsUser"] == 1000


def test_no_connection_means_the_pod_is_what_it_was():
    """E0, a laptop, and `next` before a person has minted the key. A
    half-configured pod that fails at dispatch would make D1 a change to
    every environment rather than to the one that wants it."""
    pod = _pod(sql=None)
    names = [c["name"] for key in ("initContainers", "containers")
             for c in pod["spec"][key]]
    assert k8s_sqlproxy.CONTAINER not in names
    volumes = {v["name"] for v in pod["spec"]["volumes"]}
    assert k8s_sqlproxy.KEY_VOLUME not in volumes
    worker = {e["name"] for e in _container(pod, WORKER)["env"]}
    assert k8s_sqlproxy.HOST_ENV not in worker


@pytest.mark.parametrize("value", ["", "   ", k8s_sqlproxy.OFF])
def test_off_is_a_word_because_a_blank_is_the_bug_render_catches(
        value, monkeypatch):
    """render.py refuses an empty ${...} because an empty value is
    indistinguishable from the rendering bug it exists to catch. So "no
    database yet" travels as a word, and `from_env` has to know it."""
    monkeypatch.setenv(k8s_sqlproxy.INSTANCE_ENV, value)
    assert k8s_sqlproxy.from_env() is None
    monkeypatch.setenv(k8s_sqlproxy.INSTANCE_ENV, "proj:r:db")
    assert k8s_sqlproxy.from_env() == k8s_sqlproxy.Connection(
        "proj:r:db", k8s_sqlproxy.KEY_SECRET, k8s_sqlproxy.DB_SECRET)


def test_no_metadata_egress_was_granted_to_the_execution_pod():
    """THE RULE D1 MUST NOT BREAK, and the reason the proxy carries a key
    at all. NetworkPolicy is per-POD, so a rule admitting
    169.254.169.254 for the execution pod admits the SANDBOX -- and
    model-authored code could then mint a Workload Identity token for
    `agentos-execution`. `verifier-egress-to-store` says so by name and
    grants it to the criterion Job's label instead.

    Read out of the manifest rather than trusted to the comment: the
    cheap way to make a Cloud SQL proxy work is exactly the rule this
    forbids, so it is the mistake a later change is most likely to make.
    """
    import yaml

    from app.services.tests.test_k8s_spec import _rendered

    execution_labels = {"app": "agentos-execution",
                        "agentos.io/egress": "proxy"}
    for doc in yaml.safe_load_all(_rendered()):
        if not doc or doc["kind"] != "NetworkPolicy":
            continue
        selector = doc["spec"]["podSelector"].get("matchLabels") or {}
        if not selector or not set(selector.items()) <= set(
                execution_labels.items()):
            continue
        blocks = [peer.get("ipBlock", {}).get("cidr", "")
                  for rule in doc["spec"].get("egress") or []
                  for peer in rule.get("to") or []]
        assert not [c for c in blocks if c.startswith("169.254.169.254")], (
            f"{doc['metadata']['name']} gives the execution pod the "
            f"metadata server, and the sandbox shares its network "
            f"namespace")


def test_the_sql_egress_rule_is_pinned_to_one_address_and_one_port():
    """A port-only rule needs no deploy-time lookup and would hand
    model-authored code an arbitrary outbound TCP channel to any listener
    on 3307 -- the exfiltration squid's allowlist exists to close."""
    import yaml

    from app.services.tests.test_k8s_spec import _rendered

    policy = next(d for d in yaml.safe_load_all(_rendered())
                  if d and d["kind"] == "NetworkPolicy"
                  and d["metadata"]["name"] == "execution-egress-to-sql")
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app": "agentos-execution"}
    rules = policy["spec"]["egress"]
    assert all(rule.get("to") for rule in rules), (
        "a rule with no destination permits that port to anywhere")
    assert [r["ports"][0]["port"] for r in rules] == [3307]
