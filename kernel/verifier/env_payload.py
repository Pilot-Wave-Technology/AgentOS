"""The install inputs that NAME an environment, and the hash over them.

WHAT USED TO BE HERE AND IS NOT. This module also built the tar.gz the
prepare Job carried in its own command, capped at 256KB by etcd's object
limit, because the Job was assumed to run before the tree was seeded
(5.4 fires prepare at step 3 and the tree lands at step 6). E8 named the
cost honestly and it was a REGRESSION: `.agentos/setup.sh` used to
receive the whole mount slice and received only itself, so a script that
reads a sibling, or a `requirements.txt` carrying `-e .` or
`-r base.txt`, failed. Execution 207 wrote a `setup.sh`.

PLAN 5.15 RETIRED THE PAYLOAD AND THE CAP TOGETHER. The prepare Job is
NETWORKED and always was -- zero egress is a property of the criteria
pod, and prepare runs no criterion -- so it pulls the committed tree
onto `volume.TREE` itself (`prepare_fetch`) and installs against a real
one. Nothing travels in the command any more.

WHAT SURVIVES IS THE KEY, and it is still computed from exactly these
files: `.agentos/setup.sh`, `.agentos/env`, and every manifest
`setups.REGISTRY` knows, per mount, plus the CONTRACT's image. That set
is what an install READS, so it is what the cached `volume.ENV` is named
after.

COMPANIONS COUNT EVEN WHEN THEY LOSE. `setups._mount` drops
`package.json` when a lockfile is present -- correctly, because the
lockfile's command supersedes it -- but `pnpm install --frozen-lockfile`
READS package.json. It is an input to the install, so it is an input to
the key, and a commit that edits it must rebuild.

AND THAT IS ALSO WHY THE KEY IS NOT `setups.fingerprint`. Fingerprint
hashes ONE MOUNT's chosen setups; this names the WHOLE volume's `ENV`
directory, which one stamp covers, and it must move when anything the
install reads moves. Same material, wider scope. `fingerprint` stays the
key of the tar path it was written for; nothing here re-spells the
COMMAND, which is `REGISTRY`'s and is read straight off the setups.
"""

from __future__ import annotations

import hashlib
import posixpath

from kernel.verifier.setups import ENV_FILE, REGISTRY, SETUP_FILE
from kernel.verifier.tarstream import opened

# Every committed file an install may read, and therefore every file the
# cache key is computed over. A CLOSED LIST: a manifest this does not
# know about cannot move the key, so a repository whose install reads an
# unlisted file would keep a stale environment across a real change --
# which is 5.9's dangerous direction, a check passing that should fail.
WANTED = (SETUP_FILE, ENV_FILE, *(row["manifest"] for row in REGISTRY))

# AND EVERY OTHER FILE THE SETUP MIGHT READ.
#
# `WANTED` above is a list of NAMES, and the environment's identity was
# the bytes of those names and nothing else. `.agentos/setup.sh` is
# arbitrary shell: a setup that reads anything beside itself --
#
#     pip install -r requirements/$(cat .agentos/python-version).txt
#
# -- changes what it installs without changing a byte this hash covers.
# The key stays the same, `prepare_script` step 1 finds the stamp
# current, skips the install, and the run is verified against an
# environment built from a manifest that no longer exists.
#
# WIDENED TO THE DIRECTORY, not to the tree. Everything a setup is
# entitled to read about its own environment belongs under `.agentos/`
# by convention, and hashing the whole tree would rebuild the
# environment on every source edit -- which is the cost the manifest
# hash exists to avoid.
#
# IT NARROWS THE HOLE RATHER THAN CLOSING IT. A setup reading a file
# OUTSIDE `.agentos/` still escapes, and nothing static can catch that:
# the input is arbitrary shell. `rebuild.cold_due` is the other half --
# a from-scratch rebuild every few rounds, so an undetected drift cannot
# persist indefinitely.
AGENTOS_DIR = posixpath.dirname(SETUP_FILE)


def collect(tree, ats, prefix: str = "verify/") -> dict:
    """{(at, relpath): bytes} -- every install input in the COMMITTED tree.

    From the archive and never a working copy, for the reason
    `setups.from_tar` gives and 5.10 restates: the executor's `env_setup`
    reads the working tree, so a model that edits `requirements.txt`,
    runs `env_setup` and does not commit has an environment the verifier
    can never reproduce.

    `tree` IS A PATH OR BYTES, whichever the caller has. `tarstream.opened`
    decides, and a path is opened rather than loaded -- E5's rule, because
    a `large` tier's tree is a killed process rather than a slow one.
    Only these few members are ever read into memory.
    """
    wanted: dict[str, tuple[str, str]] = {}
    # `<prefix><at>/.agentos/` per mount, so any file under it counts
    # towards the environment's identity -- see AGENTOS_DIR above.
    dirs: list[tuple[str, str, str]] = []
    for at in sorted(set(ats)):
        base = prefix + (at or "")
        for rel in WANTED:
            name = posixpath.normpath(posixpath.join(base, rel))
            wanted[name] = (at or "", rel)
        root = posixpath.normpath(posixpath.join(base, AGENTOS_DIR))
        dirs.append((root + "/", at or "", posixpath.normpath(base)))
    found: dict[tuple[str, str], bytes] = {}
    with opened(tree) as tf:
        for member in tf:
            if not member.isfile():
                continue
            norm = posixpath.normpath(member.name)
            where = wanted.get(norm)
            if where is None:
                for root, at, base in dirs:
                    if norm.startswith(root):
                        where = (at, norm[len(base):].lstrip("/"))
                        break
            if where is not None:
                found[where] = tf.extractfile(member).read()
    return found


def key(image: str, files: dict) -> str:
    """The name of the environment these files and this image build.

    THE IMAGE IS IN IT because the contract's image IS the environment's
    other half: the same `requirements.txt` against two interpreters is
    two `.venv`s, and a moved tag that served yesterday's interpreter to
    today's tree is what the tar cache's TTL existed to bound. Here the
    image is hashed instead, so a changed image is a changed key and
    there is nothing to expire.
    """
    h = hashlib.sha256()
    h.update(image.encode())
    for at, rel in sorted(files):
        h.update(b"\x00" + at.encode() + b"\x00" + rel.encode())
        h.update(b"\x00" + files[(at, rel)])
    return h.hexdigest()
