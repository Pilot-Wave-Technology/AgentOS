"""E8/E15 — the prepare Job pulls the tree and writes the environment.

WHAT WAS TRUE BEFORE. `prepare._prepare_mount` ran a repository's setup
in a container, a sidecar tarred what it created, the tar went through
the artifact store, and the bytes were spliced back into a tree. Four of
the five faults of 2026-09-17 were steps in that chain and not one of
them was about verification: the capture pod had no route to the
metadata server; its service account had no Workload Identity binding;
the artifact id came back as a `str` holding a bytes repr; and a venv's
absolute interpreter symlink was refused by the tar filter.

Section 5 removes the chain rather than repairing it. A networked Job
mounts the verification claim and writes `.venv` / `node_modules`
straight to `volume.ENV`. There is no sidecar to lose its identity, no
id to misparse, and no tar filter to refuse a symlink.

AND E15 REMOVED THE PAYLOAD. The first version of that Job carried the
manifests as a base64 tar.gz in its own command, capped at 256KB,
because prepare fires at step 3 and the tree is not seeded until step 6.
It was a regression -- `.agentos/setup.sh` received only itself -- and
plan 5.15 resolves it: the prepare Job is NETWORKED and always was, so
an initContainer pulls the committed tree onto `volume.TREE` and the
setup runs against a real one.

These tests read the manifest the code really emits and the shell it
really builds. Nothing here greps for a helper's name: a helper that
returns the right string and is never called would pass that and leave
the cluster with a Job that mounts nothing.
"""

from __future__ import annotations

import io
import json
import tarfile
from types import SimpleNamespace as NS

import pytest

from kernel.verifier import (env_link, env_payload, prepare_job,
                             prepare_script, prepare_source, prepare_volume,
                             setups, volume)

PROXY = "http://egress-proxy:3128"
KERNEL_IMAGE = "gcr.io/x/conductor:sha"
SOURCE = {"host": "10.4.0.9", "uid": 65534,
          "mounts": [("", "/workspace", "abc123def456")]}


def _tar(members: dict) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tf:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return out.getvalue()


PY_TREE = _tar({"verify/requirements.txt": b"pytest==8.3.4\n",
                "verify/src/app.py": b"x = 1\n"})


class _Batch:
    def __init__(self):
        self.created = None

    def create_namespaced_job(self, namespace, body, **_):
        self.created = body

    def read_namespaced_job_status(self, **_):
        return NS(status=NS(succeeded=1, failed=None, conditions=[]))

    def delete_namespaced_job(self, **_):
        pass


class _Core:
    def list_namespaced_pod(self, **_):
        return NS(items=[NS(metadata=NS(name="prep-x"))])

    def read_namespaced_pod_log(self, **_):
        return "agentos-prepare: stop, wrote abc123\n"


@pytest.fixture
def cluster(monkeypatch):
    """A real KubernetesDriver over a fake API, plus the Job body sent."""
    from kernel.drivers import kubernetes as k8s
    from kernel.drivers.kubernetes import KubernetesDriver

    monkeypatch.setenv("AGENTOS_EGRESS_PROXY", PROXY)
    monkeypatch.setenv("AGENTOS_EXECUTION_IMAGE", KERNEL_IMAGE)
    monkeypatch.setenv("AGENT_TOKEN_SIGNING_KEY", "s3cret-signing-key")
    batch, core = _Batch(), _Core()
    monkeypatch.setattr(k8s, "_api",
                        lambda: (NS(BatchV1Api=lambda: batch), None))
    driver = KubernetesDriver(namespace="agentos-staging")
    monkeypatch.setattr(driver, "_core_api", lambda: core)
    return driver, batch


async def _build(driver, tree=PY_TREE, **kw):
    kw.setdefault("claim", "c")
    kw.setdefault("execution_id", 7)
    kw.setdefault("source", SOURCE)
    kw.setdefault("timeout", 60)
    return await prepare_volume.build_on_volume(
        driver, "contract:sha", tree, [""], **kw)


# ── the Job, as the cluster would receive it ─────────────────────────────

