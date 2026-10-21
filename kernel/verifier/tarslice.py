"""Cut one mount out of a tree archive, and splice its build back in.

WHY SLICES EXIST. The prepare step runs repo-authored shell with network
and root. Given the WHOLE tree, a malicious or gamed setup in one mount
could read every other repository, read the injected held-out checks
(which the executor is never allowed to see), or rewrite another mount's
tests before the checks run. Sliced, a setup sees exactly its own
repository — the blast radius of a hostile `.agentos/setup.sh` is the
repository that committed it.

Slicing is also what makes the cache per-mount: repo B's node_modules
no longer rebuilds because repo A edited a README.

HELD-OUT FILES NEVER ENTER A SLICE. They are excluded going in and
re-asserted from the pristine archive coming out, so no networked
container ever holds them — the executor could otherwise exfiltrate
through its own committed setup what F4 exists to keep from it.

ON A PATH OR ON BYTES. E5: the whole tree stopped being a `bytes` the
day a `large` tier's 50GiB volume made holding it fatal. So every
function here takes either -- `tarstream.opened` decides -- and every
rewriter takes an `out=` path to write through instead of returning the
result. Passing bytes and getting bytes back is unchanged, which is what
keeps the Docker substrate and the corpus validator working.
"""

from __future__ import annotations

import posixpath

from kernel.verifier.tarstream import Sink, copy_member, opened


def names(source) -> set:
    """Every member's normalised path. Headers only; no payload is read."""
    with opened(source) as tf:
        return {posixpath.normpath(m.name) for m in tf}


def _under(name: str, prefix: str, at: str) -> bool:
    """Is this member inside the mount at `at` (or anywhere, when at='')?"""
    base = posixpath.normpath(prefix + at) if at else posixpath.normpath(
        prefix or ".")
    name = posixpath.normpath(name)
    return name == base or name.startswith(base + "/")


def slice_subtree(source, prefix: str, at: str, exclude: set | None = None,
                  out=None):
    """The mount's members only, full paths kept, excluded names dropped.

    Paths are NOT re-rooted: the slice uploads to the same place the full
    tree would, so the setup's cwd and the capture path are identical in
    both worlds and nothing downstream learns which one ran.
    """
    exclude = {posixpath.normpath(n) for n in (exclude or set())}
    sink = Sink(out)
    with opened(source) as src, sink.open() as dst:
        for m in src:
            name = posixpath.normpath(m.name)
            if name in exclude:
                continue
            if _under(name, prefix, at):
                copy_member(dst, src, m)
    return sink.result


def splice(full_tar, captured, prefix: str, at: str, keep: dict | None = None,
           out=None):
    """The full tree, with the build's NEW files overlaid. Sources win.

    THE PRISTINE ARCHIVE TAKES PRECEDENCE BY NAME, and that is the
    load-bearing sentence. The first version replaced the whole mount
    subtree with the capture -- and the capture is CACHED, keyed by
    image + manifest, so a cache hit substituted YESTERDAY'S SOURCES
    for today's commit and the criteria verified code nobody claimed.
    F7 caught it: the environ.get guard failed on lines that no longer
    existed. The verdict row would have said `passed` about a tree the
    author never proposed.

    Overlaying instead means the capture can only ADD what the build
    created (.venv, node_modules) -- never modify or delete a committed
    file. That also closes the setup-tamper vector by construction: a
    setup that edits an approved test contributes nothing, because the
    pristine copy of that test wins the collision.

    `at` and `keep` are retained for the call shape; precedence makes
    the held-out `keep` set implicit (those files are in the pristine
    archive, which always wins).

    THE COLLISION SET IS PATHS, NOT CONTENT -- so this scales with the
    number of files rather than with the size of the tree, which is
    what lets a 50GiB mount through a conductor with a gigabyte.
    """
    _ = at, keep
    kept: set = set()
    sink = Sink(out)
    with opened(full_tar) as src, opened(captured) as cap, sink.open() as dst:
        for m in src:
            kept.add(posixpath.normpath(m.name))
            copy_member(dst, src, m)
        for m in cap:
            name = posixpath.normpath(m.name)
            if name in kept:
                continue
            kept.add(name)
            copy_member(dst, cap, m)
    return sink.result


def only_new(source, pristine_names: set, out=None):
    """The members a build CREATED: everything not in the input slice.

    What an environment cache remembers must be a function of the
    manifest alone -- the sources travelled INTO the prepare container
    and must not travel back out, or the cache resurrects whatever the
    tree happened to contain the day the key was first built.
    """
    pristine = {posixpath.normpath(n) for n in pristine_names}
    sink = Sink(out)
    with opened(source) as src, sink.open() as dst:
        for m in src:
            if posixpath.normpath(m.name) in pristine:
                continue
            copy_member(dst, src, m)
    return sink.result


def restamp(source, uid: int, out=None):
    """Every entry re-owned. The prepare container runs as root; the
    criteria run as the Verifier's uid, and a root-owned tree under a
    nobody criterion is unreadable."""
    sink = Sink(out)
    with opened(source) as src, sink.open() as dst:
        for m in src:
            m.uid = m.gid = uid
            m.uname = m.gname = ""
            copy_member(dst, src, m)
    return sink.result
