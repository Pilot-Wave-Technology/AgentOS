"""H1a — the spec for a sandbox that actually executes tools.

Until now `build_spec` returned `python:3.12-slim` running `sleep 3600`: a
container that starts, does nothing, and dispatch called success. The
container carrying the bridge shim existed only in test scripts — eleven of
them, each with a copy of the discovery logic. So a story dispatched
through the Conductor reached a sandbox that could not run a single tool.

WHERE THE SUBSTRATE KNOWLEDGE LIVES. Not here. The first version of this
module inspected the Docker daemon directly, which put substrate-specific
code in the layer whose own docstring says it has no Docker or Kubernetes
imports — and it would have run locally and meant nothing on Kubernetes,
where the workspace is a PVC, the sandbox is a container in the same pod,
and no host path exists to discover.

So the driver answers `sandbox_attachments()` and this module forwards the
answer without reading it. Docker replies with binds built from host paths
it asks the daemon for; Kubernetes will reply with a volume reference. The
conductor stays substrate-agnostic, which is the property that makes the
same code run local and in cloud.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from kernel.drivers import interpreter
from kernel.spec import ExecutionSpec

SHIM_IMAGE = "python:3.12-slim"
BRIDGE_MOUNT = "/app/bridge"
WORKSPACE_MOUNT = "/workspace"
DEFAULT_BRIDGE_PORT = 9100


class SandboxSpecError(Exception):
    """The sandbox cannot be described — usually a missing mount."""


def build_sandbox_spec(
    driver,
    execution_id: int,
    workspace_container_path: str = WORKSPACE_MOUNT,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
    image: str = "",
    workspace_subpath: str = "",
    credential_pointers: dict | None = None,
    timings: dict | None = None,
) -> ExecutionSpec:
    """The spec for a sandbox with the bridge answering.

    `driver` supplies everything substrate-specific. Passing it in rather
    than importing one is what keeps this testable without a daemon and
    honest across substrates.

    `image` is the CONTRACT's base image when there is one. The Verifier
    runs criteria in that image, so a sandbox built from a different one
    starts every run with an exec/verify divergence — the executor works
    in one environment and is judged in another, and F6 would report the
    parity gap for the whole life of the story.

    Defaults to the shim image so a run with no contract still starts.
    A full step to contract-derived configuration is H1b; this is the one
    field that would otherwise make the Verifier wrong from step 0.
    """
    try:
        attach = driver.sandbox_attachments(workspace_container_path,
                                            workspace_subpath)
    except Exception as e:                                  # noqa: BLE001
        raise SandboxSpecError(
            f"the {type(driver).__name__} cannot attach a sandbox: {e}"
        ) from None

    return ExecutionSpec(
        volume_mounts=attach.get("volume_mounts", ()),
        image=image or SHIM_IMAGE,
        # The shim, not a sleep. This one line is the difference between a
        # container that reports success and one that can run a tool.
        #
        # B2 -- `interpreter` owns the runner, because the two substrates
        # used to disagree about its name: this said `python3` and the
        # pod path said `python`, so an image carrying one and not the
        # other worked locally and failed in the cluster. Docker keeps
        # the fallback: the interpreter is shipped out of the KERNEL
        # IMAGE by an initContainer, and a bind mount has no equivalent
        # -- the host checkout holds the bridge, not a built runtime.
        command=interpreter.shim_command(BRIDGE_MOUNT, shipped=False),
        env={
            "BRIDGE_PORT": str(bridge_port),
            "WORKSPACE": workspace_container_path,
            # Ec4d — A POINTER, NEVER A SECRET. This names the directory
            # leases land in; the token is in a file inside it, read at
            # the moment of use. `env_guard.assert_no_credentials` refuses
            # anything credential-SHAPED here, and a path is safe to
            # print, which is exactly why the pointer is what travels.
            #
            # Always set, even when this run holds no identity: an absent
            # directory is a run with no credential, which is a clearer
            # thing for a tool to discover than an unset variable that
            # might mean either that or a misconfiguration.
            **_credentials_pointer(workspace_container_path),
            # Ec4d/F6 — THE VARIABLES A CLOUD SDK ACTUALLY READS.
            # `AGENTOS_CREDENTIALS` above names the directory; it is ours
            # and no SDK has heard of it. `gcloud` reads
            # GOOGLE_APPLICATION_CREDENTIALS, boto reads
            # AWS_SHARED_CREDENTIALS_FILE, and without them a run holds a
            # perfectly good lease that nothing can find.
            #
            # Found by errand 74262e1f, which read its own environment
            # and reported GOOGLE_APPLICATION_CREDENTIALS as absent while
            # the executor prompt was promising "the cloud SDKs are
            # already pointed at it". They were not.
            #
            # PASSED IN, not derived here: which variables to set depends
            # on the CLOUD, and only the caller has the grant that says
            # which cloud this is.
            **(credential_pointers or {}),
            # THE SANDBOX'S BUDGETS, on the Docker path. Kubernetes reads
            # `spec.timings` into its own closed env list; here the
            # sandbox container IS this env, so they merge in. Budgets
            # open nothing, so `assert_no_credentials` has no quarrel
            # with them.
            **(timings or {}),
        },
        timings=dict(timings or {}),
        binds=attach.get("binds", []),
        network_mode=attach.get("network_mode", "bridge"),
        execution_id=execution_id,
        allowed_bind_prefixes=attach.get("allowed_bind_prefixes", []),
    )


def _credentials_pointer(workspace_container_path: str) -> dict:
    """`AGENTOS_CREDENTIALS` for the sandbox, from the one place that
    knows the layout. Imported rather than restated: the lease directory
    is defined in `kernel/workspace/credentials.py`, and a second copy of
    that path here is the shape this codebase keeps paying for."""
    from kernel.workspace.credentials import sandbox_env

    return sandbox_env(workspace_container_path)


def lease_container_path(execution_id: int,
                         workspace_container_path: str = WORKSPACE_MOUNT) -> str:
    """Where the sandbox will find this run's lease. POSIX, always."""
    from kernel.credentials.issue import lease_id_for
    from kernel.workspace.credentials import sandbox_env

    root = sandbox_env(workspace_container_path)["AGENTOS_CREDENTIALS"]
    return f"{root}/{lease_id_for(execution_id)}.json"