@pytest.mark.asyncio
async def test_the_job_mounts_the_claim_at_the_declared_path(cluster):
    """The whole point. A Job that does not mount the claim installs into
    a container filesystem that is deleted seconds later."""
    driver, batch = cluster
    await _build(driver, claim="verify-exec-7")

    pod = batch.created["spec"]["template"]["spec"]
    assert pod["volumes"] == [
        {"name": "verify",
         "persistentVolumeClaim": {"claimName": "verify-exec-7"}}]
    for container in pod["containers"] + pod["initContainers"]:
        assert container["volumeMounts"] == [
            {"name": "verify", "mountPath": volume.MOUNT}], container["name"]


@pytest.mark.asyncio
async def test_the_job_runs_the_contracts_image(cluster):
    """Defence 1, and 5.12 names it first: a PATH shim that survives from
    the executor into the thing checking the work makes F4 meaningless."""
    driver, batch = cluster
    await _build(driver)

    assert batch.created["spec"]["template"]["spec"]["containers"][0][
        "image"] == "contract:sha"


@pytest.mark.asyncio
async def test_the_job_is_networked_on_the_full_port(cluster):
    """An install needs a package index. A container with no route does
    not fail in a default-deny namespace -- it HANGS until the deadline,
    and a timeout reads as a fault in the work.

    `prepare` AND NOT `proxy`, since the class was added. This Job needs
    a pair no other class has -- the full port AND an execution pod's
    podfs on 9101 -- and neither existing class can carry it:

      * `proxy` also selects the EXECUTION pod, whose sandbox runs
        model-authored code. The 9101 rule there would let a story dial
        other stories' workspaces.
      * `capture` carries the metadata server, for a sidecar calling
        `storage.Client()`. This Job has no sidecar and no cloud
        identity, so that would hand a credential path to the one
        container here that runs a repository's own `.agentos/setup.sh`.

    The ADDRESS is unchanged -- all three resolve to the same proxy on
    3128 -- which is why only the label assertion moves."""
    from kernel.drivers.k8s_egress import LABEL, VIA_PREPARE

    driver, batch = cluster
    await _build(driver)

    assert batch.created["metadata"]["labels"][LABEL] == VIA_PREPARE
    env = {e["name"]: e["value"] for e in
           batch.created["spec"]["template"]["spec"]["containers"][0]["env"]}
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        assert env[name] == PROXY, f"{name} is not the full port"
    # A uid with no passwd entry resolves HOME to `/`, which it cannot
    # write, and pip then dies with Permission denied: '/.local'.
    assert env["HOME"]


@pytest.mark.asyncio
async def test_the_job_names_its_execution(cluster):
    """`orphan_sweep` destroys a kernel-owned pod it can match to no
    execution -- it killed a live install on 208 for this omission -- and
    plan 13 indexes Cloud Logging on the POD TEMPLATE's copy."""
    driver, batch = cluster
    await _build(driver, execution_id=42)

    assert batch.created["metadata"]["labels"]["execution-id"] == "42"
    assert (batch.created["spec"]["template"]["metadata"]["labels"]
            ["execution-id"] == "42")


def test_a_job_that_cannot_name_its_execution_is_refused():
    """Plan 13.2 makes the label non-negotiable, so there is no `None`
    branch to fall through: a pod that cannot be correlated is a pod
    whose logs are found by luck."""
    with pytest.raises(prepare_job.ExecutionUnnamed):
        prepare_job.prepare_manifest(
            NS(image="i", command=["sh"], env={}, cpus=1.0, memory_mb=512,
               # The real ExecutionSpec always carries this; the
               # prepare Job reads it to name the pool it belongs
               # in, since it asks for a whole tier of CPU.
               node_class="small",
               execution_id=None),
            "prep-1", namespace="n", claim="c", service_account="sa",
            timeout_s=60, kernel_image=KERNEL_IMAGE, source="{}", token="t")


