"""Startup must not block on the gateway, or the deploy is a coin flip.

THE FAILURE, MEASURED ON DEV. Cloud Run's startup probe allows four
minutes. `on_startup` awaited `sync_gateway_model_catalog()` as its last
act -- two live gateway calls, `models.reload` then a provider-auth
`config.set` -- so the port stayed shut until the gateway answered.
Meanwhile the chat dispatcher, started ~250 lines earlier, had its
workers hydrating every live thread against that same gateway. The
control plane raced itself to become ready.

    8b339504   deploy step 8m43s, passed on a retry
    af5fa6ac   FAILURE  "Container failed to become healthy.
                         Startup probes timed out after 4m"
    9aa0d37d   FAILURE on the first attempt, SUCCESS on a clean re-run

The container was never unhealthy: its logs show a working process
still grinding through startup when it was killed. It just could not
finish inside four minutes, and that got likelier as dev accumulated
threads.

WHY A TEST AND NOT A COMMENT. The fix is one `create_task`, and the
next person adding a startup step has every reason to `await` it -- the
rest of the function reads like a sequence. This fails the build when
something on the readiness path starts waiting on the gateway again.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_MAIN = pathlib.Path(__file__).resolve().parents[3] / "app" / "main.py"


def _startup_body() -> ast.AsyncFunctionDef:
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_startup":
            return node
    raise AssertionError("app/main.py has no `on_startup` any more")


def _awaited_names(node: ast.AST) -> set[str]:
    """Every name awaited DIRECTLY in this function body.

    Deliberately not recursive into nested function definitions: a
    coroutine defined here and handed to `create_task` is exactly the
    pattern we want, and its inner `await` is not on the readiness
    path.
    """
    out: set[str] = set()

    class _V(ast.NodeVisitor):
        def visit_AsyncFunctionDef(self, n):          # noqa: N802
            if n is not node:
                return                                 # detached; fine
            self.generic_visit(n)

        def visit_FunctionDef(self, n):               # noqa: N802
            return

        def visit_Await(self, n):                     # noqa: N802
            call = n.value
            fn = call.func if isinstance(call, ast.Call) else None
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
            self.generic_visit(n)

    _V().visit(node)
    return out


# Calls that reach the gateway. Awaiting any of these before the port
# opens makes readiness depend on a second service being up and quick.
GATEWAY_CALLS = {
    "sync_gateway_model_catalog",
    "_mr_sync",
    "reload_gateway_model_catalog",
    "sync_provider_auth_configs_to_gateway",
}


@pytest.mark.parametrize("name", sorted(GATEWAY_CALLS))
def test_startup_does_not_await_a_gateway_call(name):
    awaited = _awaited_names(_startup_body())
    assert name not in awaited, (
        f"`on_startup` awaits `{name}`, which talks to the gateway. "
        f"Readiness then waits on a second service, and Cloud Run's "
        f"startup probe gives four minutes. Use "
        f"`asyncio.create_task(...)` as the dozen siblings on this path "
        f"already do.")


def test_the_model_sync_is_still_performed_at_startup():
    """Detached, not deleted. A stale model catalogue degrades model
    SELECTION until the first sync lands, so the call must still
    happen -- just not before the port opens."""
    src = _MAIN.read_text(encoding="utf-8")
    assert "_mr_sync_detached" in src
    assert "asyncio.create_task(_mr_sync_detached())" in src


def test_its_failure_cannot_take_the_app_down():
    """A detached task that raises leaves nobody to catch it. The
    wrapper logs and swallows, as the awaited version did."""
    node = _startup_body()
    for inner in ast.walk(node):
        if (isinstance(inner, ast.AsyncFunctionDef)
                and inner.name == "_mr_sync_detached"):
            assert any(isinstance(h, ast.ExceptHandler)
                       for h in ast.walk(inner)), (
                "_mr_sync_detached must swallow its own failure")
            return
    raise AssertionError("_mr_sync_detached is not defined in on_startup")
