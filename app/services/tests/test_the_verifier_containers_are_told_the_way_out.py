"""The Verifier's own containers are told the way out, and which way.

WHAT WAS TRUE BEFORE. `kernel/verifier/prepare.py` and
`kernel/verifier/environment.py` are the two places the Verifier runs a
container with a network — one replays the executor's recorded `pip
install`, the other runs the repository's own `.agentos/setup.sh`, which
is the whole job of rebuilding a tree's declared environment. Both built
their `ExecutionSpec` with NO env at all. Grepped and confirmed: not a
proxy variable between them, and no HOME.

WHY THAT IS NOT MERELY UNTIDY. The namespace `agentos-staging` is
default-deny egress. The single route out is the squid Deployment, and
`workload-egress-via-proxy` admits a pod labelled `agentos.io/egress:
proxy` to port 3128 and DNS and to nothing else. There is nothing on the
other side to send an RST, so a container that does not USE the proxy
does not FAIL — it HANGS.

MEASURED, in-pod, on the executor's sandbox, which had the identical
hole (commit 141e156e): `pip install` direct to pypi.org never returns;
the same request through the proxy answers 200 in 0.1 seconds. On the
executor side that cost twenty minutes of a held tool lock and the pod.
Here it would consume the verification's whole budget and come back as
`activeDeadlineSeconds` — a timeout, in verification, where nobody is
watching and where a timeout gets read as a fault in the work.

WHY THE FIX IS SHAPED THIS WAY.

  ONE DECLARATION. `k8s_spec._proxy_env` already decides which variables
  (BOTH cases: curl reads `http_proxy`, most Python tooling reads
  either, npm reads both, and a half-set pair is a tool that silently
  bypasses the proxy and hangs exactly as before) and what NO_PROXY
  says. `install_env` adapts its manifest-shaped list into the dict an
  ExecutionSpec carries and adds nothing of its own to that decision.

  THE PORT IS CHOSEN PER CONTAINER. 3128's allowlist is the package
  indexes, the git hosts and the model APIs; 3129's is the object store
  alone. The two installing containers get 3128 because fetching from a
  package index is their entire purpose and 3129 would refuse pypi. A
  CRITERION still gets neither — no proxy env, and a Job labelled
  `egress: store` — because a criterion that can reach the internet can
  fetch a version of itself that passes. So do the declared services: on
  Kubernetes they are sidecars inside the criterion's own pod, and a
  proxy there would hand the criterion the reach through its database.

  ASKED OF THE DRIVER. `driver.egress_proxy` is resolved once, by the
  Kubernetes driver, from the same variable the executor's pods use. The
  Docker driver has no such attribute, so on E0 and on a laptop nothing
  is set and the containers keep the direct network they have.

These tests build the REAL specs and read the REAL manifests the drivers
emit. Nothing here greps source text: a helper that returns the right
names and is never called would pass such a test and hang in the
cluster, which is the failure mode being guarded against.
"""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace as NS

import pytest

from kernel.artifacts.capture import MARKER
from kernel.artifacts.store import FilesystemStore, set_store
from kernel.drivers.k8s_capture import WORK
from kernel.verifier import environment, prepare, setups
from kernel.verifier.services import Services

FULL_PROXY = "http://egress-proxy:3128"
STORE_PROXY = "http://egress-proxy:3129"

# BOTH CASES. Different tools read different ones, and a half-set pair
# is a tool that silently bypasses the proxy and hangs as before.
PROXY_NAMES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")


def _tar(members: dict) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tf:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return out.getvalue()


def _env_of(body: dict, container: str) -> dict:
    """The env of one container of a Job the driver actually created."""
    assert body is not None, "the driver created no Job at all"
    for entry in body["spec"]["template"]["spec"]["containers"]:
        if entry["name"] == container:
            return {e["name"]: e["value"] for e in entry["env"]}
    raise AssertionError(f"no container named {container!r} in the Job")


def _proxyish(env: dict) -> dict:
    return {k: v for k, v in env.items() if "PROXY" in k.upper()}


# ── a cluster that records what was sent to it ───────────────────────────

class _Batch:
    def __init__(self):
        self.created = None

    # `**_` because every call now carries `_request_timeout`;
    # a fake that rejects what production sends is not a fake of
    # this API. See kernel/drivers/k8s_call.py.
    def create_namespaced_job(self, namespace, body, **_):
        self.created = body

    def read_namespaced_job_status(self, **_):
        return NS(status=NS(succeeded=1, failed=None, conditions=[]))

    def delete_namespaced_job(self, **_):
        pass


class _Core:
    def __init__(self, log: str):
        self.log = log

    def list_namespaced_pod(self, **_):
        return NS(items=[NS(metadata=NS(name="job-x"))])

    def read_namespaced_pod_log(self, **_):
        return self.log


@pytest.fixture
def store(tmp_path):
    binding = FilesystemStore(str(tmp_path / "artifacts"))
    set_store(binding)
    yield binding
    set_store(None)