@pytest.mark.asyncio
async def test_nothing_captures_and_nothing_is_stored(cluster):
    """No sidecar, no AGENTOS_CAPTURE_ID, no artifact store. The four
    faults have nowhere left to happen. The ONE init container is the
    tree fetch, which reaches podfs and never the artifact store."""
    driver, batch = cluster
    await _build(driver)

    pod = batch.created["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1
    assert [c["name"] for c in pod["initContainers"]] == [prepare_job.FETCH]
    assert "CAPTURE_ID" not in str(batch.created)


def test_the_install_runs_as_the_uid_that_will_execute_it():
    """One uid across two pods that never meet: this writes the `.venv`
    and the verification pod executes out of it. A fresh claim is
    root-owned, so `fsGroup` is what lets a non-root install create
    `volume.ENV` on it at all."""
    from kernel.verifier.kinds import VERIFIER_UID

    body = prepare_job.prepare_manifest(
        NS(image="i", command=["sh"], env={}, cpus=1.0, memory_mb=512,
               # The real ExecutionSpec always carries this; the
               # prepare Job reads it to name the pool it belongs
               # in, since it asks for a whole tier of CPU.
               node_class="small",
           execution_id=3),
        "prep-1", namespace="n", claim="c", service_account="sa",
        timeout_s=60, kernel_image=KERNEL_IMAGE, source="{}", token="t")
    pod = body["spec"]["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == VERIFIER_UID
    for container in pod["containers"] + pod["initContainers"]:
        ctx = container["securityContext"]
        assert ctx["runAsUser"] == VERIFIER_UID
        assert ctx["runAsNonRoot"] is True
    assert body["spec"]["backoffLimit"] == 0


def test_the_prepare_uid_matches_the_verification_pods():
    """`pod.py` declares who may write the volume and this writes it.
    Two spellings of 65534 is a uid that disagrees once."""
    from kernel.verifier import pod
    from kernel.verifier.kinds import VERIFIER_UID

    assert pod.UID == VERIFIER_UID and pod.GID == VERIFIER_UID


# ── E15: the tree is PULLED, and the token stays out of the setup ────────

@pytest.mark.asyncio
async def test_the_init_container_pulls_the_tree_from_the_execution_pod(
        cluster):
    """5.15. Prepare is networked and always was, so `.agentos/setup.sh`
    gets its whole slice again rather than a 256KB payload of itself."""
    driver, batch = cluster
    await _build(driver)

    init = batch.created["spec"]["template"]["spec"]["initContainers"][0]
    assert init["image"] == KERNEL_IMAGE, "the fetch needs kernel/"
    assert init["command"] == ["python", "-m", "kernel.verifier.prepare_fetch"]
    env = {e["name"]: e["value"] for e in init["env"]}
    body = json.loads(env[prepare_source.TREE_VAR])
    assert body["host"] == SOURCE["host"]
    assert body["mounts"] == [{"at": "", "repo": "/workspace",
                               "commit": "abc123def456"}]


@pytest.mark.asyncio
async def test_the_setup_container_never_holds_the_workspace_token(cluster):
    """5.15a. The one piece of model-adjacent code in this Job is the
    repository's own setup.sh; a credential it can read out of its own
    environment is a credential the executor chose what to do with."""
    from kernel.workspace.podfs_auth import derive_read_only

    driver, batch = cluster
    await _build(driver, execution_id=11)

    token = derive_read_only(11)
    pod = batch.created["spec"]["template"]["spec"]
    work = json.dumps(pod["containers"])
    assert token not in work
    init_env = {e["name"]: e["value"]
                for e in pod["initContainers"][0]["env"]}
    assert init_env[prepare_source.TOKEN_VAR] == token


def test_the_token_is_the_read_only_one_and_cannot_publish():
    """The verification pod gets none of this (5.15a). What the prepare
    Job gets is scoped to the ops that only read."""
    from kernel.workspace import podfs_auth

    assert "archive" in podfs_auth.READ_ONLY_OPS
    for op in ("publish", "commit", "credentials", "plan"):
        assert not podfs_auth.permits("readonly", op), op


def test_the_fetch_strips_the_prefix_the_pod_insists_on():
    """`podfs_stream.respond` falls back to "verify/" for an empty prefix,
    so the archive is rooted there and the layout on the volume is not:
    `volume.TREE` is where the criteria look."""
    from kernel.verifier import prepare_fetch

    assert prepare_fetch._relative("verify/src/app.py") == "src/app.py"
    assert prepare_fetch._relative("verify/repos/a/x") == "repos/a/x"
    assert prepare_fetch._relative("verify") is None
    assert prepare_fetch._relative("elsewhere/x") is None


@pytest.fixture
def on_disk(tmp_path, monkeypatch):
    """`volume.MOUNT` somewhere a test can look at it.

    The paths are declared once in volume.py and read at CALL time, so a
    test relocates the mount rather than re-spelling the layout -- which
    is the property that module exists for.
    """
    from kernel.verifier import prepare_fetch

    mount = tmp_path / "verify"
    monkeypatch.setattr(volume, "TREE", str(mount / "tree"))
    monkeypatch.setattr(volume, "ENV", str(mount / "env"))
    monkeypatch.setattr(prepare_source, "STAGE", str(mount / ".tree.tar"))
    (mount).mkdir()
    return prepare_fetch, mount


def _archive(members: dict, dst: str) -> None:
    with tarfile.open(dst, "w") as tf:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))


