"""How the committed tree reaches a criterion, on each substrate.

THE DEFECT THESE CLOSE. `kinds.command` passes `upload=(TREE_PARENT,
tree_tar)` on every command criterion, and the Kubernetes driver refused
`upload=` — correctly, since a Job's pod cannot be written to before it
starts, and silently dropping the tree would run every check against an
empty container. But it meant that on the substrate this whole stage is
being built for, EVERY criterion raised, `verify` caught it, and every
check came back `error`. Non-refusing, and also: no story could ever
reach DONE on Kubernetes.

The driver now STATES which it takes — `TREE_MODEL`, alongside
`SERVICE_MODEL` and `CAPTURE_MODEL` — and on "fetch" the tree becomes an
object in the artifact store that an initContainer pulls by id.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from kernel.artifacts.store import FilesystemStore, compute_id, set_store
from kernel.drivers.k8s_job import job_manifest
from kernel.spec import ExecutionSpec
from kernel.verifier.kinds import DEPS_PARENT, TREE_PARENT, tree_kwargs


def _tar(**files) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    binding = FilesystemStore(str(tmp_path / "artifacts"))
    set_store(binding)
    yield binding
    set_store(None)


class _Push:
    TREE_MODEL = "push"


class _Fetch:
    TREE_MODEL = "fetch"


# ── the seam ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_docker_still_gets_bytes_pushed_in():
    """The substrate that works must keep working. Local development
    runs Docker, and this is the path every existing test exercises."""
    tar = _tar(**{"verify/a.py": b"x"})
    kwargs = await tree_kwargs(_Push(), tar, None)
    # `also_upload` OMITTED rather than None: run_ephemeral_capture
    # does not take it, and the prepared-mount path uploads its tree
    # slice through this same helper.
    assert kwargs == {"upload": (TREE_PARENT, tar)}


@pytest.mark.asyncio
async def test_a_driver_that_declares_nothing_is_treated_as_push():
    """`TREE_MODEL` is read with a default, so a driver written before
    this existed keeps the behaviour it was written against rather than
    silently losing its tree."""
    kwargs = await tree_kwargs(object(), _tar(**{"verify/a.py": b"x"}), None)
    assert "upload" in kwargs


@pytest.mark.asyncio
async def test_kubernetes_gets_an_id_and_the_id_is_the_content_hash(store):
    """CONTENT-ADDRESSED, and that is the security property rather than
    a convenience: the id names this tree and nothing else, and `list`
    is withheld from the identity a criterion runs as — so holding a
    hash gets you exactly one tree and no way to find a second."""
    tar = _tar(**{"verify/a.py": b"x"})
    kwargs = await tree_kwargs(_Fetch(), tar, None)
    assert kwargs == {"fetch": [(TREE_PARENT, compute_id(tar))]}
    assert store.get(compute_id(tar)) == tar


@pytest.mark.asyncio
async def test_the_replayed_dependencies_travel_the_same_way(store):
    tree, deps = _tar(**{"verify/a.py": b"x"}), _tar(**{"deps/p.py": b"y"})
    kwargs = await tree_kwargs(_Fetch(), tree, deps)
    assert kwargs["fetch"] == [(TREE_PARENT, compute_id(tree)),
                               (DEPS_PARENT, compute_id(deps))]


# ── the manifest ─────────────────────────────────────────────────────────

def _spec(**kw):
    base = dict(image="py:1", command=["pytest", "-q"], execution_id=42,
                user="65534:65534")
    base.update(kw)
    return ExecutionSpec(**base)


def _job(fetch=None, **kw):
    return job_manifest(_spec(**kw), "crit-1", namespace="n", timeout_s=600,
                        service_account="agentos-verifier", fetch=fetch,
                        fetch_image="plane:sha", fetch_env={
                            "ARTIFACT_STORE": "gcs",
                            "ARTIFACT_BUCKET": "agentos-staging-artifacts"})


def test_no_fetch_leaves_the_job_exactly_as_it_was():
    """A Job with nothing to fetch gains no volume and no init
    container — the shape every existing assertion was written against."""
    pod = _job()["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    assert "volumes" not in pod


def test_the_tree_arrives_in_an_init_container_the_criterion_shares():
    pod = _job(fetch=[("/tmp", "a" * 64)])["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["command"] == ["python", "-m", "kernel.artifacts.fetch",
                               "/tmp", "a" * 64]
    assert init["image"] == "plane:sha"
    # The same volume, or the tree lands somewhere the criterion cannot
    # see and every check fails on a missing directory.
    criterion = pod["containers"][0]
    assert criterion["volumeMounts"] == init["volumeMounts"]
    assert [v["name"] for v in pod["volumes"]] == ["fetched-0"]
    assert pod["volumes"][0]["emptyDir"] == {}


def test_two_artifacts_at_one_destination_share_one_volume():
    """Tree and deps both land under /tmp. Two emptyDirs at the same
    mount path is a manifest the API server rejects."""
    pod = _job(fetch=[("/tmp", "a" * 64),
                      ("/tmp", "b" * 64)])["spec"]["template"]["spec"]
    assert len(pod["volumes"]) == 1
    assert pod["initContainers"][0]["command"][-4:] == [
        "/tmp", "a" * 64, "/tmp", "b" * 64]


def test_the_fetcher_runs_as_the_criterion_so_the_files_are_readable():
    """It writes the tree the criterion then reads and executes. A root
    initContainer leaves a tree the criterion cannot enter, and that
    fails looking like a broken check."""
    pod = _job(fetch=[("/tmp", "a" * 64)])["spec"]["template"]["spec"]
    init, criterion = pod["initContainers"][0], pod["containers"][0]
    assert init["securityContext"]["runAsUser"] == 65534
    assert (init["securityContext"]["runAsUser"]
            == criterion["securityContext"]["runAsUser"])


def test_the_fetcher_gets_the_store_address_and_not_the_contract_secrets():
    """Its whole job is turning an id into bytes. A declared secret in
    this container would be a credential in a pod that exists to hold
    none beyond the one grant it needs."""
    pod = _job(fetch=[("/tmp", "a" * 64)],
               env={"STRIPE_KEY": "sk_live"})["spec"]["template"]["spec"]
    names = {e["name"] for e in pod["initContainers"][0]["env"]}
    assert names == {"ARTIFACT_STORE", "ARTIFACT_BUCKET"}
    assert "STRIPE_KEY" in {e["name"] for e in pod["containers"][0]["env"]}


def test_the_uid_is_parsed_in_one_place():
    """k8s_spec parsed `spec.user` and k8s_job parsed it again. Three
    parsers agreeing by coincidence is how two come to disagree."""
    from kernel.drivers.k8s_spec import spec_uid

    assert spec_uid(_spec(user="1234:1234")) == 1234
    assert spec_uid(_spec(user="")) is None
    assert _job(fetch=[("/tmp", "a" * 64)], user="1234:1234")["spec"][
        "template"]["spec"]["initContainers"][0][
        "securityContext"]["runAsUser"] == 1234
