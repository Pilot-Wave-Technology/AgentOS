"""The join nobody was making: every tool a profile offers has an effect class.

WHY THIS IS A MODULE AND NOT JUST A CI SCRIPT. It was a CI script, and the
script could not run. `test_effects_cover_profiles.py` hardcoded
`/app/profiles` and imported `fb_harness`, which pulls in the whole harness
and therefore the `anthropic` package — so the check only executed inside
one container target, and a developer could not run it at all.

It was written specifically to stop a third occurrence of "a tool shipped
without an effect class". There has since been a fifth (`goal_knowledge`,
denied at stage 3 on all three profiles). The logic was never wrong; it
simply was not reachable. So the rule moves here, where it imports nothing
but the standard library and `effects` — which itself imports nothing —
and both the container gate and a pre-push hook can call it.

WHAT AN OMISSION COSTS, restated because it is the reason to care:
`classify()` returns IRREVERSIBLE for anything it does not recognise. That
is the right default — a tool added without a class must fail loudly rather
than quietly widen what a run may do. But it surfaces at stage 3 as "needs
a person to approve", a long way from the omission, and reads as a policy
decision rather than a gap.
"""
from __future__ import annotations

import os
import re

from kernel.harness.effects import _BY_ARGUMENT, _CONTAINED, _REVERSIBLE

# `profiles/` at the repo root, or wherever the loader was pointed. Same
# resolution order as `kernel/profiles/loader.py`, so the check and the
# thing it checks can never disagree about which directory is in play.
DEFAULT_PROFILES_DIR = os.environ.get("AGENTOS_PROFILES_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "profiles")

# Only the `tools` / `tools_add` arrays — NOT every quoted string in the
# file, which would sweep up `kind`, `domain`, the workspace driver, and
# the commented-out list of tools deliberately not built yet. A check that
# reports pending work as a defect gets ignored.
_ARRAY = re.compile(r"^tools(?:_add)?\s*=\s*\[(.*?)^\]", re.S | re.M)
_NAME = re.compile(r'"([a-z][a-z0-9_]+)"')


def classified() -> set[str]:
    """Every tool `effects.py` can put a class on."""
    return set(_CONTAINED) | set(_REVERSIBLE) | set(_BY_ARGUMENT)


def surface(path: str) -> set[str]:
    """The tools one profile.toml actually offers."""
    out: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    for block in _ARRAY.finditer(body):
        for line in block.group(1).splitlines():
            out.update(_NAME.findall(line.split("#", 1)[0]))
    return out


def surfaces(profiles_dir: str | None = None) -> dict[str, set[str]]:
    """`{profile name: tools it offers}`, including `base`.

    Missing directory is an empty result rather than a raise: the caller
    is a check, and "no profiles found" is something it should report as a
    failed assertion with a path in the message, not a traceback.
    """
    root = profiles_dir or DEFAULT_PROFILES_DIR
    out: dict[str, set[str]] = {}
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, "profile.toml")
        if os.path.isfile(path):
            out[entry] = surface(path)
    base = os.path.join(root, "base.toml")
    if os.path.isfile(base):
        out["base"] = surface(base)
    return out


def unclassified(profiles_dir: str | None = None) -> dict[str, list[str]]:
    """`{tool: [profiles offering it]}` for every tool with no class.

    Empty means the join holds. Non-empty means each listed tool is
    IRREVERSIBLE by default and therefore denied at stage 3 on every
    profile named beside it.
    """
    known = classified()
    per = surfaces(profiles_dir)
    out: dict[str, list[str]] = {}
    for name, tools in per.items():
        for tool in sorted(tools - known):
            out.setdefault(tool, []).append(name)
    return out
