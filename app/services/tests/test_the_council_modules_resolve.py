"""Every name the council calls actually exists.

WRITTEN AFTER A LIVE RUN FOUND TWO BROKEN REFERENCES THAT A FULL GREEN
SUITE HAD MISSED. Splitting `provisioning.py` into modules left:

    goal_health:  name '_recent_messages' is not defined
    progress:     module 'inputs.progress_delta' has no attribute 'build'

The first was a helper that stayed behind when its only caller moved. The
second was a rename that silently did not apply, because the `sed` pattern
omitted a type annotation present in the real signature.

WHY NOTHING CAUGHT THEM. The council tests assert against SOURCE TEXT --
`assert "_ingest" in src` and the like. That style is right for checking
that a design decision survived a refactor, and it is blind to whether
the code runs: a function can be spelled correctly, asserted about
correctly, and reference a name that does not exist. `import` alone would
not have caught either one either, since both live inside function bodies
that only execute when a stage runs.

So this file resolves names rather than reading about them:

    1. every module in the package imports
    2. every `module.attribute` call resolves on the real module
    3. every bare name called in a function is defined SOMEWHERE

Cheap, static, and it fails in CI instead of in a goal review.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import pkgutil

import pytest

PACKAGE = "app.services.council"


def _modules() -> list[str]:
    pkg = importlib.import_module(PACKAGE)
    found = [PACKAGE]
    for mod in pkgutil.walk_packages(pkg.__path__, PACKAGE + "."):
        if ".tests" in mod.name:
            continue
        found.append(mod.name)
    return sorted(found)


MODULES = _modules()


@pytest.mark.parametrize("name", MODULES)
def test_the_module_imports(name):
    importlib.import_module(name)


def _source(name: str) -> tuple[str, ast.Module]:
    mod = importlib.import_module(name)
    path = getattr(mod, "__file__", None)
    if not path:
        pytest.skip(f"{name} has no file")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


def _bound_anywhere(tree: ast.Module) -> set[str]:
    """Every name bound anywhere in the file.

    DELIBERATELY OVER-APPROXIMATE. Proper scope analysis would be a
    reimplementation of pyflakes; treating any name that is ever assigned,
    imported, defined, or taken as a parameter as "available" gives a
    check with no false positives and one real power -- it still catches a
    name that is bound NOWHERE, which is exactly what a helper left behind
    by a move looks like.
    """
    out: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            # A lambda has parameters and no name -- miss those and every
            # `key=lambda x: ...` reports `x` as undefined.
            out.add(getattr(node, "name", ""))
            args = getattr(node, "args", None)
            if args:
                for a in (list(args.args) + list(args.posonlyargs)
                          + list(args.kwonlyargs)):
                    out.add(a.arg)
                for a in (args.vararg, args.kwarg):
                    if a:
                        out.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store,
                                                                 ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            out.update(node.names)
    return out


@pytest.mark.parametrize("name", MODULES)
def test_every_name_it_uses_is_defined(name):
    """Catches `_recent_messages` (a helper whose caller moved away) AND
    `_STUCK_HOURS` (a constant cut by a slice), because it checks every
    LOAD of a name rather than only the ones being called.

    The narrower call-only version of this test shipped, passed, and let
    `_STUCK_HOURS` break the progress stage for two live cycles.
    """
    _, tree = _source(name)
    known = _bound_anywhere(tree)
    missing = sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id not in known
    })
    assert not missing, f"{name} uses undefined name(s): {missing}"


@pytest.mark.parametrize("name", MODULES)
def test_every_module_attribute_it_calls_exists(name):
    """Catches `progress_delta.build`: a rename that did not apply.

    Only checks attributes on names bound by an `import` of a real
    module — anything else is an object whose attributes cannot be
    resolved statically.
    """
    _, tree = _source(name)

    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                try:
                    importlib.import_module(full)
                except Exception:          # not a module — a name in one
                    continue
                imported[alias.asname or alias.name] = full
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = alias.name

    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        # Resolve dotted chains like `inputs.progress_delta.build`.
        parts, cur = [], node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name) or cur.id not in imported:
            continue
        parts.reverse()
        target = imported[cur.id]
        for i, part in enumerate(parts):
            try:
                obj = importlib.import_module(target)
            except Exception:
                break
            if hasattr(obj, part):
                if i < len(parts) - 1:
                    target = f"{target}.{part}"
                    continue
                break
            try:                            # a submodule not yet imported
                importlib.import_module(f"{target}.{part}")
                target = f"{target}.{part}"
            except Exception:
                missing.append(f"{target}.{part}")
                break

    assert not missing, f"{name} calls missing attribute(s): {sorted(set(missing))}"
