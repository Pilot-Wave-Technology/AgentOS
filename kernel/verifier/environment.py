"""F5 — reproducing the executor's environment, from a clean base.

The executor's sandbox drifts. A story runs `pip install pytest` and
then passes its own checks; the Verifier, starting fresh at the
contract's digest, has no pytest and fails the same criterion with "No
module named pytest". A refusal for a reason that is not the work.

THE WRONG FIX IS TO INHERIT THE EXECUTOR'S CONTAINER. That is defence 1,
and without it a PATH shim survives and everything F4 proved stops being
true. So the deltas are recorded as the executor makes them, and
replayed here from the base image.

HOW THE REPLAY KEEPS THE INTERNAL NETWORK. Installing needs the
internet; running a check must not have it. Doing both in one container
would hand every criterion a network, undoing F2d.

So it is two containers and a tar:

    prep     base image, NETWORKED, `pip install --target /deps`
             the result pulled out with GET /archive
    check    base image, INTERNAL network, the deps tar pushed in
             alongside the tree, PYTHONPATH=/tmp/deps

The install reaches the internet exactly once, in a container that runs
no criterion and sees no tree. The criterion sees the packages and no
network — which is the F5 requirement and the F2d defence at the same
time.

WHAT CANNOT BE REPLAYED SAYS SO. An `apt-get` delta mutates the image
and cannot be carried in a directory. It is recorded with
`reproducible = false` and reported, because F6's parity metric needs to
count what diverged — and a delta that vanishes is a divergence nobody
can see.
"""

from __future__ import annotations

import json
import logging

from kernel.spec import ExecutionSpec
from kernel.verifier import deps_layout

logger = logging.getLogger(__name__)

# WHERE, for both managers. Declared once in deps_layout and re-exported
# here because these names already had callers.
DEPS_PARENT = deps_layout.DEPS_PARENT
DEPS_DIR = deps_layout.PY_PATH
CONTAINER_DEPS = deps_layout.CONTAINER_DEPS

# Managers this can replay into a directory the check container carries.
# `npm` joined `pip` when `env_install` stopped installing it globally: a
# global install mutates the image and cannot travel in a directory, a
# prefix install is a directory. See kernel/verifier/deps_layout.py.
# `apt` is still out, and for the original reason -- it really does
# mutate the image and there is no prefix form of it.
REPLAYABLE = {"pip", "npm"}

# Where an installer may write when the image gave its uid no home.
# `/tmp` because it is 1777 in every image, is the emptyDir the
# prepared-mount pod already mounts, and is inside NEITHER capture
# directory -- a pip cache has no business in a tree `only_new` diffs.
INSTALL_HOME = "/tmp"


def install_env(driver) -> dict:
    """What a container that INSTALLS has to be told, or it hangs.

    THE ONLY ROUTE OUT IS THE PROXY, AND NEITHER CONTAINER WAS TOLD.
    `build_deps` below and `prepare._prepare_mount` are the two places
    the Verifier runs something networked, and both built an
    ExecutionSpec with no env at all. The namespace is default-deny
    egress: the capture Job carries `agentos.io/egress: proxy`, which
    admits it to port 3128 of the squid Deployment and to DNS and to
    nothing else. So an install that does not USE the proxy does not
    FAIL -- there is no RST to fail on -- it HANGS.

    Measured in-pod on the executor's sandbox, which had the identical
    hole (fixed in `k8s_spec._proxy_env`): `pip install` direct to pypi
    never returns; through the proxy, 200 in 0.1s. There it burned
    twenty minutes holding the bridge's tool lock and destroyed the
    pod. Here it would burn the whole verify budget and come back as a
    timeout -- in verification, where nobody is watching, and where
    `prepare`'s entire job is rebuilding a tree's declared environment.

    THE FULL PORT (3128), NOT THE STORE PORT (3129), and the choice is
    per CONTAINER rather than per component. 3129's allowlist is the
    object store alone: right for a criterion, which needs only the
    tree it arrives from, and useless to these two, whose whole purpose
    is fetching from a package index. What makes the wider reach
    containable is that neither runs a criterion, neither is given the
    held-out checks, and `prepare` hands each container ONE mount's
    committed sources. A criterion still gets no proxy at all (see
    kernel/verifier/kinds.py) and its Job still carries `egress: store`
    (kernel/drivers/k8s_job.py); widening either of those is the
    containment regression this note exists to prevent.

    AND A WRITABLE HOME, found the same way. A uid with no passwd entry
    resolves HOME to `/`, which it cannot write, and a bare `pip
    install` then falls back to the user site and dies with

        OSError: [Errno 13] Permission denied: '/.local'

    The capture Job's work container declares no securityContext, so it
    runs as whatever the CONTRACT's image declares -- root for the E0
    fixture, and any uid at all for an image a contract names. Setting
    this costs nothing when the image's home is already fine and is the
    difference between a verification and a permission error when it is
    not.

    ASKED OF THE DRIVER, not read from this process's environment, for
    the reason TREE_MODEL and CAPTURE_MODEL are asked of it: the
    Kubernetes driver already resolved which proxy this deployment has,
    and a second read here is how a Verifier ends up pointed somewhere
    the executor is not. The Docker driver has no such attribute, so on
    E0 and on a laptop this is empty, `_proxy_env` returns nothing, and
    these containers keep the direct network they have always had.

    `_proxy_env` RATHER THAN A SECOND SPELLING. Which variables, in
    which cases, with which NO_PROXY is one decision and it is made in
    kernel/drivers/k8s_spec.py. It is private only because it had no
    caller outside that module until now.
    """
    from kernel.drivers.k8s_spec import _proxy_env      # noqa: PLC0415

    env = {"HOME": INSTALL_HOME}
    env.update({item["name"]: item["value"]
                for item in _proxy_env(getattr(driver, "egress_proxy", ""))})
    return env


