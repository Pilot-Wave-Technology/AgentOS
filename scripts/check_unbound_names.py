#!/usr/bin/env python3
"""Unbound-name guard — catches `NameError`-at-runtime defects statically.

Fails the build if any function LOADS a name that is bound nowhere reachable:
not a parameter, not assigned in that function, not in an enclosing function,
not module-level, not a builtin. Python compiles such a name as a global lookup,
so the line raises `NameError` every single time it executes.

WHY THIS EXISTS (2026-08-18). Three real defects of exactly this shape were found
in this repo, none caught by code review, the test suite, or a 17-defect audit:

  1. `diff_review.post_diff_review` read `per_repo`, which was only ever assigned
     in a DIFFERENT function (`begin_diff_review`). Unconditional NameError on the
     shared diff-review approval gate — every story, both execution modes.
  2. `merge_orchestrator/handlers/pre_integration._call_merge_preview_rpc` read
     `story_id` with no such parameter. Swallowed by `except Exception`, which then
     logged "workspace.mergePreview RPC failed" — blaming the gateway for a call
     that was never issued. The caller treats the None return as a hard failure, so
     EVERY pre-integration merge gate failed. Reached production.
  3. `task_controller.create_subtasks_from_planner` read `task` where the parameter
     is `parent_task`. Swallowed by a debug-level except, so silent: every
     planner-created subtask's `task.created` relay to Notion/Slack was dropped.
     Reached production.

Cases 2 and 3 show why a test suite does not catch these: a bare `except` turns the
NameError into a silent degradation, and the misleading log message actively
misdirects whoever reads it.

Requires only Python 3 (stdlib `ast`) — no dependencies, no database, no network.
Mirrors the `migrations/check_sequence.sh` / `scripts/check_ledger_actors.sh`
convention so the Cloud Build pipelines can run it as an early, cheap gate.

Usage:
    python scripts/check_unbound_names.py [PATH ...]     # default: app

Exit codes: 0 = clean, 1 = findings (or a file failed to parse).

DELIBERATELY CONSERVATIVE. When resolution is uncertain this stays silent: a false
positive would train people to ignore the gate, which is worse than a miss. Known
limits, all erring toward silence: `import *` contents are unknown, so a module
using it is skipped entirely; names injected via `globals()` are not modelled.
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

_BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__debug__", "__builtins__",
}

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _binding_targets(node: ast.AST) -> set[str]:
    """Names bound by one statement's target(s) only — never its value.

    Inspecting only the target side matters: walking the whole statement would
    descend into a lambda or comprehension in the VALUE and collect its internals
    as if they were bound out here.
    """
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in node.items if i.optional_vars]
    elif isinstance(node, ast.NamedExpr):
        targets = [node.target]

    out: set[str] = set()
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
    return out


def _collect_bindings(scope: ast.AST, *, include_params: bool) -> set[str]:
    """Every name bound in `scope`'s OWN scope.

    Descends through control flow (try/if/while/with) because those do not create
    scopes, but stops at nested functions and classes — binding only their name,
    since their bodies are separate scopes. This is what makes a module-level
    `try: ... def _uuid7(): ...  except ImportError: def _uuid7(): ...` resolve
    correctly; an earlier version only looked at top-level statements and reported
    such a def as unbound.
    """
    bound: set[str] = set()

    if include_params and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            bound.add(arg.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_NODES):
                bound.add(child.name)
                continue                      # separate scope — do not descend
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                bound.update(child.names)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                bound.add(child.name)
            elif isinstance(child, ast.Lambda):
                # A lambda is its own scope; only its presence matters here.
                continue
            # `.update()` not `|=`: an augmented assignment would make `bound` a
            # local of this nested function and raise UnboundLocalError. (Yes — this
            # checker's own first draft had exactly the class of bug it detects.)
            bound.update(_binding_targets(child))
            walk(child)

    walk(scope)

    # Comprehension and lambda targets anywhere in this scope's expressions.
    for node in ast.walk(scope):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                bound |= _binding_targets(gen)
        elif isinstance(node, ast.Lambda):
            largs = node.args
            for arg in (*largs.posonlyargs, *largs.args, *largs.kwonlyargs):
                bound.add(arg.arg)
            if largs.vararg:
                bound.add(largs.vararg.arg)
            if largs.kwarg:
                bound.add(largs.kwarg.arg)
    return bound


def _own_loads(scope: ast.AST) -> list[tuple[str, int]]:
    """Name loads belonging to this scope — excluding nested function/class bodies."""
    out: list[tuple[str, int]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_NODES):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                out.append((child.id, child.lineno))
            walk(child)

    walk(scope)
    return out


def _uses_star_import(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )


def scan_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: SYNTAX ERROR: {exc.msg}"]
    except (UnicodeDecodeError, OSError) as exc:
        return [f"{path}: could not read: {exc}"]

    if _uses_star_import(tree):
        return []          # unknowable names — stay silent rather than guess

    module_names = _collect_bindings(tree, include_params=False)
    findings: list[str] = []

    def visit(node: ast.AST, enclosing: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own = _collect_bindings(child, include_params=True)
                visible = own | enclosing | module_names | _BUILTINS
                for name, lineno in _own_loads(child):
                    if name not in visible:
                        findings.append(
                            f"{path}:{lineno}: '{name}' is read in {child.name}() but "
                            f"bound nowhere reachable — this raises NameError at runtime"
                        )
                visit(child, own | enclosing)
            else:
                # Class bodies and control flow do not add a function scope here.
                visit(child, enclosing)

    visit(tree, set())
    return findings


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("app")]
    findings: list[str] = []
    scanned = 0

    for root in roots:
        if not root.exists():
            print(f"check_unbound_names: path not found: {root}", file=sys.stderr)
            return 1
        targets = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for file in targets:
            if any(part in {"__pycache__", ".venv", "node_modules"} for part in file.parts):
                continue
            scanned += 1
            findings.extend(scan_file(file))

    print(f"check_unbound_names: scanned {scanned} file(s) under "
          f"{', '.join(str(r) for r in roots)}")

    if not findings:
        print("check_unbound_names: OK — no unbound names")
        return 0

    print("")
    print("check_unbound_names: FAILED — each line below raises NameError when reached.")
    print("A name used across a function boundary must be a PARAMETER; a module-level")
    print("global would 'fix' the error while leaking state between calls.")
    print("")
    for line in findings:
        print(f"  {line}")
    print("")
    print(f"{len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
