"""Build an ExecutionSpec from an execution row and a configurable env allowlist.

The allowlist is deliberately a permit list, not a blanket copy — the Conductor
holds DATABASE_URL and API keys in its own environment (S8). H1b replaces
the source with contract-derived config; until then, the allowlist is the
stopgap. assert_no_credentials catches a misconfigured allowlist at build
time rather than at container inspection time.

This module has NO Docker or Kubernetes imports — only kernel.spec.ExecutionSpec.
"""

from __future__ import annotations

import logging
import os

from kernel.conductor import contract_config
from kernel.conductor.env_guard import assert_no_credentials
from kernel.profiles.registry import resolve
from kernel.base_image import default_base_image
from kernel.sizes import resolve as resolve_size
from kernel.spec import ExecutionSpec

logger = logging.getLogger(__name__)


def build_spec(
    execution_row,
    env_allowlist: list[str],
    source_env: dict[str, str] | None = None,
    contract_row=None,
    mounts=(),
    size_choice: str | None = None,
) -> ExecutionSpec:
    """Build an ExecutionSpec for dispatching an execution.

    Args:
        execution_row: asyncpg Record with at least id, kind, domain, goal_id.
        size_choice: the rung a person set on this story or its goal, or
            None when nobody did. Beaten by a contract's own size and
            beats the profile's.
        env_allowlist: list of env var names to copy from source_env.
            Only keys present in BOTH the allowlist and source_env are included.
        source_env: the environment to draw from. Defaults to os.environ.

    Returns:
        ExecutionSpec with image = the contract's, else the
        deployment's sandbox base image; command=['sleep','3600'],
        env=filtered allowlist, allowed_bind_prefixes=[].
    """
    if source_env is None:
        source_env = dict(os.environ)

    # E6f: resolve the profile here, at dispatch, so an execution whose
    # (kind, domain) matches nothing fails BEFORE a container exists rather
    # than when a leg starts and finds no surface. This function documented
    # both fields in its signature and read neither.
    #
    # The resolved profile is not yet used to choose the image — that is
    # H1b's contract-derived configuration. Resolving without consuming is
    # deliberate and narrow: it makes an unknown type a dispatch-time error
    # today, and gives H1b one place to read from later.
    profile = resolve(execution_row["kind"], execution_row["domain"])
    logger.info(
        "execution %s dispatches under profile %s/%s (%d tools, driver=%s)",
        execution_row["id"], profile.kind, profile.domain,
        len(profile.tools), profile.workspace_driver,
    )

    # Build env by copying only allowlisted keys present in source_env.
    env = {
        key: source_env[key]
        for key in env_allowlist
        if key in source_env
    }

    # THE POD HAS NO CREDENTIAL, SO IT CANNOT USE THE BUCKET.
    #
    # The conductor runs ARTIFACT_STORE=gcs, and copying that into the
    # pod hands the leg a store it can never reach: `storage.Client()`
    # takes its token AND its project id from 169.254.169.254, and the
    # execution pod is denied the metadata server ON PURPOSE. Its
    # sandbox shares the pod's network namespace, so granting it there
    # would let model-authored code mint a Workload Identity token for
    # `agentos-execution` -- `deploy/k8s/conductor.yaml` argues this at
    # length and records the measured failure, `metadata_exit=28`.
    #
    # That was harmless while `emit_artifact` was the only artifact
    # writer: it is a worker-side tool, so the CONDUCTOR read the file
    # out of the pod and wrote the store with its own identity, which is
    # what that same comment says the pod does not need one. The
    # leg-in-pod cutover made `harness/loop_tool` a writer too, INSIDE
    # the pod, and nobody came back here.
    #
    # Measured on execution 222: a 133 KB `git log -p` crossed the 32 KB
    # spill threshold, `storage_client()` raised OSError, and the leg
    # died. Six generations, each rehydrating and repeating the same
    # opening moves, none reaching the file the story asked for.
    #
    # SO THE POD GETS THE FILESYSTEM BINDING, rooted on the workspace it
    # already has. RUN-SCOPED, and that is the honest limit: the claim is
    # that a leg may re-read its own oversized output WHILE IT WORKS, not
    # that those bytes outlive the pod. `store.UnknownBinding` warns
    # about the silent version of this -- "a journal full of result_refs
    # pointing at nothing" -- which is why the root is the mounted
    # workspace and not `/artifacts` in a container about to be
    # destroyed.
    env["ARTIFACT_STORE"] = "filesystem"
    env["ARTIFACT_ROOT"] = (
        (env.get("AGENTOS_WORKSPACE_ROOT") or "/workspace").rstrip("/")
        + "/.agentos/artifacts")
    # A bucket name the pod cannot use is a trap for the next reader.
    for key in ("ARTIFACT_BUCKET", "ARTIFACT_PREFIX"):
        env.pop(key, None)

    # Raise on credential keys — never silently filter.
    assert_no_credentials(env)

    # H1b: THE CONTRACT OVERRIDES, THE ALLOWLIST REMAINS THE FALLBACK.
    #
    # Not a replacement. A run with no contract — an errand, or a story
    # before approval — still needs an image, so removing the fallback
    # would make dispatch depend on a row that does not exist yet. What
    # changes is that when a contract HAS said something, it wins, and
    # the Conductor's own environment stops being the only source.
    #
    # A refusal here is deliberate and loud: `check_budget` raises rather
    # than clamping, because a budget too small to orient produces a run
    # that reads, searches, runs out and writes nothing — which reads as
    # a model that would not work.
    overrides = contract_config.overrides_from(contract_row)
    if overrides:
        logger.info("execution %s takes %s from its contract",
                    execution_row["id"], sorted(overrides))

    # ── I7: THE RUNG, WHICH NOTHING USED TO SET ──────────────────────
    #
    # This function returned a spec carrying image, command, env,
    # execution_id and allowed_bind_prefixes -- and nothing else. So
    # every execution took the dataclass defaults, 1 vCPU and 512Mi and
    # a 50 GiB disk, whether it was an errand summarising a branch or a
    # coding story on a monorepo. The profile knew which it was and was
    # never asked.
    #
    # `cpus` and `memory_mb` are THE SANDBOX's, on every substrate: on
    # Docker the sandbox is the only container, and on Kubernetes the
    # worker is given kernel.sizes.WORKER_* beside it. See kernel/sizes.py
    # for why sizing them together is what made small runs expensive.
    # THE PRECEDENCE, and the middle term is the one that was missing:
    # contract -> story/goal -> profile -> small. A contract knows about
    # the particular repository and outranks everything; `size_choice` is
    # what a person set on the story or the goal, and it matters most on
    # a story's FIRST dispatch, which has no contract at all because its
    # plan is not approved yet. See `kernel/conductor/size_choice.py`.
    size = resolve_size(overrides.get("size") or size_choice or profile.size)
    logger.info("execution %s runs %s: sandbox %sx%sMi, %dGi disk, "
                "node class %s",
                execution_row["id"], size.name, size.cpus,
                size.memory_mb, size.volume_gib, size.name)

    return ExecutionSpec(
        # THE FOURTH DECLARATION OF THIS FACT, and the one exec-189
        # actually took. A run with NO contract -- an errand, or a story
        # before its plan is approved, which is every story's first
        # dispatch -- never reaches `contracts.mint_first`, so fixing the
        # default there fixed nothing for it. It got `python:3.12-slim`:
        # no node, no npm, and a frontend story that wrote its tests and
        # then could not run them.
        #
        # `default_base_image()` is the deployment's, so the no-contract
        # path and the contract path name the same environment.
        image=overrides.get("image") or default_base_image(),
        command=["sleep", "3600"],
        env=env,
        execution_id=execution_row["id"],
        # `or ""` on the VALUE, not a default for a missing key: an
        # errand proposed before its story exists has the column and a
        # NULL in it, and str(None) would send the broker "None".
        story_id=str(execution_row["story_id"] or "")
        if "story_id" in execution_row else "",
        allowed_bind_prefixes=[],
        cpus=size.cpus,
        memory_mb=size.memory_mb,
        volume_gib=size.volume_gib,
        # THE TIER IS ALSO THE MACHINE, and until now nothing said so.
        # `node_class` had a default and no writer, so every execution
        # asked for the default pool whatever tier it had been given: a
        # medium story took a medium-sized pod and then landed on
        # whichever node had room, usually beside two others. Sizing the
        # pod and placing the pod were separate decisions made by
        # different code, and only one of them was ever made.
        node_class=size.name,
        workspace_mounts=tuple(mounts),
    )