async def record(conn, execution_id: int, manager: str, packages: list,
                 reproducible: bool = True, detail: dict | None = None) -> int:
    """Append what the executor just installed. Returns the seq."""
    seq = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM env_deltas "
        "WHERE execution_id = $1", execution_id)
    await conn.execute(
        "INSERT INTO env_deltas (execution_id, seq, manager, packages, "
        "reproducible, detail) VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)",
        execution_id, seq, manager, json.dumps(list(packages)),
        reproducible, json.dumps(detail or {}))
    logger.info("execution %s env delta %s: %s %s", execution_id, seq,
                manager, ", ".join(packages))
    return seq


async def deltas_for(conn, execution_id: int) -> list[dict]:
    """Everything the executor installed, in the order it did.

    Order matters and is not a set: `pip install X` then
    `pip install X==1.0` is a different environment from the reverse,
    and a replay that loses the order reproduces something else.
    """
    rows = await conn.fetch(
        "SELECT seq, manager, packages::text, reproducible FROM env_deltas "
        "WHERE execution_id = $1 ORDER BY seq", execution_id)
    return [{"seq": r["seq"], "manager": r["manager"],
             "packages": json.loads(r["packages"]),
             "reproducible": r["reproducible"]} for r in rows]


def _packages_of(delta: dict) -> list[str]:
    """The package list, whether it arrives parsed or as JSON text.

    `env_deltas.packages` is a jsonb column and `record()` writes it with
    `json.dumps`. Read back through a plain `fetch`, asyncpg hands it over
    as a STRING -- and `list.extend` on a string extends by CHARACTER.
    So the replay was built from:

        ['[', '"', 'f', 'a', 's', 't', 'a', 'p', 'i', '>', '=', ...]

    and pip answered exactly as it should:

        ERROR: Invalid requirement: '[': Expected package name at the
        start of dependency specifier

    Every environment replay for every story failed this way, and the
    criterion reported "the executor's recorded environment could not be
    reproduced" -- which is true, and says nothing about a JSON string
    nobody parsed. Measured on execution 202.

    Parsed here rather than at the call site because `unreproducible`
    and `record` both touch the same column, and a second place to
    remember is a second place to forget.
    """
    raw = delta.get("packages")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            # A single package name stored bare is still a package.
            return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw if str(p).strip()]
    return []


def packages_for(deltas: list[dict], manager: str) -> list[str]:
    """Everything one manager installed, in the order it did.

    BY MANAGER rather than a single list, because the two do not share a
    command line. Order is preserved within a manager and the managers
    do not interleave -- `pip install X` then `npm install Y` then `pip
    install X==1.0` still ends at X==1.0, because pip's own order is
    kept and npm cannot affect it.
    """
    ordered: list[str] = []
    for delta in deltas:
        if delta["manager"] == manager and delta["reproducible"]:
            ordered.extend(_packages_of(delta))
    return ordered


def _pip_packages(deltas: list[dict]) -> list[str]:
    return packages_for(deltas, "pip")


def unreproducible(deltas: list[dict]) -> list[dict]:
    """Deltas the replay cannot carry. F6 counts these."""
    return [d for d in deltas
            if d["manager"] not in REPLAYABLE or not d["reproducible"]]