def test_the_fetch_lands_the_tree_where_the_criteria_will_look(on_disk,
                                                               monkeypatch):
    """The end of E15: the setup gets a real tree, so a script that reads
    a sibling works. Execution 207 wrote a setup.sh."""
    prepare_fetch, mount = on_disk

    def _fake(host, payload, dst, *, token, **_):
        assert token == "ro-token"
        _archive({f"{payload['prefix']}requirements.txt": b"-r base.txt\n",
                  f"{payload['prefix']}base.txt": b"pytest\n",
                  f"{payload['prefix']}.agentos/setup.sh": b"echo hi\n"}, dst)
        return 4096

    monkeypatch.setattr("kernel.workspace.podfs_stream.fetch", _fake)
    written = prepare_fetch.seed({"host": "h", "uid": 65534,
                                  "token": "ro-token",
                                  "mounts": [{"at": "", "repo": "/workspace",
                                              "commit": "abc"}]})
    tree = mount / "tree"
    assert written == 3
    assert (tree / "requirements.txt").read_bytes() == b"-r base.txt\n"
    assert (tree / "base.txt").exists()
    assert (tree / ".agentos" / "setup.sh").exists()
    assert not (mount / ".tree.tar").exists(), "the stage file leaked"


def test_a_second_round_does_not_inherit_the_first_rounds_files(on_disk,
                                                                monkeypatch):
    """The environment is cached across rounds and the tree is not. A file
    the commit DELETED, still sitting where a check can import it, is
    5.9's direction: a check passes that should fail."""
    prepare_fetch, mount = on_disk
    tree = mount / "tree"
    tree.mkdir(parents=True)
    (tree / "gone.py").write_text("stale")

    monkeypatch.setattr(
        "kernel.workspace.podfs_stream.fetch",
        lambda host, payload, dst, **_: (
            _archive({f"{payload['prefix']}kept.py": b"x\n"}, dst), 512)[1])
    prepare_fetch.seed({"host": "h", "uid": 0, "token": "t",
                        "mounts": [{"at": "", "repo": "/w", "commit": "c"}]})
    assert (tree / "kept.py").exists()
    assert not (tree / "gone.py").exists()


def test_a_commit_cannot_write_outside_the_tree(on_disk, monkeypatch):
    """The tar is `git archive` of whatever the model committed, so the
    data filter is what stops a member escaping onto `volume.ENV` one
    directory up."""
    prepare_fetch, mount = on_disk

    monkeypatch.setattr(
        "kernel.workspace.podfs_stream.fetch",
        lambda host, payload, dst, **_: (
            _archive({f"{payload['prefix']}ok.py": b"x\n",
                      "verify/../../escaped": b"no\n"}, dst), 512)[1])
    prepare_fetch.seed({"host": "h", "uid": 0, "token": "t",
                        "mounts": [{"at": "", "repo": "/w", "commit": "c"}]})
    assert (mount / "tree" / "ok.py").exists()
    assert not (mount.parent / "escaped").exists()


def test_a_mounts_archive_request_carries_its_own_at():
    """A two-repo workspace reassembles on extraction, so a check written
    as `cd repos/acme__api && pytest` means the same thing here."""
    req = prepare_source.request({"at": "repos/acme__api",
                                  "repo": "/workspace/repos/acme__api",
                                  "commit": "deadbeef"}, 65534)
    assert req["prefix"] == "verify/repos/acme__api/"
    assert req["op"] == "archive" and req["commit"] == "deadbeef"


# ── the script, which is where the freshness guarantee lives ─────────────

def _script_for(tree: bytes, key: str = "k" * 64) -> str:
    return prepare_script.script(
        key, [(s["at"], s["command"]) for s in setups.from_tar(tree, [""])])


def test_the_old_environment_is_deleted_before_the_new_one_is_built():
    """5.9's dangerous direction. A round that REMOVES a dependency and
    installs over yesterday's `.venv` leaves the package there, and a
    check PASSES THAT SHOULD FAIL."""
    body = _script_for(PY_TREE)
    assert f"rm -rf {volume.ENV}" in body
    assert body.index("rm -rf") < body.index("python3 -m venv")


