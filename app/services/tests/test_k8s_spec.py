"""The pod manifest carries the one-pod decision, so it is asserted here.

Every test below corresponds to a line of reasoning in
docs/refactor/decisions/p1-one-pod-per-execution.md. A manifest is data,
so these need no cluster — which is the point of translating in a module
of its own. The day the client library changes, none of this moves.
"""

from __future__ import annotations

import pytest

from kernel.drivers.k8s_spec import (NODE_CLASS_LABEL, SANDBOX, WORKER,
                                     pod_manifest, pvc_manifest)
from kernel.spec import ExecutionSpec


def _spec(**kw):
    base = dict(image="agentos:1", execution_id=7, cpus=2.0, memory_mb=8192)
    base.update(kw)
    return ExecutionSpec(**base)


def _pod(**kw):
    return pod_manifest(_spec(**kw), "exec-7", namespace="agentos-staging",
                        bridge_image="registry/kernel:test",
                        pvc_name="ws-7", service_account="agentos-exec")


def test_one_pod_holds_both_containers():
    """Two pods would need a ReadWriteMany workspace — Filestore, and the
    two-writer race ReadWriteOncePod exists to prevent."""
    names = [c["name"] for c in _pod()["spec"]["containers"]]
    assert names == [WORKER, SANDBOX]


def test_both_containers_mount_the_same_workspace():
    """The worker commits at leg boundaries; the sandbox does the work.
    One volume is the reason they share a pod at all."""
    pod = _pod()
    mounts = [c["volumeMounts"][0]["name"] for c in pod["spec"]["containers"]]
    assert mounts == ["workspace", "workspace"]
    # ONE WORKSPACE, stated directly. This counted the pod's volumes and
    # asserted 1, which was the same claim only while the workspace was
    # the sole volume -- the bridge shim now arrives in a second one, so
    # the count says nothing about sharing. The property that matters is
    # that there is exactly one workspace and both containers are on it,
    # and that is now what is asserted.
    workspaces = [v for v in pod["spec"]["volumes"] if v["name"] == "workspace"]
    assert len(workspaces) == 1
    paths = {m["mountPath"] for c in pod["spec"]["containers"]
             for m in c["volumeMounts"] if m["name"] == "workspace"}
    assert paths == {"/workspace"}


