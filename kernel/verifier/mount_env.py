"""`.agentos/env` — what a mount may tell the criterion container.

SPLIT OUT OF prepare.py, which had reached its line ceiling and now has
two paths to host rather than one. The allowlist and the parser are
shared by both and belong to neither:

  the tar path       `mount_environment` reads the file out of a
                     captured archive, and learns which `bins`
                     directories exist by looking for them in it.
  the volume path    `parse_env` is handed the committed bytes
                     `env_payload.collect` already has, and the bins
                     are DERIVED from the declared volume layout
                     (`prepare_script.bin_paths`) rather than
                     discovered. Nothing there re-reads a tar.

`.agentos/env` IS ALLOWLISTED. It exists so a setup can say where its
executables and caches live — not to set LD_PRELOAD in the container
that decides whether the work is done. Unknown keys are dropped and said
out loud in the report.
"""

from __future__ import annotations

import io
import posixpath
import tarfile

from kernel.verifier.kinds import TREE_PATH
from kernel.verifier.setups import ENV_FILE
from kernel.verifier.tarslice import names

# What a committed .agentos/env may set in the check container. PATH is
# the point; the rest are the conventional "where do my packages live"
# variables. LD_PRELOAD and friends are exactly what this list is for.
ALLOWED_ENV_KEYS = {"PATH", "VIRTUAL_ENV", "PYTHONPATH", "NODE_PATH",
                    "GOMODCACHE", "GOFLAGS", "CARGO_HOME", "BUNDLE_PATH"}


def parse_env(text: str, base: str) -> tuple[list, dict, list]:
    """(path_prepends, env, dropped_keys) from one `.agentos/env`.

    `base` is what `$TREE` means to THIS mount, and it is a parameter
    because the two paths disagree about it: the tar path unpacks under
    `kinds.TREE_PATH` inside a criterion container, the volume path
    reads the tree at `volume.TREE`. A single spelling would silently
    point one of them at a directory that does not exist.
    """
    prepends, env, dropped = [], {}, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').replace("$TREE", base)
        if key not in ALLOWED_ENV_KEYS:
            dropped.append(key)
            continue
        if key == "PATH":
            prepends.append(value)
        else:
            env[key] = value
    return prepends, env, dropped


def mount_environment(tar_bytes: bytes, at: str, bins,
                      prefix: str = "verify/",
                      base_path: str = TREE_PATH) -> tuple[list, dict, list]:
    """(path_prepends, env, dropped_keys) one mount's build asks for."""
    base = posixpath.join(base_path, at) if at else base_path
    member_names = names(tar_bytes)
    prepends = []
    for b in bins:
        inside = posixpath.normpath(posixpath.join(prefix + (at or ""), b))
        if inside in member_names or any(n.startswith(inside + "/")
                                         for n in member_names):
            prepends.append(posixpath.join(base, b))
    env_name = posixpath.normpath(posixpath.join(prefix + (at or ""),
                                                 ENV_FILE))
    if env_name not in member_names:
        return prepends, {}, []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        text = tf.extractfile(env_name).read().decode("utf-8", "replace")
    more, env, dropped = parse_env(text, base)
    return prepends + more, env, dropped
