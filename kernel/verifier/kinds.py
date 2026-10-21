"""F2 — criterion kinds. Extension point 2, so this file is a registry.

The kernel owns the Verifier MECHANISM: a fresh container at the
contract's digest, the committed tree, the command from the contract.
The KINDS are the coding module's — `command`, `differential`, `metric`,
`probe`, `artifact` — and Stage J's milestone is that a documents module
ships with `git diff kernel/` empty. A `command` evaluator hardcoded into
the verifier loop would make that impossible, so the loop looks kinds up
here and a module adds one by registering.

`command` is the only kind F2 builds. The other four are named in the
plan and belong to increments that have their own tests; a stub that
returns `pass` would be worse than their absence, and one that returns
`fail` would refuse work for a check nobody wrote. Both land as `error`
via `unrunnable`, which R1 makes non-refusing.
"""

from __future__ import annotations

import logging
import os
import shlex

from kernel.artifacts.aio import aput
from kernel.spec import ExecutionSpec
from kernel.verifier import deps_layout
from kernel.verifier.budget import was_declared
from kernel.verifier.outcome import Outcome, from_exit, unrunnable

logger = logging.getLogger(__name__)

# Where the committed tree lands inside the verifier container. Pushed
# into /tmp, which every image has, because Docker will not create a
# destination directory that is not already there.
TREE_PARENT = "/tmp"
TREE_PREFIX = "verify/"
TREE_PATH = "/tmp/verify"

# F5: where the replayed dependencies land. Same parent as the tree,
# because Docker will not create a destination directory that is not
# already in the image. THE ARCHIVE HAS TWO ROOTS and they are found two
# different ways -- python's by PYTHONPATH, node's by node's own walk up
# from the tree at /tmp/verify. See kernel/verifier/deps_layout.py.
DEPS_PARENT = deps_layout.DEPS_PARENT
DEPS_PATH = deps_layout.PY_PATH

# The Verifier runs as a different uid from the executor. `nobody` on
# Debian — deliberately not a uid the executor could have arranged to own
# something as.
VERIFIER_UID = 65534


async def tree_kwargs(driver, tree_tar: bytes,
                      deps_tar: bytes | None = None) -> dict:
    """How THIS substrate takes the committed tree.

    Two answers, and the driver states which — `TREE_MODEL`, alongside
    `SERVICE_MODEL` and `CAPTURE_MODEL`:

      push    bytes, written into the container before it starts.
              Docker, where `PUT /containers/{id}/archive` exists.
      fetch   an id, pulled from the artifact store by an initContainer.
              Kubernetes, where nothing can be written into a pod before
              it runs.

    ASKED OF THE DRIVER rather than tried and caught, for the reason
    SERVICE_MODEL gives: "this substrate has no pre-start push" and "the
    push failed" are different facts and only one of them is fine.

    Until this existed the Kubernetes driver refused `upload=`, which
    every command criterion passes — so on the substrate this whole
    stage is being built for, EVERY criterion raised, `verify` caught
    it, and every check came back `error`. Non-refusing, correctly, and
    also meaning no story could ever reach DONE.
    """
    if getattr(driver, "TREE_MODEL", "push") != "fetch":
        # `also_upload` OMITTED rather than passed as None when there
        # are no dependencies, because `run_ephemeral_capture` does not
        # take it — and the prepared-mount path uploads a tree slice
        # through exactly this helper.
        kwargs = {"upload": (TREE_PARENT, tree_tar)}
        if deps_tar:
            kwargs["also_upload"] = [(DEPS_PARENT, deps_tar)]
        return kwargs

    # THE TREE BECOMES AN OBJECT, and its id is the only route to it.
    # The store is content-addressed, so the hash names this tree and
    # nothing else, and `list` is withheld from the identity the
    # criterion runs as — which is what lets a criterion be handed its
    # own tree and no way to reach anybody else's.
    fetch = [(TREE_PARENT, await aput(tree_tar))]
    if deps_tar:
        fetch.append((DEPS_PARENT, await aput(deps_tar)))
    return {"fetch": fetch}


