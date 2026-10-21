"""The progress file, from the three sides a leg touches it.

E6c's third memory layer. The workspace says what EXISTS, the journal
says what was DONE, and this is the only thing that says what is
FINISHED -- and for a while it was written and never delivered, because
`format_progress_for_brief` had no production caller at all.

WHY THESE THREE LIVE TOGETHER, AND IT IS NOT BECAUSE THEY SHARE A
VARIABLE NAME. They are the only filesystem the leg runner touches, and
two of the three touch it from the WRONG SIDE:

    seed_if_absent     opens `progress_path` where the HARNESS runs
    prompt_for_update  asks the MODEL to write it, inside the sandbox
    read_into_brief    opens `progress_path` where the HARNESS runs

That only works while the two sides are the same filesystem. `run_leg`'s
own docstring says so -- "the shared /workspace mount makes an absolute
path valid for both" -- and that shared mount is a property of running
the leg beside the sandbox, not a property of the path.

WHAT IS TRUE TODAY, measured while splitting this file (plan 11.5e asks
exactly this question, and 11.5f's D2 row inherits it):

  * NOTHING IN PRODUCTION PASSES `progress_path`. `worker_tick` ->
    `_take_leg` calls `run_leg` without it, so it is None and all three
    of these are dead on the serving path.
  * Every caller that DOES pass one passes a WORKSPACE path --
    `os.path.join(workspace, ...)` in the Eb4 and E6c gates, and
    `--progress-path` on `leg_runner`, a CLI only test scripts invoke.

So the answer to 11.5e is: it is a WORKSPACE path, resolved on the
harness's own filesystem. For a pod-run story those are not the same
filesystem, and `seed_if_absent` would create a file on the CONDUCTOR --
the identical defect P2 removed from five places in `envsetup.py`, and
`read_into_brief` would then find nothing and degrade silently, because
both are best-effort by design.

SETTLED BY D2, AND THE ANSWER IS THE FIRST BRANCH -- WITHOUT THE BUG.
11.5e offered two readings: a workspace path (a real bug, which moving
the leg removes) or local staging (never a bug, and a guard would
CREATE one). 11.5l measured it as the first, and the measurement is
narrower than it sounds: `progress_path` is resolved on the harness's
own filesystem AND written on the harness's own filesystem. The two
have never disagreed, because there is only one of them. What made it
look like a defect was that the harness ran in the CONDUCTOR while the
model read the file in the POD -- a split between the writer and the
READER, not between the resolver and the writer.

D2 closes that split rather than guarding it. `leg_runner` runs the leg
in the execution pod's WORKER container, and the workspace claim is
mounted in both containers of that pod -- `k8s_spec.pod_manifest` gives
`mount` to the worker and to the sandbox, which is the whole reason
`fsGroup: 65532` is set on it. So "the two sides are the same
filesystem" stops being a property of running the leg beside the sandbox
and becomes a property of running it in the same POD. No skip, no host
check, no `remote` branch: 11.5e's instruction not to "fix" a legitimate
local write into a skip is followed by not adding one.

AND IT UNCOVERED A SECOND ONE, which was invisible while both sides were
the same process. The two containers are deliberately DIFFERENT UIDS --
worker 65531, sandbox 65532, sharing group 65532 so each can write a
tree the other half owns (`k8s_spec` argues it at SANDBOX_UID). A file
the worker creates under the default 0022 umask is 0644, owner 65531:
group-READABLE and not group-writable. So `seed_if_absent` would have
created exactly the file `prompt_for_update` then asks the MODEL to
write, in a mode the model's uid cannot open for writing -- a seed that
silently forbids the update it exists to invite. `_make_it_the_models_to_
write` below is why that does not happen, and it is a two-uid fact
rather than a file-mode preference: do not "tidy" it away.

STILL NOT LIVE, AND D2 DOES NOT MAKE IT SO. `run_leg` still defaults
`progress_path` to None and the only caller that supplies one is
`leg_runner --progress-path`. That CLI is now the production entry point
rather than a test-only one, so the flag is one deploy argument away
from being live -- but turning a dormant feature on is E6c's wiring, not
D2's, and a phase that moves a runner should not also start writing
files nobody asked for. What D2 owes, and pays here, is that the write
is CORRECT the moment somebody does pass one.

The best-effort handlers are not laziness: a story must not fail to
complete because a note about its own progress could not be written or
read. Do not "simplify" them into raises. What changed is what they SAY:
the swallowed `OSError` used to be able to hide a write landing on the
wrong machine, and after D2 it can only be a local failure on a tree
this process can see -- so it is reported as one, plainly, rather than
as a shrug that covered two very different faults.
"""

from __future__ import annotations

import logging
import os
import stat

