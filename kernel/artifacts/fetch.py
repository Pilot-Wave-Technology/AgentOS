"""Pull artifacts out of the store and unpack them. An entry point.

    python -m kernel.artifacts.fetch <dest> <artifact-id> [<dest> <id> ...]

WHY THIS EXISTS. On Docker the Verifier pushes the committed tree into
the criterion's container before it starts — `PUT /containers/{id}/
archive`, which is what `run_ephemeral(upload=...)` is. Kubernetes has no
equivalent: a Job's pod cannot be written to before it runs, so the tree
has to ARRIVE rather than be delivered. It arrives here, in an
initContainer that runs before the criterion and writes into an emptyDir
they share.

THE HASH IS THE CAPABILITY, and this is the other half of that. The
store is content-addressed, so the id names exactly one tree and nothing
else; `list` is withheld from the identity this runs under, so holding
the id is the only way to reach an object. That is what lets a criterion
be given its own tree and no route to anyone else's.

AND THE ID IS RE-CHECKED, in `store.get` rather than here. The
Verifier's whole claim is that the criterion ran against the COMMITTED
tree, and that holds only if somebody hashes the bytes that came back.
Every binding does, so the same guarantee covers the control panel and
the `artifact` criterion — see kernel/artifacts/store.py:verified.

A FAILURE HERE IS AN INFRA ERROR, AND FALLS OUT FOR FREE. An
initContainer that exits non-zero leaves the criterion container in
`PodInitializing`, which kernel/drivers/k8s_outcome.py classifies as
`never started` — `error`, not `fail`. A tree that could not be fetched
must never be recorded as the work being wrong.

NO CLOUD SDK AT IMPORT. `get_store()` builds the binding lazily for the
reason kernel/artifacts/gcs.py gives: E0's image has none and E5's
module-load check imports every module under kernel/.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile


def unpack(artifact_id: str, dest: str) -> int:
    """Fetch one artifact and extract it into `dest`. Returns its size.

    The id is re-checked, and NOT here: `store.get` re-hashes every
    read, so the guarantee holds for the control panel and the
    `artifact` criterion as well as for this. Checking again in this
    module would be a second declaration of the same fact.
    """
    from kernel.artifacts.store import get_store

    data = get_store().get(artifact_id)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        # `data` REFUSES what an archive should never carry into a
        # directory: absolute paths, `..` escapes, links pointing out of
        # the tree, setuid bits. The tar is one we built, but it is built
        # FROM A REPOSITORY, and a repository is exactly the kind of
        # place a hostile path lands in. The executable bit survives the
        # filter, which the prepared tree needs.
        tar.extractall(dest, filter="data")
    return len(data)


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or len(args) % 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2

    for dest, artifact_id in zip(args[::2], args[1::2]):
        try:
            size = unpack(artifact_id, dest)
        except Exception as exc:                            # noqa: BLE001
            # NAMED, and on stderr. This is the only place a person sees
            # why a criterion never started — the pod is gone by the
            # time anyone looks, and the Job's own status says nothing
            # beyond "the init container failed".
            print(f"fetch: could not unpack {artifact_id[:12]} into {dest}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"fetch: {artifact_id[:12]} -> {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