@pytest.fixture
def cluster(monkeypatch, tmp_path, store):
    """A real KubernetesDriver over fake API objects, plus the Job body.

    The manifest under assertion is the one `run_ephemeral_capture` and
    `run_ephemeral` really build — not one this test reassembles from
    the same parts, which would keep passing if the driver stopped
    calling them.
    """
    from kernel.drivers import k8s_ephemeral
    from kernel.drivers.kubernetes import KubernetesDriver

    monkeypatch.setenv("AGENTOS_EXECUTION_IMAGE", "plane:sha")
    monkeypatch.setenv("AGENTOS_EGRESS_PROXY", FULL_PROXY)
    monkeypatch.setenv("AGENTOS_STORE_PROXY", STORE_PROXY)
    # The prepared-mount cache would skip the container entirely on a
    # hit, and the real default path does not exist on a developer's
    # machine.
    monkeypatch.setattr(prepare, "CACHE_DIR", str(tmp_path / "envcache"))

    captured = store.put(_tar({"verify/.venv/bin/python": b"#!\n"}))
    batch, core = _Batch(), _Core(f"{MARKER} {captured}\n")
    monkeypatch.setattr(k8s_ephemeral, "_api",
                        lambda: (NS(BatchV1Api=lambda: batch), None))
    driver = KubernetesDriver(namespace="agentos-staging")
    monkeypatch.setattr(driver, "_core_api", lambda: core)
    return driver, batch


async def _run_prepare(driver) -> None:
    """The real prepared-mount path, over a one-file repository."""
    tree = _tar({"verify/requirements.txt": b"pytest\n"})
    await prepare.build_prepared(driver, "py:1", tree,
                                 setups.from_tar(tree, [""]), timeout=60)


async def _run_replay(driver) -> None:
    """The real environment-replay path, over one recorded install."""
    await environment.build_deps(driver, "py:1", [
        {"seq": 1, "manager": "pip", "packages": ["pytest"],
         "reproducible": True}], timeout=60)


# ── the hang ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_prepare, _run_replay],
                         ids=["prepared-mount", "environment-replay"])
async def test_every_container_that_installs_is_told_the_proxy(cluster, run):
    """THE DEFECT. Both of these ran `pip`/`npm` against a default-deny
    namespace with no proxy address, and a container with no route does
    not fail, it hangs until the Job's deadline kills it."""
    driver, batch = cluster
    await run(driver)

    env = _env_of(batch.created, WORK)
    missing = [n for n in PROXY_NAMES if n not in env]
    assert not missing, (
        f"the container that installs is missing {missing}; without "
        f"every one of them some tool bypasses the proxy and hangs")


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_prepare, _run_replay],
                         ids=["prepared-mount", "environment-replay"])
async def test_an_installing_container_gets_the_full_port_not_the_store(
        cluster, run):
    """THE PORT IS THE CONTAINMENT DECISION. 3129's allowlist is the
    object store alone and would refuse pypi; 3128's is the package
    indexes. A later change that pointed these at the store port would
    reintroduce the hang for a different reason, and one that widened a
    criterion's reach is caught by the criterion test below."""
    driver, batch = cluster
    await run(driver)

    env = _env_of(batch.created, WORK)
    assert {env[n] for n in PROXY_NAMES} == {FULL_PROXY}, (
        f"expected the full allowlist port, got {_proxyish(env)}")
    assert STORE_PROXY not in env.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_prepare, _run_replay],
                         ids=["prepared-mount", "environment-replay"])
async def test_in_cluster_addresses_do_not_go_through_squid(cluster, run):
    """Sending cluster-internal traffic to an internet proxy has it
    refused by the allowlist — squid has no entry for a pod IP."""
    driver, batch = cluster
    await run(driver)

    env = _env_of(batch.created, WORK)
    for name in NO_PROXY_NAMES:
        assert name in env, f"{name} is unset, so .svc goes to squid"
        assert "localhost" in env[name] and ".svc" in env[name]


# ── the home ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("run", [_run_prepare, _run_replay],
                         ids=["prepared-mount", "environment-replay"])
async def test_an_installing_container_has_a_home_it_can_write(cluster, run):
    """`runAsUser: 65532` has no passwd entry, so HOME resolves to `/`
    and a bare `pip install` falls back to the user site and dies with
    `OSError: [Errno 13] Permission denied: '/.local'` — measured on the
    executor's sandbox. The capture Job's work container declares no
    securityContext at all, so it runs as whatever uid the CONTRACT's
    image names and is exposed to the same thing."""
    driver, batch = cluster
    await run(driver)

    home = _env_of(batch.created, WORK).get("HOME")
    assert home and home != "/", (
        "the installing container has no writable HOME, so pip's user "
        "site resolves under / and the install dies of EACCES")


@pytest.mark.asyncio
async def test_the_home_is_not_inside_what_the_prepared_mount_captures(
        cluster):
    """A pip cache under HOME must not land in the tree `only_new`
    diffs: anything captured there is spliced over the committed
    sources and verified as if somebody had written it."""
    driver, _ = cluster
    from kernel.verifier.kinds import TREE_PATH

    home = environment.install_env(driver)["HOME"]
    assert not home.startswith(TREE_PATH)
    assert not home.startswith(environment.CONTAINER_DEPS)