def secrets_for(contract: dict) -> dict:
    """F2d - the secrets the contract declares, from the VERIFIER env.

    A DEFENCE, NOT A CONVENIENCE. The Verifier can hold credentials
    the executor never had, and a live-API check is worth something
    precisely because the agent could not reach the API to fake it.
    The contract NAMES them and does not carry them, so a contract
    row is never a secret and a leaked one costs nothing.

    A name that is not set here is simply absent. Failing the
    verification would turn a deployment gap into a refusal, and R1
    is clear that only a check which ran and said no may refuse.
    """
    names = ((contract.get('workspace_spec') or {}).get('secrets') or [])
    present = {n: os.environ[n] for n in names if os.environ.get(n)}
    missing = [n for n in names if n not in present]
    if missing:
        logger.warning('contract declares secrets not set here: %s',
                       ', '.join(missing))
    return present


def check_path(path_prepend: str, deps_tar: bytes | None) -> str:
    """The prepared mounts' executables, then the replay's."""
    parts = [p for p in (path_prepend or "").split(":") if p]
    if deps_tar:
        parts.append(deps_layout.NODE_BIN)
    return ":".join(dict.fromkeys(parts))


# The name three tests already import, and the name E2's batched runner
# needs to be able to say out loud — it resolves the same PATH per check
# inside one container. Kept as an alias rather than renamed at the call
# sites, because a test that greps for a private name is still a test.
_path = check_path


def container_spec(image: str, *, contract: dict, command: list,
                   deps_tar: bytes | None = None,
                   network: str | None = None,
                   extra_env: dict | None = None,
                   memory_mb: int = 1024) -> ExecutionSpec:
    """The container a check runs in. ONE definition, two callers.

    `run_command` below runs one criterion in it; E2's batched runner
    (kernel/verifier/batch.py) runs several. Every decision recorded
    here is about the CONTAINER rather than about the check, which is
    exactly why it survives batching unchanged — and why it is stated
    once. Two copies of it is how the second caller quietly loses the
    network denial or the uid.

    F5: the executor's recorded installs, replayed from the base image
    into a directory and carried here. The install itself happened in a
    networked container that ran no criterion and saw no tree; this one
    still has no network. PYTHONPATH rather than a site-packages
    overwrite, so what the image already provides is not silently
    replaced.

    No binds at all. The tree arrives as a tar pushed into the container
    (or fetched into an emptyDir), so it can reach nothing it was not
    given.

    No network. A criterion that can reach the internet can fetch a
    version of itself that passes, and can exfiltrate the tree it was
    given. F2d's declared services are the one exception and they arrive
    as `network` — an INTERNAL Docker network, or sidecars in this
    criterion's own pod, neither of which has a route out.
    """
    return ExecutionSpec(
        image=image,
        command=command,
        env={**secrets_for(contract),
             # NODE_PATH ALONGSIDE the walk-up, not instead of it. The
             # CommonJS resolver reads it and the ESM resolver does not,
             # so it buys the odd tool that resolves from somewhere
             # outside /tmp and costs nothing; `/tmp/node_modules` being
             # on node's intrinsic path is what actually carries this.
             **({"PYTHONPATH": DEPS_PATH,
                 "NODE_PATH": deps_layout.NODE_DIR} if deps_tar else {}),
             **(extra_env or {})},
        # F2d: the verification's own INTERNAL network when the
        # contract declares services, and no network at all when it
        # does not. Internal permits container-to-container traffic
        # and denies everything outside, so a criterion reaches
        # `db:5432` by name and still cannot fetch a version of
        # itself that passes or exfiltrate the tree it was given.
        network_mode=network or "none",
        user=f"{VERIFIER_UID}:{VERIFIER_UID}",
        memory_mb=memory_mb,
    )