async def build_deps(driver, image: str, deltas: list[dict],
                     timeout: float = 300.0,
                     execution_id: int | None = None) -> tuple[bytes, dict]:
    """Install the recorded packages and return them as a tar.

    Returns (tar_bytes, report). An empty tar and a report is the honest
    outcome when there is nothing to install or the install failed — the
    caller turns a failed install into `error`, never `fail`, because a
    check that could not be given its dependencies did not run.

    `execution_id` IS NOT DECORATION. `capture_manifest` stamps the
    `execution-id` label only when the spec carries one, and until
    2026-09-17 neither capture call site did — so `orphan_sweep` saw a
    kernel-owned pod it could match to no execution, called it an
    orphan, and destroyed it seven seconds into `pip install`. The
    replay then failed, `error` retried, and it read as flake because a
    capture is short enough that only an overlapping tick bites. Keyword
    with a default, because the corpus validator and the inert probe
    verify things that have no execution at all.
    """
    pip = packages_for(deltas, "pip")
    npm = packages_for(deltas, "npm")
    packages = pip + npm
    skipped = unreproducible(deltas)
    if not packages:
        return b"", {"installed": [], "skipped": skipped, "ok": True}

    # THE SUBSTRATE MAY NOT BE ABLE TO DO THIS AT ALL, and saying so
    # here is the difference between a verdict and a traceback. On
    # Kubernetes a Job's pod cannot be read after it exits, so the
    # replay has nowhere to capture from — `run_ephemeral_capture`
    # refuses, and without this check that refusal would surface as
    # NotImplementedError from the middle of a verification rather than
    # as the outcome the caller already knows how to handle.
    #
    # ok=False makes every criterion `unrunnable`, which is `error` and
    # never `fail`: a check that could not be given its dependencies did
    # not run, so it found nothing wrong.
    if getattr(driver, "CAPTURE_MODEL", "archive") == "none":
        return b"", {
            "installed": [], "skipped": skipped, "ok": False,
            "output": (
                "this substrate cannot capture a finished container's "
                "filesystem, so the executor's recorded environment "
                "cannot be replayed. A story whose executor installed "
                "nothing verifies normally; this one recorded "
                f"{len(packages)} package(s). The replay needs to travel "
                "through the artifact store — see "
                "kernel/verifier/prepare.py."),
        }

    # ONE CONTAINER, BOTH MANAGERS, one captured directory. `&&` and not
    # `;`: a pip failure that let npm run would capture half an
    # environment and report `ok`, and the criterion would fail on the
    # missing half as if the work were wrong. The whole replay is
    # all-or-nothing by design -- see the caller, which turns a failed
    # build into `error` for every criterion.
    steps = []
    if pip:
        steps.append(deps_layout.pip_install(CONTAINER_DEPS, pip))
    if npm:
        # `mkdir -p` because npm, unlike pip's `--target`, does not
        # create its prefix -- and when pip ran first it already exists,
        # so this is only load-bearing for an npm-only story.
        steps.append(f"mkdir -p {CONTAINER_DEPS} && "
                     + deps_layout.npm_install(CONTAINER_DEPS, npm))

    spec = ExecutionSpec(
        image=image,
        command=["sh", "-c", " && ".join(steps)],
        # NETWORKED, and this is the only container in the whole Verifier
        # that is. It runs no criterion and is given no tree, so what it
        # can reach is a package index and nothing of the work.
        network_mode="bridge",
        # AND TOLD WHERE THE NETWORK IS. `network_mode` is a Docker word
        # and says nothing in a default-deny namespace, where the route
        # out is a proxy address. Without this the pip above does not
        # fail, it hangs — see `install_env` for which proxy and why.
        env=install_env(driver),
        memory_mb=1024,
        # THE LABEL THE ORPHAN SWEEP LOOKS FOR. See the docstring: a
        # capture pod with no execution id is one a reaper cannot tell
        # from a leak.
        execution_id=execution_id,
    )
    result = await driver.run_ephemeral_capture(
        spec, capture=CONTAINER_DEPS, timeout=timeout)
    # A run the SUBSTRATE ended -- a preempted node, a sidecar that
    # stored nothing -- is not a failed install, and a report carrying
    # only pip's output would send somebody to debug a package name.
    tail = (result.get("output") or "")[-1500:]
    if result.get("infra_error"):
        tail = f"[{result['infra_error']}]\n{tail}"
    report = {"installed": packages, "pip": pip, "npm": npm,
              "skipped": skipped,
              "ok": result["exit_code"] == 0 and bool(result.get("archive")),
              "output": tail}
    # THE LOG FOLLOWS `ok`, NOT THE EXIT CODE, and it used to follow the
    # exit code. `ok` is `exit_code == 0 AND an archive came back`, so a
    # replay whose install succeeded and whose CAPTURE returned nothing
    # took the success branch: "env replay installed 5 package(s), 0 not
    # reproducible", INFO, while handing the caller ok=False and turning
    # every criterion into `error`.
    #
    # Execution 205 did exactly that, eight times. The sidecar's actual
    # words -- `OSError: Project was not passed and could not be
    # determined from the environment` -- were sitting in
    # `report["output"]` the whole time and reached nothing but
    # `criterion_results.detail`, which is not where anybody looks when
    # a run is looping. Diagnosing it meant reading the database.
    #
    # A log line that says "installed" about a replay that produced
    # nothing is worse than silence: it sends the next person to look at
    # the executor.
    if not report["ok"]:
        logger.warning("env replay failed for %s: %s", packages,
                       report["output"][-300:])
        return b"", report
    logger.info("env replay installed %d package(s), %d not reproducible",
                len(packages), len(skipped))
    return result.get("archive") or b"", report


def rebase_deps_tar(tar_bytes: bytes) -> bytes:
    """Re-root the captured archive. See deps_layout.rebase.

    Kept as a name here because `verify_claim` already called it. The
    `prefix` argument is gone: the archive now unpacks into TWO roots --
    `deps/` for python and `node_modules/` for node -- and a single
    prefix could no longer describe where anything went.
    """
    return deps_layout.rebase(tar_bytes)