# ── the containment the proxy must not widen ─────────────────────────────

@pytest.mark.asyncio
async def test_a_criterion_is_still_given_no_way_out(cluster):
    """F2's defence, asserted from the other side of this change. A
    criterion that can reach the internet can fetch a version of itself
    that passes, and can exfiltrate the tree it was given."""
    from kernel.verifier.kinds import run_command

    driver, batch = cluster
    await run_command(driver, {"base_image": "py:1"},
                      {"id": "c1", "kind": "command",
                       "spec": {"run": "pytest -q"}},
                      _tar({"verify/a.py": b"x\n"}), timeout=60)

    body = batch.created
    assert _proxyish(_env_of(body, "criterion")) == {}, (
        "a criterion was handed a route to the internet")
    assert body["metadata"]["labels"]["agentos.io/egress"] == "store", (
        "the criterion's Job must reach the store port alone")


class _RecordingDriver:
    """Enough of a substrate to watch what `Services` asks for."""

    SERVICE_MODEL = "network"

    def __init__(self):
        self.specs = []

    async def create_network(self, name, internal=False):
        self.internal = internal
        return "net-1"

    async def create(self, spec, **_):
        self.specs.append(spec)
        return "cid-1"

    async def run_ephemeral(self, spec, timeout=None):
        self.specs.append(spec)
        return {"exit_code": 0, "output": ""}

    async def destroy(self, cid, **_):
        pass

    async def remove_network(self, name):
        pass


@pytest.mark.asyncio
async def test_a_declared_service_is_given_no_way_out_either():
    """A service LISTENS; it does not fetch. On Docker it sits on an
    internal network with no egress at all, so a proxy address here
    would name a route that does not exist — and the readiness probe
    runs on that same network."""
    driver = _RecordingDriver()
    async with Services(driver, "v1") as svc:
        await svc.up([{"name": "db", "image": "postgres:17",
                       "env": {"POSTGRES_PASSWORD": "verify"},
                       "ready": {"command": "pg_isready", "timeout": 5}}])

    assert driver.internal is True
    assert driver.specs, "no service container was created"
    for spec in driver.specs:
        assert _proxyish(spec.env) == {}, (
            f"a service container was handed {_proxyish(spec.env)}")


@pytest.mark.asyncio
async def test_a_service_sidecar_shares_the_criterions_pod_and_its_reach():
    """On Kubernetes the service is a container INSIDE the criterion's
    pod, which carries `egress: store`. A proxy there would be the
    criterion reaching the internet through its own database."""
    driver = _RecordingDriver()
    driver.SERVICE_MODEL = "sidecar"
    async with Services(driver, "v1") as svc:
        await svc.up([{"name": "db", "image": "postgres:17"}])

    assert svc.sidecars, "no sidecar was built"
    for sidecar in svc.sidecars:
        env = {e["name"]: e["value"] for e in sidecar["env"]}
        assert _proxyish(env) == {}


# ── the substrate that has no proxy ──────────────────────────────────────

def test_a_substrate_with_no_proxy_is_left_with_its_direct_network():
    """E0 and a laptop run Docker, where `network_mode="bridge"` really
    is a network. Pointing those containers at `egress-proxy:3128` would
    break every local verification on a name that does not resolve, so
    the absence of the attribute has to mean absence, not a default."""
    env = environment.install_env(NS())

    assert _proxyish(env) == {}
    assert env["HOME"], "a writable HOME is not a Kubernetes concern"

# ── and the WORKER, which is where the push happens ──────────────────

def test_the_worker_container_can_reach_the_forge():
    """THE ONE THAT WOULD HAVE LOST A FINISHED STORY.

    The init container had the proxy, so the CLONE worked. The sandbox
    had it after B1, so `pip install` worked. The worker had neither --
    and the worker is where `story_commit` runs, and `story_commit`
    PUSHES.

    Measured in exec-197: `git ls-remote origin` from the worker took
    133 SECONDS and then "Failed to connect to github.com port 443".
    Default-deny drops rather than refusing, so it is a hang and then a
    timeout, on the LAST step of a story rather than the first.
    """
    from kernel.drivers.k8s_spec import pod_manifest
    from kernel.spec import ExecutionSpec
    from kernel.workspace.mounts import Mount

    mount = Mount(at="repos/a__b", binding="git", source="https://x/y",
                  ref="main", writable=True, kind="branch")
    pod = pod_manifest(
        ExecutionSpec(image="img", story_id="s1", execution_id=1,
                      workspace_mounts=(mount,)),
        "exec-1", namespace="ns", pvc_name="ws", service_account="sa",
        bridge_image="img", egress_proxy="http://egress-proxy:3128")

    by_name = {c["name"]: c for c in pod["spec"]["containers"]}
    for role in ("worker", "sandbox"):
        env = {e["name"]: e.get("value")
               for e in (by_name[role].get("env") or [])}
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                    "https_proxy"):
            assert env.get(var) == "http://egress-proxy:3128", (
                f"{role} cannot reach the forge: {var} is {env.get(var)!r}")
        assert "NO_PROXY" in env and "no_proxy" in env
