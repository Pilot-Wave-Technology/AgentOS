"""Which mount's environment a criterion gets, from the command it runs.

Lifted out of `verify.py` verbatim at E2, because a second caller
appeared: the batched runner had to resolve the same answer per check
before writing the check's `export` lines, and `verify.py` importing
`batch.py` importing `verify.py` is a cycle. That caller retired at E9 --
the verification pod gives every criterion one container, so there is
nothing left to batch -- and this file stays where it is. The function is
unchanged: the incident it records below was measured against this exact
text, and re-deriving it would be re-deriving a bug.

`verify.environment_for` is still importable: it re-exports this, which
is what `kernel/tools/check_paths.py` and `test_f5c_setups.py` name.
"""

from __future__ import annotations

import re

_CD = re.compile(r"^\s*cd\s+[\"']?([^\s\"'&;]+)")


def environment_for(command: str, mounts_env: dict) -> tuple[dict, str]:
    """(env, path_prepend) for one criterion, from the mount it targets.

    One global PATH made the first repository's .venv shadow the
    second's: `cd repos/B && pytest` resolved A's pytest because A
    sorts first. The convention every check follows -- `cd <mount> &&
    ...` -- names the target, so the target's executables go FIRST and
    every other mount's follow as a fallback (a repo with no
    environment of its own may legitimately be served by a sibling's).
    """
    if not mounts_env:
        return {}, ""
    target = ""
    m = _CD.match(command or "")
    if m:
        path = m.group(1).strip("/")
        best = ""
        for at in mounts_env:
            if at and (path == at or path.startswith(at + "/")):
                if len(at) > len(best):
                    best = at
        target = best
    ordered = ([target] if target in mounts_env else []) + [
        at for at in sorted(mounts_env) if at != target]
    prepends, env = [], {}
    for at in reversed(ordered):
        # reversed: later dict.update wins, so the TARGET's env lands
        # last and overrides a sibling's same-named variable.
        env.update(mounts_env[at].get("env") or {})
    for at in ordered:
        prepends += mounts_env[at].get("prepend") or []
    return env, ":".join(dict.fromkeys(prepends))