def test_the_stamp_is_written_last():
    """A stamp written before the install finishes names a half-built
    environment as current, and `volume.stale` believes it."""
    body = _script_for(PY_TREE)
    stamp_write = [i for i, line in enumerate(body.splitlines())
                   if line.startswith("printf %s") and volume.STAMP in line]
    install = [i for i, line in enumerate(body.splitlines())
               if "python3 -m venv" in line]
    assert stamp_write and install and stamp_write[-1] > install[-1]


def test_the_script_re_asks_the_volume_before_doing_anything():
    """The caller's stamp is the scheduler's copy; the volume is the thing
    holding the bytes, and volume.py says it wins."""
    body = _script_for(PY_TREE)
    first = body.splitlines()
    assert first[0] == "set -e"
    assert any(volume.STAMP in line and line.startswith("if ")
               for line in first)


def test_the_registrys_own_command_is_what_runs():
    """ONE registry, run on both sides. A second spelling here is how the
    executor ends up with an environment the verifier cannot rebuild."""
    body = _script_for(PY_TREE)
    command = setups.from_tar(PY_TREE, [""])[0]["command"]
    assert command in body


def test_the_slice_is_copied_out_of_the_tree_and_nothing_is_base64():
    """E15. The whole mount reaches the install, so a setup.sh that reads
    a sibling -- or a requirements.txt with `-r base.txt` -- works. 207
    wrote a setup.sh."""
    body = _script_for(PY_TREE)
    assert f"cp -R {volume.TREE}/. {volume.ENV}/" in body
    assert "base64" not in body, "the payload is retired with its cap"
    assert not hasattr(env_payload, "payload")
    assert not hasattr(env_payload, "MAX_PAYLOAD_BYTES")


def test_the_copy_does_not_ask_to_preserve_what_only_root_may():
    """`cp -a` implies `-p`, and a non-root copy then exits non-zero with
    a complete copy already in place -- the exact pod k8s_bridge records
    losing to `preserving times: Operation not permitted`."""
    body = _script_for(PY_TREE)
    assert "cp -a " not in body and "cp -p" not in body


def test_an_unseeded_mount_fails_loudly_rather_than_stamping_nothing():
    """A missing slice means the fetch and the discovery disagree about
    the mount list. Installing nothing would stamp an empty environment,
    and a later check fails on a dependency nobody asked for."""
    body = _script_for(PY_TREE)
    assert f"if [ ! -d {volume.TREE} ]" in body
    assert "exit 1" in body


# ── E16: the symlink nobody owned ────────────────────────────────────────

def test_node_modules_is_linked_from_the_tree_to_the_cached_environment():
    """Node resolves `node_modules` by WALKING UP FROM THE SOURCE FILE.
    A PATH entry does not help, so the install's answer has to be
    reachable from the tree the criteria actually run in."""
    tree = _tar({"verify/package-lock.json": b'{"lockfileVersion":3}\n',
                 "verify/package.json": b'{"name":"x"}\n'})
    body = _script_for(tree)
    link, target = env_link.pair("")
    assert link == f"{volume.TREE}/node_modules"
    assert target == f"{volume.ENV}/node_modules"
    assert f"ln -s {target} {link}" in body
    assert body.index("npm ci") < body.index("ln -s")


def test_the_link_is_replaced_rather_than_written_through():
    """`ln -s` onto a live symlink creates the link INSIDE the directory
    it points at, so round two would make ENV/node_modules/node_modules
    and leave the tree pointing at round one."""
    line = env_link.shell("api")
    assert line.index("rm -rf") < line.index("ln -s")
    assert line.startswith("if [ -d ")


def test_python_gets_no_such_link():
    """A venv bakes its interpreter path in ABSOLUTELY and both pods
    mount at volume.MOUNT, so ENV/<at>/.venv/bin/python is already the
    right string -- fault 12 made impossible rather than worked around.
    Routing it through the tree would put a path that is deleted every
    round into every shebang."""
    body = _script_for(PY_TREE)
    assert ".venv" not in body.split("sh -c")[-1].split("ln -s")[-1]
    assert prepare_script.bin_paths("", (".venv/bin",)) == [
        f"{volume.ENV}/.venv/bin"]


