"""The tool catalog — every tool the harness implements, with its schema.

Profiles name tools; this is what those names mean. Two things follow from
keeping it in one place:

  * A profile naming a tool the harness does not implement is caught at
    load, not discovered when a model calls it and gets a confusing
    refusal from chain stage 2. `tools_add = ["writ"]` is a typo, and the
    difference between a typo and a deliberately narrow surface is exactly
    what a security boundary must not have to guess.

  * The schemas handed to the adapter are derived from the profile, so the
    model is never shown a tool the envelope will refuse. Showing a tool
    and then refusing it wastes a step and teaches the model nothing.

Schemas are the neutral shape from E5b's seam: name, description,
parameters. Adapters translate; nothing provider-specific belongs here.

ONE FAMILY PER MODULE. The table reached 450 lines as one literal, which
is the length at which people stop reading it and start appending to it.
Adding a tool is now a small edit to the family it belongs to — and the
families are the argument: what a tool touches is why it runs where it
runs. `spec.py` holds the declaration itself and the two closed sets it
validates against.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import (EFFECTS, SIDES, _B, _I, _S,  # noqa: F401
                                       _schema)
from kernel.tools.catalog.actions import TOOLS as _ACTIONS
from kernel.tools.catalog.artifacts import TOOLS as _ARTIFACTS
from kernel.tools.catalog.delegation import TOOLS as _DELEGATION
from kernel.tools.catalog.files import TOOLS as _FILES
from kernel.tools.catalog.shell import TOOLS as _SHELL
from kernel.tools.catalog.workspace import TOOLS as _WORKSPACE

# Merged in surface order — roughly the order an executor meets them, which
# is also the order they appear in a profile's prompt.
CATALOG: dict[str, dict] = {}
for _part in (_FILES, _SHELL, _WORKSPACE, _ARTIFACTS, _DELEGATION, _ACTIONS):

    _clash = set(_part) & set(CATALOG)
    if _clash:
        # Two families declaring the same name would silently keep one of
        # them, and which one would depend on import order.
        raise RuntimeError(f"tool(s) declared in two families: {sorted(_clash)}")
    CATALOG.update(_part)
del _part


# Declared, not yet built. A tool stays here until it is routed AND
# implemented; the profile test refuses to let any profile expose one.
#
# The alternative — leaving them in a profile's surface and letting the
# executor answer "unimplemented worker tool" — hands the model a confusing
# refusal for something it was told it had, and hides the debt in prose.
# Listing them makes deferral a fact a test can check.
PENDING: frozenset = frozenset({
    # web_search stays pending: no search backend is configured, and a tool
    # that always answers "not configured" is worse than one that is absent.
    "web_search",
})


def is_pending(name: str) -> bool:
    return name in PENDING


# ── the two derived views ────────────────────────────────────────────────
# `executor.py` routes on the first and `effects.py` classifies on the
# second. Both used to be hand-kept literals in those files; they are
# functions of the catalog now, so a new tool joins them by existing.


def tools_by_side(side: str) -> frozenset:
    """Every tool that runs on this side. A PENDING tool is not one.

    Pending tools are excluded because routing is about dispatch: a name
    in a routing set is a promise that something answers it, and
    `web_fetch` sat in one for months with no function behind it. The
    profile loader already refuses to expose a pending tool, so nothing
    can call one; leaving it out of the routing set is the second half of
    the same statement.
    """
    if side not in SIDES:
        raise ValueError(f"unknown side {side!r}; known: {SIDES}")
    return frozenset(n for n, s in CATALOG.items()
                     if s["_side"] == side and n not in PENDING)


def effect_of(name: str) -> str:
    """This tool's declared effect class, or "" if it has none.

    "" means PENDING — nothing is implemented, so there is nothing to
    classify. Callers must treat it as unknown, NOT as harmless.
    """
    spec = CATALOG.get(name)
    return spec["_effect"] if spec else ""


def tools_by_effect(effect: str) -> frozenset:
    if effect not in EFFECTS:
        raise ValueError(f"unknown effect {effect!r}; known: {EFFECTS}")
    return frozenset(n for n, s in CATALOG.items() if s["_effect"] == effect)


def schemas_for(tool_names) -> list[dict]:
    """Schemas for a profile's surface, in the profile's order.

    Raises on an unknown name rather than skipping it: a silent skip turns
    a typo in profile.toml into a tool the model never sees and no one
    notices is missing.

    The `_`-prefixed keys are stripped here — this is the seam where a
    schema stops being ours and becomes the provider's, and a key the wire
    protocol has never heard of comes back as a 400 naming a field the
    author never wrote. Stripped per call rather than from a frozen copy
    because `baseline_g3` mutates the catalog to reconstruct an old
    surface, and a precomputed view would not follow it.
    """
    unknown = [t for t in tool_names if t not in CATALOG]
    if unknown:
        raise KeyError(
            f"tool(s) not implemented by the harness: {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(CATALOG))}"
        )
    return [{k: v for k, v in CATALOG[t].items() if not k.startswith("_")}
            for t in tool_names]
