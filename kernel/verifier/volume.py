"""Where a verification's PVC is mounted, and what lives on it.

ONE DECLARATION, for the reason `deps_layout` is one: the prepare Job
WRITES this layout and the verification pod READS it, they are built by
different code, and a path spelled twice is a path that disagrees once.
`k8s_egress` is the same lesson learned the expensive way -- the label
and the port were chosen in different files and nothing made them agree.

WHY A VOLUME AT ALL. Until Phase E the tree travelled as a tar through
the artifact store: `git archive` -> `io.BytesIO` -> GCS -> a capture
sidecar printing an id -> a criterion Job unpacking it. FOUR of the five
faults of 2026-09-17 were in that path and none of them was about
verification:

    8   the capture pod had no route to the metadata server
    9   its service account had no Workload Identity binding
    10  the id came back unreadable -- a `str` holding a bytes repr
    12  a venv's absolute interpreter symlink refused by the tar filter

A volume has no metadata server, no token, no marker to parse and no
tar. Those four stop being fixed and start being impossible.

It also removes a ceiling nobody declared: `archive` holds the WHOLE
tree in memory, which is fine at the 45MB measured on 210 and is not
fine on a `large` tier with a 50GiB disk.

THE TREE AND THE ENVIRONMENT HAVE DIFFERENT LIFETIMES, which is why
they are separate directories rather than one.

  * the ENVIRONMENT is keyed on the committed manifest hash. It survives
    every generation that does not change the manifest -- and 203 ran
    five generations, so that is most of them.
  * the TREE is replaced on every round, because every round is a new
    commit.

Keeping them apart is what lets a refusal re-seed `TREE` and leave
`ENV` alone. Putting them together would mean rebuilding a `.venv`
because a source file changed, which is the cost this whole design
exists to remove.
"""

from __future__ import annotations

# The PVC's mountpoint inside both the prepare Job and the pod. Not
# `/workspace` (k8s_spec) and not `/tmp/verify` (kinds.TREE_PATH): this
# is a third thing, and reusing either name would make one of them mean
# two things depending on which pod you are in.
MOUNT = "/verify"

# The committed tree the criteria run against. Replaced every round.
TREE = f"{MOUNT}/tree"

# What `setups.REGISTRY`'s command builds -- `.venv`, `node_modules`.
# Keyed on the manifest hash, kept across rounds. See `stamp`.
ENV = f"{MOUNT}/env"

# The manifest hash the ENV directory was built from, written by the
# prepare Job and read by anything deciding whether ENV is stale.
#
# ON THE VOLUME AND NOT ONLY IN THE DATABASE, deliberately. The pod can
# read a file; it holds no database credential and must not gain one to
# answer "is my own environment current". The database row is the
# scheduler's copy; this is the volume's own account of itself, and when
# they disagree the volume is right -- it is the thing that actually has
# the bytes.
STAMP = f"{ENV}/.agentos-manifest-hash"


def stale(stamped: str | None, wanted: str) -> bool:
    """True when the ENV on the volume is not the one we need.

    An ABSENT stamp is stale, which is the safe direction: a volume
    whose environment we cannot identify gets rebuilt, and rebuilding an
    environment that was already correct costs a prepare Job. Trusting
    it instead would verify against an unknown `.venv` -- and a stale
    environment lets a check PASS THAT SHOULD FAIL when a dependency was
    removed, which is the failure direction this cache must never take.
    """
    return not stamped or stamped != wanted