# ── the key, and what it costs when nothing moved ────────────────────────

@pytest.mark.asyncio
async def test_an_unchanged_manifest_costs_nothing(cluster):
    """5.10. A model calling `env_setup` defensively five times must not
    churn five pods and five RWO detach/attach cycles."""
    driver, batch = cluster
    first = await _build(driver)
    assert first["cache"] == "miss" and batch.created is not None

    batch.created = None
    again = await _build(driver, stamped=first["key"])
    assert again["cache"] == "hit" and again["ok"]
    assert batch.created is None, "an unchanged manifest started a Job"


def test_a_changed_manifest_changes_the_key():
    """The whole point of the cache. Round two adding a dependency must
    not run against round one's `.venv`."""
    one = env_payload.key("i", env_payload.collect(PY_TREE, [""]))
    two = env_payload.key("i", env_payload.collect(
        _tar({"verify/requirements.txt": b"pytest==8.3.4\nrequests\n"}), [""]))
    assert one != two


def test_a_changed_image_changes_the_key():
    """The same requirements against two interpreters are two `.venv`s."""
    found = env_payload.collect(PY_TREE, [""])
    assert env_payload.key("a:1", found) != env_payload.key("a:2", found)


def test_the_key_ignores_the_source_the_install_never_reads():
    """Keyed on the manifests, not the tree: editing `src/app.py` must not
    rebuild an environment, or every round pays for the volume again."""
    edited = _tar({"verify/requirements.txt": b"pytest==8.3.4\n",
                   "verify/src/app.py": b"x = 2\n"})
    assert (env_payload.key("i", env_payload.collect(PY_TREE, [""]))
            == env_payload.key("i", env_payload.collect(edited, [""])))


def test_a_lockfiles_package_json_counts_towards_the_key():
    """`setups` drops package.json when a lockfile supersedes it -- but
    `pnpm install --frozen-lockfile` READS package.json, so a commit
    editing it must rebuild."""
    tree = _tar({"verify/pnpm-lock.yaml": b"lockfileVersion: '9.0'\n",
                 "verify/package.json": b'{"name":"x"}\n'})
    found = env_payload.collect(tree, [""])
    assert ("", "package.json") in found and ("", "pnpm-lock.yaml") in found


def test_the_key_can_be_read_from_a_tree_on_disk(tmp_path):
    """E5's rule reaches here too: a `large` tier's tree is a killed
    process rather than a slow one, so `collect` opens a path instead of
    loading it."""
    path = tmp_path / "tree.tar"
    path.write_bytes(PY_TREE)
    assert (env_payload.collect(str(path), [""])
            == env_payload.collect(PY_TREE, [""]))


@pytest.mark.asyncio
async def test_the_key_is_the_committed_tree_not_the_working_one(cluster):
    """5.10's first rule. The executor's `env_setup` reads the WORKING
    tree; the verifier must key on what was committed, or it prepares an
    environment the verifier will never see. The only tree this function
    can see is `tree_tar`, which comes from `archive_committed_tree`."""
    import inspect

    src = inspect.getsource(prepare_volume.build_on_volume)
    assert "from_tar" in src and "from_dir" not in src


# ── failure is `error`, never `fail` ─────────────────────────────────────

def _ending(**over):
    async def _run(*_a, **_k):
        return {"exit_code": 1, "output": "", "timed_out": False,
                "infra_error": None, **over}
    return _run


@pytest.mark.asyncio
async def test_a_broken_manifest_reports_not_ok_and_says_why(monkeypatch,
                                                             cluster):
    """A check that could not be given its dependencies did not run, so it
    found nothing wrong. The caller turns `ok=False` into `error`."""
    driver, _ = cluster
    monkeypatch.setattr(
        driver, "run_job_on_claim",
        _ending(output="ERROR: No matching distribution"))
    report = await _build(driver)
    assert report["ok"] is False
    assert "No matching distribution" in report["output"]


@pytest.mark.asyncio
async def test_a_preemption_is_named_as_infrastructure(monkeypatch, cluster):
    """A run the SUBSTRATE ended says nothing about the manifest, and a
    report carrying only pip's output sends somebody to debug a package
    name that was never reached."""
    driver, _ = cluster
    monkeypatch.setattr(driver, "run_job_on_claim",
                        _ending(exit_code=None,
                                infra_error="the node was reclaimed"))
    report = await _build(driver)
    assert report["ok"] is False
    assert "the node was reclaimed" in report["output"]