from kernel.harness.progress import (
    format_progress_for_brief,
    make_progress_prompt,
    parse_progress_file,
    seed_progress_file,
    should_prompt_progress,
)

logger = logging.getLogger(__name__)


def seed_if_absent(progress_path: str | None) -> None:
    """E6c/Eb4: seed the plan file when there is none.

    `seed_progress_file` existed and nothing called it. E6c requires the
    harness to seed an empty one, and Eb4 names why that is not
    scaffolding: an errand HAS no plan file, and that is the ordinary
    case rather than a gap to paper over. Without this the completion
    block never renders for a profile that does not plan.

    THE WRITE IS LOCAL WHEREVER THE LEG IS, WHICH IS WHAT D2 SETTLES.
    `leg_runner` is the pod's leg entry point now, and the worker
    container mounts the same workspace claim the sandbox does
    (`k8s_spec.pod_manifest` hands `mount` to both) -- so when the leg
    runs there, this writes to the tree the model is editing. Run from
    the conductor, as it still is in every environment that ships today,
    it writes to the conductor's own filesystem and `progress_path` is a
    path resolved on that same filesystem. Neither is a write landing on
    a machine that cannot see it; the defect 11.5e feared was the case
    where the RESOLVER and the WRITER disagreed, and moving the leg is
    what makes them the same process in both shapes.

    So an `OSError` here is a real fault on a real tree -- a full volume,
    a path outside the mount, a read-only claim -- and it is logged as
    one rather than as a shrug that also covered "this went to the wrong
    host".
    """
    if not progress_path:
        return
    try:
        if not os.path.exists(progress_path):
            seed_progress_file(progress_path)
            _make_it_the_models_to_write(progress_path)
            logger.info("seeded an empty progress file at %s", progress_path)
    except OSError as e:
        # Still best-effort -- see the module docstring -- but no longer
        # ambiguous. The path is on THIS process's own filesystem, so
        # this is a failure to write a file we can see, not a write that
        # went to the wrong host.
        logger.warning("could not seed the progress file on this pod's own "
                       "workspace (%s): %s", progress_path, e)


def _make_it_the_models_to_write(progress_path: str) -> None:
    """Group-writable, because the model is a DIFFERENT UID in this pod.

    The harness seeds this file in the worker container (uid 65531) and
    then asks the model to update it from the sandbox (uid 65532). They
    share group 65532 -- `k8s_spec.WORKSPACE_GID`, set as the pod's
    `fsGroup` precisely so each can write a tree the other half owns --
    and under the default 0022 umask the seeded file would be 0644:
    readable by that group and writable only by the worker.

    So without this line the seed is worse than no seed. `read_into_brief`
    would report the empty skeleton as the run's progress for ever, while
    every model attempt to write it failed with EACCES inside the
    sandbox, where the harness never sees the error.

    `chmod` on the existing file rather than a umask change: a umask is
    process-wide and would silently widen every other file this leg
    writes, including ones in `/run/agentos` whose whole point is that
    the sandbox's uid cannot read them.

    Not fatal, and deliberately inside the caller's `OSError` handler's
    reach: a tree that refuses the chmod (a filesystem with no POSIX
    modes, which is every Windows test run) still gets a seeded file.
    """
    try:
        mode = stat.S_IMODE(os.stat(progress_path).st_mode)
        os.chmod(progress_path, mode | stat.S_IWGRP | stat.S_IRGRP)
    except OSError as e:
        logger.warning("progress file seeded but not made group-writable "
                       "(%s): the model's uid may not be able to update it",
                       e)


def prompt_for_update(progress_path: str | None, steps: int,
                      interval: int) -> dict | None:
    """The harness-issued nudge, or None when it is not due.

    HARNESS-ISSUED AND NOT A PROMPT INSTRUCTION, because three agents
    told explicitly to keep notes produced 1 commit, 0 and 0 (E6c). The
    harness has to ask, at the moment it matters.

    This is the one of the three that crosses to the model: it names the
    path and asks for a WRITE, so the sandbox resolves it, not us.
    """
    if not progress_path or not should_prompt_progress(steps, interval):
        return None
    return {"role": "user", "content": make_progress_prompt(progress_path)}


def read_into_brief(progress_path: str | None, payload: dict) -> None:
    """E6c criterion 2: attach completion state to the outgoing brief.

    Mutates `payload` rather than returning, so the caller cannot forget
    the key's name -- `progress_state` is what `loop_rehydrate` looks for
    on the way back in, and the two spellings have to agree.
    """
    if not progress_path:
        return
    try:
        if os.path.exists(progress_path):
            with open(progress_path) as fh:
                payload["progress_state"] = format_progress_for_brief(
                    parse_progress_file(fh.read())
                )
    except Exception as e:                                  # noqa: BLE001
        # Never block the brief on progress state.
        logger.warning("progress state not attached to brief: %s", e)