def test_the_sandbox_gets_no_credential_and_one_path():
    """THIS TEST USED TO ASSERT `sandbox["env"] == []`, and that is the
    line that failed execution 10.

    "Not 'no secrets' — nothing" was the stated rule, and it was one
    word too strong. `kernel/bridge/tools_core.py` resolves the
    workspace root from WORKSPACE and falls back to `/tmp` when it is
    unset, so an env-less sandbox rooted every relative path, and
    `bash`'s cwd, in an empty directory -- while `/workspace` held a
    correct checkout the whole time. The executor called `list_dir`
    twelve times and failed for want of a repository that was there.

    The property worth defending was never "the list is empty". It is
    that nothing in the list OPENS anything: no token, no DSN, no
    bearer, no secret reference. A mount path is a fact about this
    container's own filesystem, visible to anything in it as `ls /`.

    HOME IS THE SECOND ENTRY, admitted on the same argument and for a
    failure of the same shape. `runAsUser: 65532` is an id the image has
    no passwd entry for, so HOME resolved to `/`, which that uid cannot
    write -- and `env_install(pip, ...)`, the one install path the
    Verifier reproduces, died with

        OSError: [Errno 13] Permission denied: '/.local'

    Measured in the built image as uid 65532. `/tmp` is 1777 and on the
    container's own layer, so it is writable, it is not the workspace,
    and like `/workspace` it is a path rather than a capability.
    """
    pod = _pod(env={"DATABASE_URL": "postgres://...", "GEMINI_API_KEY": "k"})
    worker, sandbox = pod["spec"]["containers"]
    names = [e["name"] for e in sandbox["env"]]
    assert names == ["WORKSPACE", "HOME", "GIT_CONFIG_COUNT",
                     "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                     "AGENTOS_CREDENTIALS"]
    values = {e["name"]: e["value"] for e in sandbox["env"]}
    assert values["WORKSPACE"] == "/workspace"
    assert values["HOME"] == "/tmp"
    # AGENTOS_CREDENTIALS IS THE THIRD ENTRY, admitted on exactly the
    # argument above: it is a DIRECTORY the lease was written into, and
    # kernel/workspace/credentials.py is built around that distinction --
    # "the POINTER is what the journal records ... a path is safe to
    # print". The secret is the file's CONTENT, which arrives on the
    # volume and never in this list.
    #
    # It was missing here and present on the Docker path, so a goal with
    # a cloud grant ran on Kubernetes with the pointer absent: the model
    # could not find the credential it had been issued, and the failure
    # surfaced far away as a permission error nobody traced back.
    assert values["AGENTOS_CREDENTIALS"].startswith("/workspace")
    # AND THE TOKEN IS NOT HERE. `names ==` already refuses anything
    # unlisted; this says which one we are actually worried about, since
    # the worker gets one and the sandbox shares its network namespace.
    from kernel.workspace.podfs_auth import ENV_VAR as PODFS_TOKEN

    assert PODFS_TOKEN not in names
    # safe.directory: git refuses a tree owned by another uid, and the
    # init container clones as the worker's. Relaxes an OWNERSHIP check,
    # not a permission -- the mode bits still decide what can be written.
    assert values["GIT_CONFIG_KEY_0"] == "safe.directory"
    # The half a name check would miss: a secretKeyRef carries a value
    # without ever naming one.
    assert not any("valueFrom" in e for e in sandbox["env"])
    assert any(e["name"] == "DATABASE_URL" for e in worker["env"]), (
        "the worker still needs its own environment")


def test_the_pid_namespace_is_not_shared_and_says_so():
    """False is the default. This design DEPENDS on it — sharing would
    let the sandbox read /proc/<worker>/environ and take the DSN, at
    which point no other boundary in the pod means anything. A default
    relied on silently is a default somebody changes."""
    assert _pod()["spec"]["shareProcessNamespace"] is False


def test_the_two_containers_run_as_different_users():
    """The same reasoning that makes the Verifier a different uid from
    the executor: nothing the sandbox writes should be executable by, or
    overwritable over, the worker's files in a shared tree."""
    pod = _pod(user="1000")
    worker, sandbox = pod["spec"]["containers"]
    assert worker["securityContext"]["runAsUser"] == 1000
    assert sandbox["securityContext"]["runAsUser"] != 1000


def test_no_container_may_escalate_privilege():
    for c in _pod()["spec"]["containers"]:
        ctx = c["securityContext"]
        assert ctx["allowPrivilegeEscalation"] is False
        assert ctx["capabilities"]["drop"] == ["ALL"]
        assert ctx["runAsNonRoot"] is True


@pytest.mark.parametrize("isolation,expected", [
    ("shared", None),
    ("syscall", "gvisor"),
])
def test_isolation_is_a_word_here_and_a_runtime_class_there(isolation,
                                                            expected):
    """`gvisor` is a Kubernetes noun and appears in exactly one place —
    the translation table. A caller that wanted a sandbox and had to
    spell it would depend on Kubernetes, which is what kernel/spec.py
    exists to prevent."""
    assert _pod(isolation=isolation)["spec"].get("runtimeClassName") == expected


def test_every_pod_names_the_pool_it_belongs_in():
    """INCLUDING THE DEFAULT, which is the inversion this records.

    The previous rule was that the default pool needs no selector,
    because a constraint that buys nothing can only fail to schedule.
    That held on Autopilot, where Google places the pod and there are no
    pools to choose between. On Standard an unselected pod lands on
    whatever has room -- in practice the platform pool, beside the
    conductor -- and one-story-per-machine is broken without anything
    failing: the story runs, on the wrong hardware, contending with the
    process that dispatched it.
    """
    assert _pod()["spec"]["nodeSelector"] == {NODE_CLASS_LABEL: "small"}
    assert _pod(node_class="large")["spec"]["nodeSelector"] == {
        NODE_CLASS_LABEL: "large"}


def test_the_grace_period_is_the_one_spot_actually_gives():
    """15 seconds, best effort. A larger number is silently truncated
    rather than honoured, so asking for 60 would put a figure in the
    manifest that nothing delivers."""
    assert _pod()["spec"]["terminationGracePeriodSeconds"] == 15


def test_the_claim_is_read_write_once_POD():
    """RWO permits two pods on the same NODE to mount read-write. For a
    git working tree where a replacement pod may be scheduled while the
    old one is still dying, the API server should refuse the second
    binding rather than let two writers race the index."""
    v = pvc_manifest(_spec(), "ws-7", namespace="agentos-staging")
    assert v["spec"]["accessModes"] == ["ReadWriteOncePod"]


def test_volume_class_and_size_come_from_the_spec():
    v = pvc_manifest(_spec(volume_class="fast", volume_gib=100), "ws-7",
                     namespace="agentos-staging")
    assert v["spec"]["storageClassName"] == "premium-rwo"
    assert v["spec"]["resources"]["requests"]["storage"] == "100Gi"


def test_requests_equal_limits():
    """Autopilot bills the REQUEST, and an out-of-ratio request is
    silently raised rather than refused. Requests and limits that differ
    would mean the number in the manifest is not the number on the bill."""
    c = _pod()["spec"]["containers"][0]
    assert c["resources"]["requests"] == c["resources"]["limits"]


def test_the_execution_id_is_a_label():
    """The reconciler finds containers by label. A pod it cannot
    attribute is one the orphan sweep must guess about."""
    assert _pod()["metadata"]["labels"]["execution-id"] == "7"
    assert _pod()["metadata"]["labels"]["managed-by"] == "kernel"


def test_the_module_needs_no_kubernetes_sdk():
    """E0's image has none, and E5's module-load imports everything under
    kernel/. A manifest is a dict; importing a client to build one would
    fail that gate on a machine behaving exactly as designed."""
    import kernel.drivers.k8s_spec as m

    assert "client" not in vars(m)
    assert "kubernetes" not in getattr(m, "__dict__", {})


# ── a criterion is a Job, and it may run for hours ───────────────────────

def _job(timeout_s, **kw):
    from kernel.drivers.k8s_job import job_manifest
    spec = ExecutionSpec(image="py:1", command=["pytest", "-q"],
                         execution_id=42, **kw)
    return job_manifest(spec, "crit-1", namespace="agentos-staging",
                        timeout_s=timeout_s, service_account="agentos-verifier")


def test_the_deadline_is_the_declared_timeout_not_a_constant():
    """A LARGE CODEBASE'S SUITE RUNS FOR TENS OF MINUTES. The planning
    note said five, which would kill a 40-minute suite at 5 and report a
    timeout — and an undeclared timeout is classified `error`, which
    tells the executor its environment is broken. It then edits a
    manifest it has no problem with, re-proposes, and fails with "N legs
    without converging". That loop is why budget.py exists, and a
    hardcoded deadline here would reintroduce it one layer down."""
    assert _job(2400)["spec"]["activeDeadlineSeconds"] == 2400
    assert _job(300)["spec"]["activeDeadlineSeconds"] == 300


def test_the_deadline_is_clamped_to_the_same_ceiling_budget_uses():
    """A criterion holds a slot and a pod for its whole run, so an
    unbounded declaration is a way to hold both for ever by writing a
    number in a file."""
    from kernel.drivers.k8s_job import MAX_DEADLINE_S

    assert _job(999_999)["spec"]["activeDeadlineSeconds"] == MAX_DEADLINE_S
    assert MAX_DEADLINE_S == 7200


def test_the_job_never_retries_itself():
    """`flake` and `fail` are different values in a four-valued verdict
    precisely so an intermittent test does not refuse a story. A Job that
    retried on its own would run a flaky test twice and report the second
    answer, collapsing them. Retrying is the adjudicator's decision."""
    assert _job(600)["spec"]["backoffLimit"] == 0


def test_a_declared_service_is_a_sidecar_in_the_criterion_pod():
    """The Docker driver builds a private network per verification. A pod
    does not need one — its namespace already is a private network, the
    service answers on localhost, and there is no network object to leak
    or misconfigure into admitting a second criterion."""
    from kernel.drivers.k8s_job import job_manifest, service_sidecar

    spec = ExecutionSpec(image="py:1", execution_id=42)
    j = job_manifest(spec, "crit-1", namespace="n", timeout_s=600,
                     service_account="v",
                     sidecars=[service_sidecar("postgres", "postgres:17")])
    names = [c["name"] for c in j["spec"]["template"]["spec"]["containers"]]
    assert names == ["criterion", "postgres"]


def test_the_verifier_runs_as_a_different_user_from_the_executor():
    """One of the four properties that make a verifier's container
    trustworthy: nothing the executor left owned and writable on the path
    can be executed by the thing checking it."""
    from kernel.drivers.k8s_spec import pod_manifest

    job_uid = _job(600)["spec"]["template"]["spec"]["containers"][0][
        "securityContext"]["runAsUser"]
    exec_pod = pod_manifest(ExecutionSpec(image="x", user="1000"), "e",
                           bridge_image="registry/kernel:test",
                            namespace="n", pvc_name="p", service_account="s")
    exec_uid = exec_pod["spec"]["containers"][0]["securityContext"]["runAsUser"]
    assert job_uid != exec_uid


def test_the_job_module_needs_no_kubernetes_sdk():
    import kernel.drivers.k8s_job as m

    assert "client" not in vars(m)


def test_a_criterion_gets_narrower_egress_than_an_executor():
    """THE NAMESPACE IS DEFAULT-DENY, and the policies select on
    `agentos.io/egress`, not on the app name — but the two classes get
    DIFFERENT values, and that is F2's defence rather than tidiness.

    An execution pod needs a package index, a git host and a model API.
    A criterion needs the artifact store its committed tree arrives
    from, and on Docker it gets `network_mode: none` and needs nothing:
    dependencies reach it through the recorded environment replay,
    which exists so a check never has to fetch.

    Giving both `proxy` — as the first version did, reasoning that "a
    verifier installs dependencies" — would hand every criterion the
    executor's reach, and a criterion that can reach the internet can
    fetch a version of itself that passes.
    """
    from kernel.drivers.k8s_job import job_manifest
    from kernel.drivers.k8s_spec import pod_manifest

    spec = ExecutionSpec(image="x", execution_id=1)
    pod = pod_manifest(spec, "e", namespace="n", pvc_name="p",
                       bridge_image="registry/kernel:test",
                       service_account="sa")
    job = job_manifest(spec, "c", namespace="n", timeout_s=600,
                       service_account="v")
    assert pod["metadata"]["labels"]["agentos.io/egress"] == "proxy"
    assert job["metadata"]["labels"]["agentos.io/egress"] == "store"
    # And on the Job's POD template, which is what the policy selects.
    assert job["spec"]["template"]["metadata"]["labels"][
        "agentos.io/egress"] == "store"


# THROUGH THE REAL RENDERER, not a second substitution written here. The
# deploy applies this manifest via scripts/infra/render.py, and a test
# that does its own `str.replace` is testing a copy -- it would pass
# against a renderer that had stopped working, which is how a check
# comes to be worth nothing. render() also REFUSES an unset name, so
# adding a ${...} to the manifest and forgetting it here fails loudly
# rather than leaving a literal in the parsed YAML.
def _rendered(**overrides):
    """`overrides` for the variables whose VALUE the test is about.

    Only one so far: PLATFORM_NODE_SELECTOR differs between an Autopilot
    and a Standard cluster, and both answers have to render. Everything
    else here is filler, because every other test that calls this is
    asking about the manifest rather than about the value.
    """
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "agentos_render", root / "scripts" / "infra" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render(
        (root / "deploy" / "k8s" / "conductor.yaml").read_text(
            encoding="utf-8"),
        {"NAMESPACE": "n", "IMAGE": "i", "ARTIFACT_BUCKET": "b",
         "PROXY_IMAGE": "p", "PROJECT": "j",
         "CLOUDSQL_INSTANCE": "j:us-central1:db",
         # The conductor tells each execution pod where the git
         # credential broker is; render.py refuses a blank one, which
         # is why adding the variable to the manifest broke this
         # helper rather than quietly rendering an empty value.
         "CONTROL_PANEL_URL": "https://cp.example",
         # TWICE NOW, and that is the guard working. A new required
         # variable fails every test that renders the manifest, loudly,
         # instead of rendering `image: ""` -- which is a pod spec that
         # names nothing and a failure that names nothing either.
         "SANDBOX_BASE_IMAGE": "reg/sandbox-base:test",
         # THREE TIMES NOW. E6: the same image by digest, which is what
         # `kernel/image_digest.py` checks the name against before it
         # will call a contract pinned. Its repository must MATCH the
         # one above or the resolver reports "not pinned" and says why
         # -- a declared digest for some other repository would silence
         # the parity warning AND dispatch the wrong image.
         "SANDBOX_BASE_DIGEST": "reg/sandbox-base@sha256:" + "0" * 64,
         # D1. The execution pod's own database is a SEPARATE decision
         # from the conductor's, so it is a separate variable -- and
         # `off` rather than blank, because render.py refuses a blank
         # and "not configured" still has to be renderable.
         "EXECUTION_SQL_INSTANCE": "off",
         "CLOUDSQL_IP": "10.1.2.3",
         # FOUR TIMES NOW, and the guard is still the reason this is a
         # one-line fix rather than a live incident. Every log line in
         # both Deployments said `build_sha: "unset"` because nothing
         # set BUILD_SHA; the deploy now derives it from the image tag
         # and the manifest names it, so render.py refuses here until
         # this helper knows about it too.
         "BUILD_SHA": "abc123",
         # FIVE TIMES NOW, and this one was not caught by the guard --
         # it was caught by GKE. `agentos.io/node-class` was written
         # LITERALLY into three Deployments, which renders and parses
         # perfectly and is then refused at admission by Autopilot's
         # Warden, so three deploys went red having patched nothing.
         # It is a variable now, and the Standard answer is the default
         # here because that is where the platform is going.
         # test_the_manifest_applies_to_the_cluster_it_is_given.py
         # overrides it to check the other branch.
         "PLATFORM_NODE_SELECTOR": "agentos.io/node-class: platform",
         # SIX TIMES NOW, and this one is the guard catching a variable
         # whose whole purpose is to change. It fingerprints the egress
         # allowlist so the proxy restarts when its rules do; squid
         # reads its config once at start, so without it an allowlist
         # change ships and does nothing. A fixed value here is correct:
         # these tests ask whether the annotation is PRESENT and
         # derived, not what it hashes to.
         "EGRESS_CONFIG_SHA": "deadbeef1234",
         # SEVEN. The serverless VPC connector's range, which the one
         # NetworkPolicy rule letting the control plane reach a pod
         # names. It was the literal 10.8.0.0/28 -- terraform's default,
         # so it worked only for whoever left the default alone.
         "CONNECTOR_CIDR": "10.8.0.0/28",
         # WHETHER THIS CLUSTER'S POOLS CARRY `agentos.io/env`.
         # `off` is the default here for the same reason
         # PLATFORM_NODE_SELECTOR's is the Standard answer: it is what
         # an environment gets before anyone migrates its pools, and it
         # is the behaviour every build had before the label existed.
         # deploy_execution_plane.sh decides the real value by asking
         # the cluster.
         "NODE_POOL_SCOPED": "off",
         # THE TOLERATION LINE, and `off`'s value for it is a YAML
         # COMMENT rather than a value -- an untainted environment gets
         # no `tolerations` key at all, not an empty one. That is the
         # difference test_a_pod_picks_its_own_environments_pool.py
         # asserts, and rendering it as anything else here would hide
         # it from every test in this file that parses the result.
         "PLATFORM_TOLERATIONS": "# untainted pools: no toleration needed",
         **overrides})


def test_the_network_policy_selects_the_label_the_manifests_set():
    """The two halves live in different files and are only correct
    together, so read the manifest rather than trusting the comment."""
    import yaml

    objects = [d for d in yaml.safe_load_all(_rendered()) if d]
    policies = {d["metadata"]["name"]: d for d in objects
                if d["kind"] == "NetworkPolicy"}

    egress = policies.get("workload-egress-via-proxy")
    store = policies.get("verifier-egress-to-store")
    assert egress, "the egress policy is gone; workloads have no path out"
    assert store, "the store policy is gone; no criterion can fetch its tree"
    assert egress["spec"]["podSelector"]["matchLabels"] == {
        "agentos.io/egress": "proxy"}
    assert store["spec"]["podSelector"]["matchLabels"] == {
        "agentos.io/egress": "store"}

    # DIFFERENT PORTS, or the two allowlists are one allowlist. squid
    # decides which hosts a port may reach; the policy decides which
    # port a pod may reach. Neither alone is the control, so a criterion
    # pointed at 3128 would silently get the executor's whole reach.
    def _port(policy):
        return [r["ports"][0]["port"] for r in policy["spec"]["egress"]
                if r.get("to")][0]

    assert _port(egress) != _port(store)


def test_the_store_port_reaches_less_than_the_full_port():
    """The two halves of the criterion's boundary live in a ConfigMap
    and a NetworkPolicy, and are only correct together. Read the ACLs
    rather than trusting the comment above them."""
    import yaml

    conf = next(d["data"]["squid.conf"]
                for d in yaml.safe_load_all(_rendered())
                if d and d["kind"] == "ConfigMap"
                and d["metadata"]["name"] == "egress-allowlist")

    def _hosts(acl):
        found = set()
        for line in conf.splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[:2] == ["acl", acl] \
                    and parts[2] == "dstdomain":
                found.update(parts[3:])
        return found

    store, full = _hosts("store_hosts"), _hosts("full_hosts")
    assert store, "the criterion's allowlist is empty; no tree can be fetched"
    assert store < full, (
        "a criterion may reach as much as an executor: F2's defence is "
        "that a check cannot fetch a version of itself that passes")
    # AND THE DEFAULT IS DENIAL. squid applies the first matching
    # http_access rule and allows what nothing matched, so without a
    # closing `deny all` every ACL above it is decoration.
    rules = [l.split()[1] for l in conf.splitlines()
             if l.strip().startswith("http_access")]
    assert rules[-1] == "deny", "the allowlist does not close; squid allows"


def test_every_substituted_env_value_is_quoted():
    """`value: ${BUILD_SHA}` unquoted is a NUMBER when the commit is 92873e3.

    Kubernetes parses manifests with Go's YAML, which reads digits-e-digits
    as a float, and an env value must be a string -- so the conductor and
    verifier Deployments were rejected ("cannot unmarshal number into ...
    EnvVar.value of type string") for the one commit whose hash looked like
    scientific notation. Python's YAML keeps it a string, so rendering here
    cannot catch it; the quoting is checked as text instead.
    """
    import pathlib
    import re

    manifest = (pathlib.Path(__file__).resolve().parents[3]
                / "deploy" / "k8s" / "conductor.yaml").read_text(encoding="utf-8")
    bare = re.findall(r"(?m)^\s+value: (\$\{[A-Z_]+\})\s*$", manifest)
    assert not bare, f"unquoted substituted env values: {bare}"
