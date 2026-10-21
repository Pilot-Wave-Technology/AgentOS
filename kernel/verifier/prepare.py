"""Build the prepared tree: committed sources plus their own environments.

ONE CONTAINER PER MOUNT, not one for the tree. Three properties fall out
of that choice and each closes a listed hole:

  isolation   a mount's setup — repo-authored shell, running with network
              and root — sees ONLY its own repository. It cannot read the
              other mounts, cannot rewrite their tests, and never holds
              the injected held-out checks (excluded from every slice,
              re-asserted from the pristine archive at splice time).
  cache       keyed per mount: image + that mount's setup texts and
              manifest bytes. Repo B's node_modules stops rebuilding
              because repo A edited a README.
  size        each capture carries one mount, not the whole workspace.

Entries expire after AGENTOS_ENVCACHE_TTL seconds (default one day):
contracts still name images by TAG, and a moved tag under an eternal
cache would serve yesterday's interpreter to today's tree forever.
Writes go through a temp file and os.replace, so two concurrent misses
on one key cannot interleave into a torn archive.

`.agentos/env` IS ALLOWLISTED. It exists so a setup can say where its
executables and caches live — not to set LD_PRELOAD in the container
that decides whether the work is done. Unknown keys are dropped and
said out loud in the report.

NO NEW DOCKER SURFACE. A snapshot-image design would need IMAGES on the
socket proxy, which infra keeps off deliberately; everything here rides
run_ephemeral_capture, the same seam build_deps uses.

AND SECTION 5 REPLACES ALL OF IT ON THE POD SUBSTRATE. See
kernel/verifier/prepare_volume.py: a networked Job mounts the
verification claim and writes `.venv` / `node_modules` straight to
`volume.ENV`, so there is no capture sidecar, no AGENTOS_CAPTURE_ID, no
tar and no artifact round-trip -- the four steps that carried four of
the five faults of 2026-09-17. This module stays because Docker has no
claim to write onto and `stage.py` still calls it there; E9 retires the
Kubernetes half of it.

`.agentos/env`'s allowlist and parser moved to
kernel/verifier/mount_env.py when that second path arrived. They are
shared by both and belong to neither, and this file was at its line
ceiling. `mount_environment` is re-exported because infra/e0/ci already
imports it from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shlex
import time

from kernel.spec import ExecutionSpec
from kernel.verifier.environment import install_env
from kernel.verifier.kinds import TREE_PATH, VERIFIER_UID, tree_kwargs
from kernel.verifier.mount_env import (ALLOWED_ENV_KEYS,  # noqa: F401
                                       mount_environment)
from kernel.verifier.setups import MAX_PREPARED_BYTES, fingerprint
from kernel.verifier.tarslice import (names, only_new, restamp,
                                      slice_subtree, splice)

logger = logging.getLogger(__name__)

# The repo's B1 guard bans environ.get with a default -- a silent
# fallback hides a misconfigured deployment. This form says the default
# out loud and still lets the env override.
CACHE_DIR = (os.environ["AGENTOS_ENVCACHE"]
             if "AGENTOS_ENVCACHE" in os.environ else "/artifacts/envcache")
CACHE_TTL_S = int(os.environ["AGENTOS_ENVCACHE_TTL"]
                  if "AGENTOS_ENVCACHE_TTL" in os.environ else "86400")


def _script(mount_setups: list) -> str:
    at = mount_setups[0]["at"]
    lines = ["set -e", f"cd {shlex.quote(TREE_PATH)}"]
    for s in mount_setups:
        body = f"sh -c {shlex.quote(s['command'])}"
        lines.append(f"( cd {shlex.quote(at)} && {body} )" if at
                     else f"( {body} )")
    return "\n".join(lines)


def _cache_get(key: str) -> bytes | None:
    path = os.path.join(CACHE_DIR, f"{key}.tar")
    try:
        if time.time() - os.path.getmtime(path) > CACHE_TTL_S:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def evict_cache(now: float | None = None) -> int:
    """Delete cache entries past their TTL. Returns how many went.

    `_cache_get` EXPIRES on read and never unlinks -- it returns None and
    leaves the file. `_cache_put` only ever overwrites its own key. So
    every image-tag bump and every edited setup left a `.tar` in
    `/artifacts/envcache` for ever, and nothing anywhere collected them.
    A prepared mount is bounded at MAX_PREPARED_BYTES (1400MB) apiece, so
    this is not a slow leak.

    Best effort, and never raises: an eviction that fails must not fail
    the reconcile pass it runs in.
    """
    now = time.time() if now is None else now
    gone = 0
    try:
        names = os.listdir(CACHE_DIR)
    except Exception:                                       # noqa: BLE001
        # BROADER THAN OSError. A misconfigured AGENTOS_ENVCACHE raises
        # ValueError, not OSError, and this runs inside the reconcile
        # pass -- where anything raised takes down the pod sweep, the
        # claim sweep and the slot backstop with it. The same mistake
        # `kernel/obs/alive.py` made the same day.
        return 0
    for name in names:
        if not name.endswith(".tar"):
            continue                      # a .tmp mid-write is not ours
        path = os.path.join(CACHE_DIR, name)
        try:
            if now - os.path.getmtime(path) <= CACHE_TTL_S:
                continue
            os.remove(path)
            gone += 1
        except OSError:
            continue
    if gone:
        logger.info("envcache: evicted %d entry(ies) past %ds",
                    gone, CACHE_TTL_S, extra={"event": "reconcile"})
    return gone


def _cache_put(key: str, blob: bytes) -> bool:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, f".{key}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, os.path.join(CACHE_DIR, f"{key}.tar"))
        return True
    except OSError:
        return False


async def _prepare_mount(driver, image: str, tree_tar: bytes,
                         mount_setups: list, inject: dict,
                         timeout: float,
                         execution_id: int | None = None
                         ) -> tuple[bytes, dict]:
    """(captured_mount_tar, report) for one mount. Cache-aware."""
    at = mount_setups[0]["at"]
    key = fingerprint(image, mount_setups)
    label = f"{at or '.'}[{','.join(s['source'] for s in mount_setups)}]"
    warns = [s["warn"] for s in mount_setups if s.get("warn")]

    cached = _cache_get(key)
    if cached is not None:
        return cached, {"mount": label, "cache": "hit", "key": key[:12],
                        "warnings": warns}

    # OFF THE EVENT LOOP. Slicing, splicing and re-stamping a tar are
    # pure Python over every member of the tree, and a conductor that
    # does them inline stops answering anything else while it does --
    # see kernel/verifier/verify.py, where the same omission froze two
    # pods for three minutes and got them both restarted.
    tree_slice = await asyncio.to_thread(
        slice_subtree, tree_tar, "verify/", at,
        exclude=set(inject or {}))
    spec = ExecutionSpec(
        image=image,
        command=["sh", "-c", _script(mount_setups)],
        # NETWORKED — the only container in the claim that is. It holds
        # one mount's committed sources and nothing else: not the other
        # repositories, not the held-out checks.
        network_mode="bridge",
        # AND TOLD WHERE THE NETWORK IS, which it never was. This runs
        # the REPOSITORY's own setup — pip, npm, cargo, go, whatever
        # `.agentos/setup.sh` says — so it is the container that hangs
        # hardest in a default-deny namespace with no proxy address.
        # `install_env` states which of the proxy's two ports this gets
        # and why a criterion still gets neither.
        env=install_env(driver),
        memory_mb=2048,
        # AND WHOSE RUN THIS IS. `capture_manifest` stamps the
        # `execution-id` label only when the spec carries one, and this
        # call site did not set it -- so `orphan_sweep` found a
        # kernel-owned pod matching no execution and destroyed a live
        # setup mid-install on execution 208. A pod that cannot name
        # its execution is indistinguishable from a leak.
        execution_id=execution_id,
    )
    # The same seam a criterion's tree travels on: pushed as bytes where
    # the substrate can write into a container before it starts, fetched
    # by id where it cannot. See kernel/verifier/kinds.py:tree_kwargs.
    result = await driver.run_ephemeral_capture(
        spec, capture=TREE_PATH, timeout=timeout,
        **await tree_kwargs(driver, tree_slice))
    tail = (result.get("output") or "")[-1200:]
    # A run the SUBSTRATE ended says nothing about the setup script, and
    # a report showing only the script's output would send somebody to
    # debug a shell that never failed.
    if result.get("infra_error"):
        tail = f"[{result['infra_error']}]\n{tail}"
    if result["exit_code"] != 0 or not result.get("archive"):
        return b"", {"mount": label, "cache": "miss", "ok": False,
                     "output": tail, "warnings": warns,
                     "timed_out": result.get("timed_out", False)}
    # THE CACHE STORES THE ENVIRONMENT AND NOTHING ELSE. The raw
    # capture is the whole mount -- sources included -- and caching that
    # is how a story got haunted by its own first attempt: leg 1
    # committed backend/, the capture (keyed by image+manifest, which
    # never changed) kept it, leg 3 removed it, and every later
    # verification overlaid backend/ back in as a "new" file. A check
    # saying `test ! -d backend` then failed for ever against work that
    # was correct. Subtracting the slice's own names leaves exactly what
    # the setup CREATED (.venv, node_modules) -- the only thing an
    # environment cache has any business remembering.
    captured = await asyncio.to_thread(
        lambda: restamp(only_new(result["archive"], names(tree_slice)),
                        VERIFIER_UID))
    if len(captured) > MAX_PREPARED_BYTES:
        return b"", {"mount": label, "ok": False, "warnings": warns,
                     "output": f"the prepared mount weighs "
                               f"{len(captured) // (1024 * 1024)}MB; "
                               f"dependencies that heavy belong in the "
                               f"contract's base image"}
    _cache_put(key, captured)
    return captured, {"mount": label, "cache": "miss", "key": key[:12],
                      "output": tail, "warnings": warns}


async def build_prepared(driver, image: str, tree_tar: bytes,
                         setups: list, inject: dict | None = None,
                         timeout: float = 600.0,
                         execution_id: int | None = None
                         ) -> tuple[bytes, dict]:
    """(prepared_tar, report). report['ok'] is the field callers gate on.

    report['mounts_env'] maps each mount's `at` to {"prepend": [...],
    "env": {...}} so the caller can give every criterion the environment
    of the mount it actually targets, instead of one global PATH where
    the first repository's venv shadows the second's.

    `execution_id` is passed straight through to every mount's capture
    spec so the Job carries the `execution-id` label the orphan sweep
    matches on -- see `_prepare_mount`. Keyword with a default, because
    a caller with no execution (the corpus validator) is legitimate.
    """
    by_at: dict = {}
    for s in setups:
        by_at.setdefault(s["at"], []).append(s)

    report: dict = {"mode": "setup", "ok": True, "mounts": [],
                    "mounts_env": {}, "dropped_env_keys": []}
    tar = tree_tar
    for at in sorted(by_at):
        captured, mrep = await _prepare_mount(
            driver, image, tree_tar, by_at[at], inject or {}, timeout,
            execution_id=execution_id)
        report["mounts"].append(mrep)
        for w in mrep.get("warnings") or []:
            logger.warning("prepare %s: %s", mrep["mount"], w)
        if mrep.get("ok") is False:
            report["ok"] = False
            report["output"] = mrep.get("output", "")
            report["failed_mount"] = mrep["mount"]
            return b"", report
        keep = {k: v for k, v in (inject or {}).items()
                if posixpath.normpath(k).startswith(
                    posixpath.normpath("verify/" + at) + "/") or not at}
        tar = await asyncio.to_thread(
            splice, tar, captured, "verify/", at, keep=keep)
        # `mount_environment` parses the captured tar too, so it goes
        # the same way rather than being the one that stayed behind.
        prepends, env, dropped = await asyncio.to_thread(
            mount_environment, captured, at, tuple(dict.fromkeys(
                b for s in by_at[at] for b in s["bins"])))
        report["mounts_env"][at] = {"prepend": prepends, "env": env}
        if dropped:
            report["dropped_env_keys"] += dropped
            logger.warning("prepare %s: .agentos/env keys dropped by the "
                           "allowlist: %s", at or ".", ", ".join(dropped))
    logger.info("prepare: %d mount(s), %dKB, %s", len(by_at),
                len(tar) // 1024,
                ", ".join(f"{m['mount']}={m.get('cache', 'fail')}"
                          for m in report["mounts"]))
    return tar, report