@pytest.mark.asyncio
async def test_a_tree_that_never_arrived_is_told_apart_from_a_bad_install(
        monkeypatch, cluster):
    """Plan 13: the init container's log is the only thing that
    distinguishes them, and `run_job` deletes the Job in a `finally`."""
    driver, _ = cluster
    monkeypatch.setattr(
        driver, "run_job_on_claim",
        _ending(output="/bin/sh: was not seeded",
                extra={"tree-fetch": "agentos-prepare-fetch: stop (failed: "
                                     "WorkspaceUnreachable: no route)"}))
    report = await _build(driver)
    assert report["ok"] is False
    assert "WorkspaceUnreachable" in report["output"]


@pytest.mark.asyncio
async def test_a_tree_with_no_manifest_still_seeds_the_volume(cluster):
    """RE-POINTED, and the reason it used to hold is the reason it
    stopped. It read: "Nothing would be written, so a pod start and an
    RWO attach would buy a stamp on an empty directory." True when the
    Job's only job was installing.

    E15 gave it a second one: `prepare_fetch` runs as the init container
    and SEEDS `volume.TREE`. So a Job with no install commands buys the
    one thing a no-manifest run needs to be verified in its own pod --
    the committed tree on the volume, and a stamp naming the empty
    environment beside it. Without it there is no `env_key`, `warm_for`
    declines, and the single class of work with nothing to install was
    the only class that could never use the verification pod: a docs
    story, an errand, any repository declaring no dependencies.
    """
    driver, batch = cluster
    report = await _build(driver, tree=_tar({"verify/README.md": b"hi\n"}))
    assert report["ok"]
    assert batch.created is not None, (
        "no Job, so no tree on the volume and no env_key -- this run "
        "can only ever be verified through the tar path")


@pytest.mark.asyncio
async def test_a_substrate_with_no_volume_says_so_rather_than_raising():
    """"This substrate has no claim" and "the Job failed" are different
    facts, and a traceback out of the middle of a verification is how the
    first reads as the second."""
    report = await _build(NS())
    assert report["ok"] is False
    assert "no persistent claim" in report["output"]


@pytest.mark.asyncio
async def test_a_job_with_nowhere_to_pull_from_is_refused(cluster):
    """The regression 5.15 removed, refused rather than reintroduced: a
    setup run against an empty directory reports a repository that
    committed no manifests."""
    driver, batch = cluster
    report = await _build(driver, source={"host": "", "mounts": []})
    assert report["ok"] is False and "no workspace" in report["output"]
    assert batch.created is None


def test_the_seam_refuses_a_substrate_that_declares_it_cannot():
    """E17. The reach-through into `k8s_run` and `kubernetes._api` is
    hoisted onto the driver, and a substrate that says `none` answers
    with a sentence naming the field."""
    from kernel.driver import Driver

    assert Driver.CLAIM_JOB_MODEL == "none"
    from kernel.drivers.kubernetes import KubernetesDriver

    assert KubernetesDriver.CLAIM_JOB_MODEL == "job"


# ── what the criteria are told afterwards ────────────────────────────────

def test_the_bin_paths_are_derived_from_the_declared_layout():
    """The tar path scanned a captured archive to learn whether
    `.venv/bin` existed. Here the layout is declared, so there is nothing
    to look up and nothing that can disagree."""
    assert prepare_script.bin_paths("", (".venv/bin",)) == [
        f"{volume.ENV}/.venv/bin"]
    assert prepare_script.bin_paths("api", ("node_modules/.bin",)) == [
        f"{volume.ENV}/api/node_modules/.bin"]


def test_agentos_env_is_still_allowlisted():
    """It exists so a setup can say where its executables live, not to set
    LD_PRELOAD in the container that decides whether the work is done."""
    from kernel.verifier.mount_env import parse_env

    prepend, env, dropped = parse_env(
        "PATH=$TREE/bin\nNODE_PATH=$TREE/nm\nLD_PRELOAD=/evil.so\n",
        volume.TREE)
    assert prepend == [f"{volume.TREE}/bin"]
    assert env == {"NODE_PATH": f"{volume.TREE}/nm"}
    assert dropped == ["LD_PRELOAD"]