async def run_command(driver, contract: dict, criterion: dict,
                      tree_tar: bytes, timeout: float = 300.0,
                      network: str | None = None,
                      deps_tar: bytes | None = None,
                      extra_env: dict | None = None,
                      path_prepend: str = "",
                      sidecars: list | None = None) -> Outcome:
    """The `command` kind: run it, and the exit status is the answer.

    THE COMMAND COMES FROM THE CONTRACT. Not from a Makefile, not from
    `pyproject.toml`, not from anything in the tree — which is what makes
    a rewritten Make target and a `--ignore` in the repo's config both
    inert. If this ever reads the command out of the tree, F4's
    second defence is gone and the suite that would notice is the one
    being run.
    """
    command = (criterion.get("spec") or {}).get("run")
    if not isinstance(command, str) or not command.strip():
        return unrunnable("a command criterion has no `run`",
                          criterion_id=criterion.get("id"))

    image = contract.get("base_digest") or contract.get("base_image")
    if not image:
        return unrunnable("the contract names no base image",
                          criterion_id=criterion.get("id"))

    # `sh -c` because a criterion is a command line, not an argv. Not
    # split through shlex: the string IS the contract's intent, and
    # splitting it would change what an approved criterion means.
    # `path_prepend` is how a prepared tree's own executables
    # (.venv/bin, node_modules/.bin) resolve bare — PREPENDED in the
    # shell rather than set via Env, because Env would REPLACE the
    # image's PATH and the kernel does not know what that was.
    # A REPLAYED node install puts its executables in
    # `/tmp/node_modules/.bin`, and `jest` or `tsc` named bare in a
    # criterion resolves nowhere without it. LAST, after a prepared
    # mount's own prepends: a repo that built its own node_modules
    # from a committed manifest should win over the ad-hoc replay.
    resolved = check_path(path_prepend, deps_tar)
    spec = container_spec(
        image, contract=contract,
        command=["sh", "-c",
                 (f'export PATH="{resolved}:$PATH"; ' if resolved else "")
                 + f"cd {shlex.quote(TREE_PATH)} && {command}"],
        deps_tar=deps_tar, network=network, extra_env=extra_env)
    # SIDECARS TRAVEL WITH THE CALL, because on a substrate with no
    # networks the declared services are containers in this criterion's
    # own pod rather than peers it reaches by name. Empty on Docker,
    # where `network_mode` above already carries it.
    result = await driver.run_ephemeral(
        spec, timeout=timeout, services=sidecars or None,
        **await tree_kwargs(driver, tree_tar, deps_tar))
    # The budget and whether anybody DECLARED it both travel into the
    # outcome: a run that hits the default is told not to go looking for
    # an environment fault, and is told which field to set instead.
    # `infra_error` is how a substrate says the run ended for a reason
    # that is not the work: a Spot node reclaimed, an image that would
    # not pull. Docker never sets it — a container exits or the call
    # raises — and Kubernetes sets it often. It travels because the
    # alternative is the substrate's failure being recorded as the
    # story's.
    return from_exit(result["exit_code"], result["output"], command,
                     timed_out=result["timed_out"],
                     timeout_s=timeout,
                     timeout_declared=was_declared(contract, criterion),
                     infra_error=result.get("infra_error"))


async def not_implemented(driver, contract: dict, criterion: dict,
                          tree_tar: bytes, timeout: float = 300.0,
                          network: str | None = None,
                          deps_tar: bytes | None = None,
                          extra_env: dict | None = None,
                          path_prepend: str = "",
                          sidecars: list | None = None) -> Outcome:
    kind = criterion.get("kind")
    return unrunnable(f"criterion kind {kind!r} is not implemented",
                      kind=kind, criterion_id=criterion.get("id"))


async def evaluated_on_the_worker(driver, contract: dict, criterion: dict,
                                  tree_tar: bytes, timeout: float = 300.0,
                                  network: str | None = None,
                                  deps_tar: bytes | None = None,
                                  extra_env: dict | None = None,
                                  path_prepend: str = "",
                                  sidecars: list | None = None) -> Outcome:
    """Ec2d — `artifact` is real, and it is not answered from in here.

    It asks whether the run emitted a matching artifact, which is a query
    against `kernel.artifact_manifest`. This loop has a container, a tree
    and a driver, and deliberately no database — `verify.py` is isolated
    from everything stateful, and threading a connection through it for one
    kind would spend that isolation on a check that needs no container at
    all.

    So `kernel.verifier.artifact_kind.evaluate` runs on the worker, where
    the connection already is: at the plan dry-run, and at
    `propose_completion` for a profile with no Verifier. Reaching this
    function means one leaked past both, which is a wiring bug and says so
    rather than passing quietly.
    """
    return unrunnable(
        "an `artifact` criterion is evaluated on the worker against the "
        "artifact manifest, not in the verifier container — reaching this "
        "evaluator means a contract carried one into a path that does not "
        "check it",
        kind="artifact", criterion_id=criterion.get("id"))


# Named in the plan, built by their own increments. Present here so a
# contract using one produces a clear `error` rather than a KeyError, and
# so the list of what is missing is visible in one place.
KINDS = {
    "command": run_command,
    "differential": not_implemented,
    "metric": not_implemented,
    "probe": not_implemented,
    "artifact": evaluated_on_the_worker,
}


def evaluator_for(kind: str):
    """The evaluator for a kind. Unknown kinds are `error`, never `fail`.

    A contract naming a kind this Verifier does not implement is a
    deployment mismatch. Refusing the story for it would blame the work.
    """
    return KINDS.get(kind, not_implemented)