# What a pod's WORKER container may inherit from the conductor. An
# allowlist and a narrow one: `assert_no_credentials` refuses anything
# credential-shaped in a spec, and the conductor's environment is full
# of exactly that. The clone bearer does not travel here at all -- it is
# a secretKeyRef on the composing init container, named and never read.
POD_ENV_ALLOWLIST = (
    # WHETHER A FINISHED STORY OWES A REVIEW, and the leg needs it
    # because one of the four paths that finish a story runs HERE.
    # `loop.py`'s autonomous branch -- a standing grant covers the plan,
    # the self-check passed, nobody is asked -- calls `finish_execution`
    # inside the leg loop, which is inside this pod.
    #
    # Without it on this list the flag is half on: the control panel
    # would send accepted stories into the review phase and the pod
    # would finish autonomous ones straight to DONE. Those are precisely
    # the stories nobody looked at, so they are the ones the review
    # matters most for -- and the gap would be invisible, because a
    # story that was never reviewed and one that was reviewed and
    # approved both read as `reviewed` on the board.
    "STORY_REVIEW_IN_POD",
    # WHETHER A FINISHED STORY WAITS FOR ITS MERGE, and it travels for
    # exactly the same reason as the flag above: `finish_execution` is
    # the one place all four "finished" paths converge, `owes_a_landing`
    # is the first question it asks, and one of those paths -- loop.py's
    # autonomous branch -- runs INSIDE this pod.
    #
    # Half on is the failure to picture. The control panel would move
    # stories to `awaiting_merge` and sweep for merges, while the pod
    # finished autonomous runs straight to DONE as before. The story
    # would reach `done` with its PR still open, the workspace would be
    # reclaimed under it, and the board would say the work had landed.
    "STORY_HOLDS_FOR_MERGE",
    # WHICH MODEL THE LEG USES, and it has to travel or the pod
    # picks its own. `pod_main` and `service_tick` share one
    # fallback -- "gemini" -- so a deployment set to `anthropic`
    # would run Anthropic in the conductor and GEMINI IN EVERY POD,
    # with no error and nothing in the log saying which model
    # answered. The same class as the base-image bug below: a
    # fallback that is correct for one process and silently wrong
    # for the other.
    #
    # NOT A SECRET, so a plain value is right here. The KEY it
    # selects is a `secretKeyRef` on the worker container
    # (`k8s_spec.model_key_env`) and must never join this list.
    "AGENTOS_ASSIST_MODEL",
    # WHICH SUBSTRATE THE POD IS ON, and its absence is measured:
    # execution 213 died with `KeyError: 'DOCKER_HOST'`. `registry.ENV`
    # is `AGENTOS_DRIVER` and its DEFAULT is "docker", so a worker that
    # is not told otherwise builds a Docker driver inside a Kubernetes
    # pod and reaches for a socket that is not there.
    #
    # It does not appear in a grep for `os.environ["AGENTOS_..."]`
    # because `registry.driver_name` reads it through a CONSTANT. That
    # is why the audit that found the other two missed this one.
    "AGENTOS_DRIVER",
    # The leg resolves a profile per leg (`profiles.loader`) and writes
    # git credentials under the runtime dir (`workspace.git_bearer`).
    # Both are deployment config, not secrets -- the model KEY stays a
    # secretKeyRef on the worker container alone.
    #
    # THE FLAG FOUR LLM FEATURES GATE ON, and the leg needs it because
    # one of them runs in the leg. `quick.available()` is
    # `GEMINI_API_KEY` AND `AGENTOS_ASSIST == "1"`; the key reaches the
    # pod through `model_key_env` and the flag did not, so
    # `available()` was False in every execution pod and the SCOUT --
    # which reads a repository with no committed setup and drafts one --
    # returned nothing, silently. A repository AgentOS had never seen got
    # no environment, no draft and no diagnosis.
    #
    # THE COMMENT BELOW IS WHY IT STAYED MISSING, so it is corrected
    # rather than deleted: it argued the leg does not need this because
    # "`quick.py` is the ASSESSOR's path and runs in the verifier". True
    # of the assessor and false of `quick.py`, which the scout imports
    # too -- and the scout runs in the leg. One module, two callers, one
    # of them on the other side of the pod boundary.
    "AGENTOS_ASSIST",
    # `AGENTOS_ASSIST_CONTEXT_TOKENS` is still deliberately NOT here. It
    # trips the guard in `test_the_pod_fans_out_over_its_own_pool` --
    # which matches "TOKEN" as a substring and is right to, because a
    # guard that reasons about which TOKENs are credentials is a guard
    # somebody argues with. That one really is the assessor's alone: the
    # leg's budget comes from the model's registry capabilities (LR7).
    "AGENTOS_PROFILES_DIR", "AGENTOS_RUNTIME_DIR",
    # WHICH BUILD THE POD IS RUNNING, for the same reason the two
    # Deployments carry it: without it every line a leg logs says
    # `build_sha: "unset"`. It matters more here than there -- a pod
    # outlives a conductor rollout, so a story can genuinely be running
    # last build's kernel while the conductor runs this one, and that is
    # exactly the question the field exists to answer.
    "BUILD_SHA",
    "ARTIFACT_STORE", "ARTIFACT_BUCKET", "ARTIFACT_PREFIX",
    "AGENTOS_STORE_PROXY", "AGENTOS_WORKSPACE_ROOT",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    # THE WORKER MINTS CONTRACTS, and it is not the conductor.
    #
    # `contracts.mint_first` runs in the WORKER container when a plan is
    # approved, and a contract's `base_image` is what every later
    # dispatch pulls. The conductor had AGENTOS_SANDBOX_BASE_IMAGE from
    # its manifest; the worker's environment is THIS allowlist and
    # nothing else, so `default_base_image()` fell back to E0's literal
    # and the contract was minted naming an image the cluster cannot
    # pull.
    #
    # Watched on NCAL4 run 191: the pod came up 2/2 on the right image,
    # the story planned, the plan was approved -- and the contract
    # minted at that moment sent the NEXT dispatch back to
    # `docker.io/agentos-kernel/fixture:dev`. The fix reached the
    # process that dispatches and missed the process that decides.
    "AGENTOS_SANDBOX_BASE_IMAGE",
    # AND THE DIGEST TRAVELS WITH THE NAME, for that same reason.
    #
    # `contracts.base_digest` has existed since F1 and every READER
    # prefers it, but no INSERT path ever wrote one -- so every contract
    # named a moving tag and every run logged "the contract names an
    # image by TAG, not a digest; the Verifier may not have run the
    # image that was approved". Executions 205, 207, 208, 209 and 210
    # all carried it, and `new_version`'s supersede branch COPIES the
    # column forward, so one NULL propagates through every amendment.
    #
    # `image_digest.resolve` reads this variable, and it is the WORKER
    # that mints the contract. Setting it on the conductor alone would
    # repeat run 191 exactly: the fix reaching the process that
    # dispatches and missing the process that decides.
    "AGENTOS_SANDBOX_BASE_DIGEST",
)


