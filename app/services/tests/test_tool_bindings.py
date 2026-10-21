"""Every routed tool has an implementation behind the name.

THE BUG THIS GENERALISES. `executor.py` dispatched `web_fetch` to
`tools_worker.web_fetch`, and that function did not exist — the module
imported `fetch` from `tools_web` and never used it. Every `web_fetch`
call from story-coding and story-business came back as
`E_TOOL_RAISED: 'web_fetch' raised AttributeError`.

`test_profiles.py` could not catch it. It asserts the tool name is in
`WORKER_TOOLS` or `SANDBOX_TOOLS` — which it was. Set membership says the
router knows where to send the call; it says nothing about anything being
there to receive it.

READ BY AST, NOT BY IMPORT. `kernel.harness.tools_worker` imports
`models.types`, which imports `models/__init__`, which imports
`anthropic` — absent on a dev machine. A test that can only run in the
container is a test that does not run, which is how the last three of
these survived. Parsing the source needs nothing.
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _source(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _modules(rel: str) -> list[str]:
    """One path, or every `.py` under it if it is a package.

    `tools_worker` became a package when it passed 400 lines. The names it
    exports are the contract — `executor.py` dispatches `tools_worker.X`
    — and which file each lives in is housekeeping, so this test must not
    care. It cares that the name resolves.
    """
    here = ROOT / rel
    if here.is_dir():
        return [str(p.relative_to(ROOT)).replace("\\", "/")
                for p in sorted(here.glob("*.py"))]
    return [rel]


def _defined_in(rel: str) -> set[str]:
    """Function names a module (or package) defines or re-exports."""
    out: set[str] = set()
    for path in _modules(rel):
        tree = ast.parse(_source(path))
        out |= {n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        # A package's `__init__` re-exports rather than defines, and a
        # dispatch resolves against the re-export just as well.
        for n in tree.body:
            if isinstance(n, ast.ImportFrom):
                out |= {a.asname or a.name for a in n.names}
    return out


def _executor_family() -> str:
    """Every `kernel/harness/executor*.py`, concatenated.

    NOT `executor.py` alone, and the reason is the same one `_modules`
    gives above. Wave 4 split the if-chain out to
    `executor_worker_dispatch.py` to get `executor.py` under the house
    ceiling; a guard reading one path would then have found ZERO
    dispatches and passed VACUOUSLY — the exact failure the file this
    guards used to cite as the reason it could not be split. Which file
    a dispatch lives in is housekeeping. That the name resolves is the
    contract, and the union cannot be emptied by moving code inside the
    family.
    """
    return "\n".join(_source(rel) for rel in sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "kernel/harness").glob("executor*.py")))


def _dispatched_to(module_alias: str) -> set[str]:
    """Attribute names the executor family calls on a dispatch module.

    Matches `tools_worker.web_fetch(` — the shape of most branches in
    `worker_execute`'s if-chain — AND `tools_worker.web_fetch,`, which
    is the shape of one handed to `asyncio.to_thread`.

    Both are dispatches; only one is a call. Matching the call alone made
    this test silently stop covering whichever branch was moved off the
    event loop — and it stopped covering `web_fetch` the day that one
    was, which is how the build caught it. It is the same "passed as a
    value, not invoked" distinction the Kubernetes guard in
    test_the_conductor_cannot_wedge.py relies on, pointing the other way.
    """
    return set(re.findall(rf"\b{re.escape(module_alias)}\.(\w+)\s*[(,)]",
                          _executor_family()))


def test_every_worker_dispatch_has_a_function():
    """THE REGRESSION. If the executor calls `tools_worker.X`, then
    `tools_worker` must define `X`."""
    called = _dispatched_to("tools_worker")
    # NON-VACUITY, and it is not padding. This assertion holds against
    # the one way this test can stop meaning anything: a rename, a
    # `getattr`, or a split that leaves the regex matching nothing at
    # all, at which point `called - defined` is empty and the test is
    # green on no evidence. Five is comfortably under the count today
    # and above zero, which is the number that matters.
    assert len(called) >= 5, (
        f"only {sorted(called)} dispatch through a literal "
        f"`tools_worker.X` — either the router moved outside "
        f"kernel/harness/executor*.py or the dispatches stopped being "
        f"attribute accesses, and this guard now checks nothing")
    defined = _defined_in("kernel/harness/tools_worker")
    missing = sorted(called - defined)
    assert not missing, (
        f"executor.py dispatches to {missing} and tools_worker defines no "
        f"such function — every call returns E_TOOL_RAISED at runtime")


def test_web_fetch_specifically():
    """Named, because it was broken on two live profiles and the generic
    test above would not say which one mattered."""
    assert "web_fetch" in _defined_in("kernel/harness/tools_worker")
    assert "web_fetch" in _dispatched_to("tools_worker")


def test_web_fetch_uses_the_ssrf_guarded_fetch():
    """It must go through `tools_web.fetch`, which is deny-by-default and
    re-checks every redirect hop. A hand-rolled urlopen would have none
    of that — and the import was already sitting there unused, which is
    how the gap looked deliberate."""
    fn = None
    for path in _modules("kernel/harness/tools_worker"):
        fn = next((n for n in ast.parse(_source(path)).body
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "web_fetch"), fn)
    # A bare `next()` raised StopIteration here, which pytest reports as an
    # error with no message — the reader learns nothing about which of the
    # three failures is the root one.
    assert fn is not None, "tools_worker defines no web_fetch"
    body = ast.unparse(fn)
    assert "fetch(" in body, "web_fetch must delegate to tools_web.fetch"
    assert "urlopen" not in body and "urllib" not in body, (
        "web_fetch must not open a URL itself — the SSRF guard lives in "
        "tools_web.fetch")


def test_the_ssrf_guard_blocks_what_it_claims_to():
    """`tools_web` imports cleanly, so this one runs for real rather than
    by inspection."""
    from kernel.harness.tools_web import fetch

    for url in ("http://169.254.169.254/latest/meta-data/",   # cloud metadata
                "http://127.0.0.1:8000/",                      # the worker
                "http://10.0.0.1/"):                           # private range
        text, error = fetch(url)
        assert error, f"{url} was not blocked"
        assert not text


def test_the_routing_sets_are_derived_not_restated():
    """The other direction, and it is now structural rather than checked.

    A routing entry for a tool the catalog does not declare is a name no
    model can ever call, and `web_fetch` was one for months. That could
    happen because `SANDBOX_TOOLS` and `WORKER_TOOLS` were hand-written
    literals keyed by tool name, in a different file from the catalog.
    They are `tools_by_side(...)` now, so the orphan this test used to
    look for cannot be written down.

    What is left to check is that nobody puts the literal back.
    """
    src = _source("kernel/harness/executor.py")
    assert "tools_by_side" in src, (
        "executor.py no longer derives its routing from the catalog")
    for name in ("SANDBOX_TOOLS", "WORKER_TOOLS"):
        m = re.search(rf"^{name}\s*=\s*(.+)$", src, re.M)
        assert m, f"{name} is gone — something else reads it"
        assert "tools_by_side" in m.group(1), (
            f"{name} is a literal again: {m.group(1)!r}")


def test_every_catalog_tool_says_where_it_runs():
    """`side` is required by `_schema`, so this cannot fail by omission —
    it fails if somebody adds a fourth side without teaching the router,
    which would route the tool nowhere and answer `unroutable tool`.

    The catalog imports nothing, so this runs for real.
    """
    from kernel.tools.catalog import CATALOG, PENDING, SIDES, tools_by_side

    routed: set[str] = set()
    for side in SIDES:
        here = tools_by_side(side)
        assert not (routed & here), f"{side} double-routes {routed & here}"
        routed |= set(here)

    # `loop` tools never reach the executor's dispatch, so they are routed
    # in the catalog's sense without being in either executor set.
    assert routed == set(CATALOG) - PENDING, (
        f"unroutable: {sorted(set(CATALOG) - PENDING - routed)}")


def test_pending_tools_are_routed_nowhere():
    """A name in a routing set is a promise something answers it.

    `web_fetch` sat in `WORKER_TOOLS` with no function behind it, and
    `test_profiles.py` passed because membership was all it asserted. A
    PENDING tool has nothing to answer with by definition, so it must not
    be in a routing set at all — the profile loader refusing to expose it
    is the other half of the same statement.
    """
    from kernel.tools.catalog import PENDING, SIDES, tools_by_side

    for side in SIDES:
        leaked = sorted(PENDING & tools_by_side(side))
        assert not leaked, f"{side} routes pending tool(s) {leaked}"


def test_every_executor_construction_uses_real_parameters():
    """`console/server.py` passed `conn=conn` to `BridgeToolExecutor`,
    which has no such parameter — a TypeError on every `/seed` dispatch.

    Worker-side tools reach the database through
    `subagent_context["conn"]`, and the file's other two construction
    sites already did it that way, so this was one site drifting from two
    neighbours rather than a misunderstanding of the design.

    By AST for the usual reason: importing the executor pulls in
    `anthropic`.
    """
    src = _source("kernel/harness/executor.py")
    cls = next(n for n in ast.parse(src).body
               if isinstance(n, ast.ClassDef) and n.name == "BridgeToolExecutor")
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    params = {a.arg for a in init.args.args} - {"self"}

    bad = []
    for rel in ("kernel/console/server.py", "kernel/harness/leg_runner.py",
                "kernel/harness/subagent_run.py"):
        for node in ast.walk(ast.parse(_source(rel))):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "BridgeToolExecutor"):
                extra = sorted({k.arg for k in node.keywords if k.arg} - params)
                if extra:
                    bad.append(f"{rel}:{node.lineno} passes {extra}")
    assert not bad, bad
