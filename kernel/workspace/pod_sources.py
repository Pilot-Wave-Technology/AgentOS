"""Which mount sources a POD-COMPOSED workspace can actually materialise.

`CopyinDriver.acquire` takes two shapes of source: a URI, which it
fetches, and a LOCAL DIRECTORY, which it copies from the filesystem it
is running on. The second one is true on Docker and E0, where the
conductor composes the workspace on its own disk. It is meaningless
under `WORKSPACE_MODEL = "pod"`: the directory is on the conductor and
the workspace is composed by an initContainer in a different container,
on a different node, holding a ReadWriteOncePod claim the conductor
cannot even mount.

WHY THE ANSWER IS A REFUSAL AT ADMISSION AND NOT A FAILURE IN THE POD.
D4 in docs/refactor/decisions/p3-cloud-workspace.md: the mount list is
known before the pod is created, so the fact can be stated then. Let it
through instead and the initContainer copies nothing (or copies whatever
happens to sit at that path inside the image), the pod never becomes
Ready, and what a person reads is a pod event -- which looks like
cluster trouble and sends them to the wrong system entirely. The same
mistake `WORKSPACE_MODEL` itself was introduced to stop, where
`[Errno 13] Permission denied: '/workspace'` was retried every tick for
a capability that could not appear.

GIT MOUNTS ARE NOT CHECKED HERE, deliberately. A local git origin has
the same problem (the corpus harness clones from one), but a remote in
scp form -- `git@github.com:owner/name` -- carries no scheme either, so
a "has no ://" test would refuse the commonest real remote there is.
The corpus case is E0-only and never dispatched to a pod; a wrong
refusal on every ssh remote would not be.
"""

from __future__ import annotations

# `copyin` is the only binding that reads the composing host's own
# filesystem. `git` fetches over the network and `none` establishes
# nothing.
_FILESYSTEM_BINDING = "copyin"


def local_directory_mounts(mounts) -> tuple:
    """The mounts whose source is a path on the conductor, not a URI."""
    return tuple(m for m in mounts
                 if m.binding == _FILESYSTEM_BINDING
                 and m.source and "://" not in m.source)


def local_source_refusal(execution_id: int, stranded) -> str:
    """Why this execution cannot be composed in a pod, naming the mount.

    NAMES THE RESOURCE AND THE PATH, because the fix is a data fix: the
    document has to be uploaded so the grant carries a `gs://` (or
    `artifact://`) URI instead of a path that exists on one machine.
    A message that said only "unsupported source" would leave a person
    looking for a missing feature.
    """
    named = "; ".join(
        f"{m.label} <- {m.source}"
        + (f" (resource {m.resource_id})" if m.resource_id else "")
        for m in stranded)
    return (
        f"execution {execution_id} is granted {len(stranded)} document "
        f"mount(s) from a LOCAL DIRECTORY, and this substrate composes "
        f"the workspace inside the execution's own pod -- it cannot "
        f"mount a path on the conductor. Refused here rather than in "
        f"the pod, where it would read as a pod that never became "
        f"Ready. Re-grant the resource with a gs:// or artifact:// URI. "
        f"Mounts: {named}")