async def spec_for_dispatch(conn, driver, execution_id: int, *,
                            bridge_port: int, image: str = "",
                            workspace_subpath: str = "",
                            credential_pointers: dict | None = None):
    """The spec this substrate dispatches, whichever shape that is.

    THE DISPATCH PATHS ALL ASSUMED DOCKER. `build_sandbox_spec` asks the
    driver for `sandbox_attachments` -- which host paths, which volume,
    which network namespace the sandbox should join -- and Kubernetes
    refuses that by name, because a pod's containers are declared
    together and there is nothing to attach after the fact. The refusal
    worked exactly as written: the first errand to reach dispatch on the
    cluster failed with "A caller needing one has assumed the Docker
    process model", every tick, and sat at ADMITTED.

    This is that caller, fixed once rather than at each of the three
    sites the comment in `dispatch_pass` already warns about -- "each one
    has to agree; a single wide one gives some runs the reach the others
    were denied".

    On a pod substrate the EXECUTION is the unit: one pod holds worker
    and sandbox, the workspace claim is its own, and `build_spec` is the
    builder -- the same one that carries the size rung and the mount
    list the init container composes from.
    """
    # WHICH CLOCKS THIS RUN OBEYS, resolved here and STAMPED.
    #
    # Same place and the same reason as the base image and the executor
    # model below: this is the async caller holding both the connection
    # and the story id. Resolved ONCE -- three of these knobs are
    # enforced inside the sandbox, which has no database, so they can
    # only ever be literals placed here; and stamping the execution means
    # a TTL lapsing mid-run cannot leave the run half-compressed.
    from kernel import timings as _timings                  # noqa: PLC0415
    from kernel import timings_store                        # noqa: PLC0415

    # NO CONNECTION READS AS PRODUCTION, the direction every other reader
    # of this setting takes. A caller with no connection is a local
    # dispatch or a test harness, and a missing settings read must never
    # be the reason a run cannot start -- `for_story` already answers
    # production for an absent row, an expired one and an unreadable
    # table, and this is the fourth case of the same rule.
    story_id = None
    if conn is not None:
        story_id = await conn.fetchval(
            "SELECT story_id FROM executions WHERE id = $1", execution_id)
    profile, overrides = ((_timings.PRODUCTION, {}) if conn is None
                          else await timings_store.for_story(conn, story_id))
    sandbox_timings = _timings.sandbox_env(profile, overrides)
    worker_timings = _timings.worker_env(profile, overrides)
    if conn is not None:
        await timings_store.stamp(conn, execution_id, profile)

    if getattr(driver, "WORKSPACE_MODEL", "local") != "pod":
        return build_sandbox_spec(
            driver, execution_id, bridge_port=bridge_port, image=image,
            workspace_subpath=workspace_subpath,
            credential_pointers=credential_pointers,
            timings=sandbox_timings)

    from kernel.conductor.spec_builder import build_spec  # noqa: PLC0415
    from kernel.workspace.mounts import mounts_for        # noqa: PLC0415

    row = await conn.fetchrow(
        # `e.story_id` because a POD composes its own workspace and the
        # git broker scopes credentials to the story. The conductor's
        # own path reads it separately (runner.py, the Docker branch);
        # in a pod there is nothing to read it from later.
        "SELECT e.id, e.kind, e.domain, e.goal_id, e.story_id, "
        "e.contract_id FROM executions e WHERE e.id = $1", execution_id)
    contract = None
    if row and row["contract_id"]:
        found = await conn.fetchrow(
            "SELECT * FROM contracts WHERE id = $1", row["contract_id"])
        contract = dict(found) if found else None

    # WHICH RUNG, resolved before the spec is built because it sizes the
    # sandbox. Same async caller, same reason as the model below: this is
    # the one place holding the connection, the story and the goal.
    #
    # NEVER FATAL, for the reason the model block gives at length: a run
    # that cannot resolve a size is a run at the profile's size, which is
    # what it would have used before this existed. Refusing to dispatch
    # over a settings lookup turns a preference into an outage.
    size_choice = None
    try:
        from kernel.conductor import size_choice as _sz    # noqa: PLC0415
        size_choice = await _sz.chosen_for_story(
            conn, row.get("story_id"), row.get("goal_id"))
    except Exception:                                       # noqa: BLE001
        logger.warning("execution %s: could not resolve a machine size; "
                       "the profile's applies", row.get("id"),
                       exc_info=True)

    spec = build_spec(dict(row), list(POD_ENV_ALLOWLIST),
                      contract_row=contract,
                      mounts=await mounts_for(conn, execution_id),
                      size_choice=size_choice)
    # The contract's image is the SANDBOX's -- the environment the work
    # runs in and the Verifier judges it in. The worker takes the
    # kernel's, set in the manifest, because it serves workspace
    # operations and those are kernel code.
    #
    # AND THE GOAL'S CHOICE APPLIES BEFORE A CONTRACT EXISTS. A story's
    # FIRST dispatch has no contract -- the plan is not approved yet --
    # so `build_spec` fell through to the deployment default and a goal
    # that had named a Rust or Go image was ignored for exactly the leg
    # that reads the tree and writes the plan. Resolved here because
    # this is the async caller that holds both the connection and the
    # goal id; the contract still wins where it says anything.
    if not (contract or {}).get("base_digest")             and not (contract or {}).get("base_image"):
        from kernel.base_image import for_goal              # noqa: PLC0415
        chosen = await for_goal(conn, row["goal_id"])
        if chosen:
            spec.image = chosen
    if image:
        spec.image = image

    # B7 -- AND THE POD IS TOLD WHICH IMAGE IT GOT. Both readers of the
    # environment text run inside the pod and neither holds a connection
    # or a goal id, so `for_goal` is not callable from either; this is
    # the one place that has finished deciding. Written last, after the
    # contract, the goal and the deployment default have all had their
    # say, because an earlier value would describe an image the run does
    # not use.
    #
    # An ENV VAR and not a query, and the difference is priced: the text
    # sits in the stable prefix, and anything time-varying there turns
    # every step into a cache miss. Fixed for the life of the pod.
    from kernel.env_text import ENV_VAR as _IMAGE_IN_USE  # noqa: PLC0415

    spec.env[_IMAGE_IN_USE] = spec.image or ""
    spec.timings = sandbox_timings
    # The worker's two, on the container that has `run_job`. Budgets,
    # so `assert_no_credentials` has no quarrel with them.
    spec.env.update(worker_timings)

    # WHICH MODEL THIS RUN'S LEGS USE, resolved here and FROZEN.
    #
    # Same place and the same reason as the base image above: this is
    # the async caller holding both the connection and the goal id. The
    # order is the story's choice, then the goal's, then the deployment
    # default -- and `freeze` writes it to `executions.executor_model`
    # under `WHERE executor_model IS NULL`, so a second dispatch pass
    # cannot rewrite a running story's model.
    #
    # "Once dispatched for a story it cannot be changed." Editing the
    # goal or the story afterwards changes the NEXT run. A leg that
    # changed model mid-story would make its own step journal
    # unreadable -- half the reasoning from one model, half from
    # another, and the cost attribution wrong for both.
    #
    # NEVER FATAL. A run that cannot resolve a model is a run that uses
    # the deployment default, which is what it would have used before
    # this existed. Refusing to dispatch over a settings lookup would
    # turn a preference into an outage.
    try:
        from kernel.harness.models import choice            # noqa: PLC0415
        # `chosen_for_story`, NOT `for_story`: only a choice a person
        # actually made is frozen. `for_story` appends the deployment
        # default, and freezing that would make every run look
        # deliberately configured -- and would override a profile's own
        # model with a default nobody picked. None writes nothing.
        model = await choice.chosen_for_story(conn, row.get("story_id"),
                                              row.get("goal_id"))
        await choice.freeze(conn, row["id"], model)
    except Exception:                                       # noqa: BLE001
        logger.warning("execution %s: could not resolve a model; the "
                       "deployment default applies", row.get("id"),
                       exc_info=True)

    # AND THE RUNG IS PINNED THE SAME WAY, under `WHERE sandbox_size IS
    # NULL`, so editing the goal or the story afterwards changes the NEXT
    # run and not this one. A leg that changed machine mid-story would
    # make its own timings unreadable, which is the same argument the
    # model freeze makes one comment up.
    try:
        from kernel.conductor import size_choice as _sz    # noqa: PLC0415
        await _sz.freeze(conn, row["id"], size_choice)
    except Exception:                                       # noqa: BLE001
        logger.warning("execution %s: could not pin the machine size",
                       row.get("id"), exc_info=True)

    # AND THE GOAL'S LEG SIZE AND LEG COUNT, pinned the same way and for
    # the same reason: editing the goal changes the next run, not this
    # one. Never fatal -- the defaults are what it would have run with.
    try:
        from kernel.harness.models import leg_choice        # noqa: PLC0415
        await leg_choice.freeze(
            conn, row["id"],
            await leg_choice.chosen_for_goal(conn, row.get("goal_id")))
    except Exception:                                       # noqa: BLE001
        logger.warning("execution %s: could not pin the goal's leg "
                       "settings; the defaults apply", row.get("id"),
                       exc_info=True)
    return spec
