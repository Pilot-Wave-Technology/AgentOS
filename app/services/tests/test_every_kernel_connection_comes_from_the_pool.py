"""A connection nobody counted is the one that takes the database down.

WHAT WAS TRUE BEFORE. Three places under `kernel/` opened a connection
with `asyncpg.connect` instead of taking one from `loopbridge`'s pool:

    kernel/answerer/deliver.py:126        one per question in flight
    kernel/credentials/sidecar_http.py:56 one per sidecar process, for ever
    kernel/harness/leg_runner.py:124      one per leg process

Each had a real reason to want a connection to ITSELF and no reason at
all to want a private one, and the two are not the same claim. A leg
holds a Postgres advisory lock for the whole leg and `lease.py` is
explicit that "acquire and release MUST happen on the same one"; an
answer runs for minutes beside a pass that is still using its own
connection, and asyncpg refuses concurrent use of one outright. So:
dedicated, yes. Uncounted, no.

THE MEASURED SHAPE OF THE CEILING. `loopbridge.POOL_MIN, POOL_MAX = 2,
16`, and `worker/concurrency.py` already spends a paragraph on what
happens past it: "a number that exhausts the pool does not fail loudly,
it makes every OTHER query in the process wait on a connection, which
reads as 'the database got slow' and never as 'somebody raised a
concurrency setting'." The instance behind it is a db-custom-2-8192 on
the default `max_connections`. Every leg already holds one pooled
connection for minutes and the pool-safe fan-out is 10, so the headroom
these three were spending was headroom that had already been budgeted.
At twenty concurrent stories the bypasses are an unbounded addend on a
bounded number, and the failure is silent by construction.

TWO THINGS THE BYPASS COST BESIDES CONNECTIONS, both found while fixing
it and both invisible from the connection count. `make_pool` runs `SET
ROLE agentos_worker` on every pooled connection -- the F1 boundary --
so the sidecar was authorising grants as the superuser and `leg_runner`
was running legs with more privilege than the worker that runs them in
production. And each site carried its own copy of `search_path`, which
is the setting `loopbridge`'s docstring exists to keep in one place.

WHY THE FIX IS SHAPED AS `acquire`. The pool reaches code in this build
by being threaded down as a zero-argument `pool.acquire`, from
`worker/service.py` through `worker_pass` into `worker_tick`, which
passes it on to `adjudicate_pass` and to each leg. There is no module
level pool singleton anywhere and this change does not add one, so
`deliver_questions` grows the same `acquire` keyword its neighbours
already take, and degrades to answering inline when the caller has none
-- exactly as `adjudicate_pass` and `worker_tick` degrade to serial.

AND WHY THE GUARD. `null_executor.py` still opens its own, and the
comment above it reads "Canonical connection style -- C4 onward copies
this pattern". That is how three of these appeared. The guard below is
an AST walk rather than a grep for the same reason
`ci/check_no_environ_default.py` is: a grep over text cannot tell code
from prose, and this very docstring names the call it forbids.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import pathlib
import textwrap
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from kernel.answerer import deliver
from kernel.credentials import sidecar_http
from kernel.credentials.sidecar_server import TOKEN_ENV
from kernel.conductor import pauses
from kernel.harness import leg_runner
from kernel.harness.loop_wire import decode

ROOT = pathlib.Path(__file__).resolve().parents[3]

# The ONLY places under kernel/ permitted to open a connection of their
# own, each because it is a process with no pool to take one from.
ALLOWED = {
    # A scripted, no-LLM fixture run as its own process inside the
    # sandbox container, where the pool -- and the agentos_worker role
    # it adopts -- may not exist at all.
    "kernel/null_executor.py",
    # Tests that connect to assert something about SQL. Not a serving
    # path; they run one statement and close.
    "kernel/worker/tests/test_workspace_retry_sql.py",
    "kernel/workspace/tests/test_browse_resolve.py",
}


# ── the guard ────────────────────────────────────────────────────────────

def _asyncpg_names(tree: ast.AST) -> tuple[set, set]:
    """What `asyncpg` and `asyncpg.connect` are CALLED in this file.

    Resolved per file rather than matched by spelling. `import asyncpg as
    pg` and `from asyncpg import connect` are the same call under another
    name, and a guard a rename walks past is not a guard.
    """
    modules, direct = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "asyncpg":
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] != "asyncpg":
                continue
            for alias in node.names:
                if alias.name == "connect":
                    direct.add(alias.asname or alias.name)
                else:
                    modules.add(alias.asname or alias.name)
    return modules, direct


def _opens_a_connection(node: ast.AST, modules: set, direct: set) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in direct
    if not isinstance(func, ast.Attribute) or func.attr != "connect":
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id in modules
    return isinstance(owner, ast.Attribute) and owner.attr in modules


def _sites() -> list[str]:
    found = []
    for path in (ROOT / "kernel").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        modules, direct = _asyncpg_names(tree)
        if not (modules or direct):
            continue
        for node in ast.walk(tree):
            if _opens_a_connection(node, modules, direct):
                found.append(f"{rel}:{node.lineno}")
    return found


def _fires_on(snippet: str) -> bool:
    """Does the guard see a bypass in this source?"""
    tree = ast.parse(textwrap.dedent(snippet))
    modules, direct = _asyncpg_names(tree)
    return any(_opens_a_connection(n, modules, direct)
               for n in ast.walk(tree))


def test_only_the_named_fixtures_open_a_connection_of_their_own():
    """A fourth bypass must fail here rather than at twenty stories.

    The three this replaced were each defensible on their own and
    catastrophic together, and nothing in the running system could have
    told you they existed: they do not error, they make other queries
    wait."""
    offenders = sorted({s.split(":")[0] for s in _sites()} - ALLOWED)
    assert not offenders, (
        "these open their own connection instead of taking one from "
        f"loopbridge's pool: {offenders}. If the process genuinely has no "
        "pool, add it to ALLOWED with the reason; otherwise thread "
        "pool.acquire in the way worker/service.py already does.")


def test_the_guard_is_not_fooled_by_a_rename():
    """`import asyncpg as pg` is the same bypass wearing a different
    name, and a guard that matches on spelling is one rename from being
    decorative."""
    bypasses = (
        """
        import asyncpg
        c = asyncpg.connect(d)
        """,
        """
        import asyncpg as pg
        c = pg.connect(d)
        """,
        """
        from asyncpg import connect
        c = connect(d)
        """,
        """
        from asyncpg import connect as c2
        c = c2(d)
        """,
    )
    for spelling in bypasses:
        assert _fires_on(spelling), spelling
    # and it does not fire on something merely named connect
    assert not _fires_on("""
        import socket
        s = socket.create_connection(d)
        """)


def test_the_three_repaired_sites_are_covered_by_the_guard():
    """The allowlist must not be able to quietly re-admit them."""
    for repaired in ("kernel/answerer/deliver.py",
                     "kernel/credentials/sidecar_http.py",
                     "kernel/harness/leg_runner.py"):
        assert repaired not in ALLOWED


# ── fakes: a pool that records what it hands out ─────────────────────────

class _PooledConn:
    """A connection from the fake pool, and it answers the leg's queries.

    IT USED TO BE AN EMPTY MARKER, and D2 is what made that insufficient:
    `leg_runner` now takes `leg_lock` on this connection and asks
    `loop_gate` whether the leg may start, so a stand-in with no methods
    fails on `pg_try_advisory_lock` before the leg is reached.

    THE ADVISORY LOCK IS THE ONE THAT MATTERS, and the reason is worth a
    paragraph in a fake. `leg_lock` is what stops one execution being
    legged twice, and after D2 the two racers are a pod and a conductor
    on different deploys rather than two ticks. A stand-in that fails to
    answer `fetchval` breaks LOUDLY, which is the lucky version; one that
    answers on a DIFFERENT connection than the one holding the lock
    breaks silently, because an advisory lock dies with its own session
    and would then be released at the wrong moment with nothing raised.
    So the lock is recorded HERE, on the object that took it, and
    `locked_here` below is what a test asserts session affinity with.
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self.locked_here: list[int] = []

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            self.locked_here.append(args[1])
            return True
        if "cancel" in sql:                       # loop_gate's cancellation
            return None
        return None

    async def fetchrow(self, sql, *args):
        if "FROM executions" in sql:
            # `story_id` because `loop_gate` reads it to ask whether THIS
            # errand is held -- a person can pause one autonomous errand
            # from its GO thread without freezing the goal. A fake row
            # missing it fails the gate with a KeyError, which is what
            # this file did when that shipped.
            return {"state": "RUNNING", "goal_id": "KER28",
                    "story_id": "s-1"}
        return None                               # no pause row

    async def fetch(self, sql, *args):
        """Grown for the same reason `fetchrow` was.

        The leg now loads five per-leg facts on this connection before
        it starts -- the plan gates, the egress allowlist, the approved
        calls and the sandbox PATH -- because `leg_runner` passed none
        of them and both plan gates were therefore dead in every pod.
        `approved_calls` and `allowed_hosts` read with `fetch`, and a
        fake without it fails on an AttributeError before the leg is
        reached. Empty is the honest answer for a run with no grants.
        """
        return []

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            self.locked_here.remove(args[1])
        return "OK"


class _Lease:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def __aenter__(self):
        self._pool.live += 1
        self._pool.peak = max(self._pool.peak, self._pool.live)
        conn = _PooledConn(len(self._pool.handed_out))
        self._pool.handed_out.append(conn)
        return conn

    async def __aexit__(self, *exc):
        self._pool.live -= 1
        return False


class _FakePool:
    """Stands in for asyncpg's pool: `acquire()` is the whole surface."""

    def __init__(self) -> None:
        self.handed_out: list[_PooledConn] = []
        self.live = 0
        self.peak = 0
        self.closed = False

    def acquire(self):
        return _Lease(self)

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── site 1: the answerer ─────────────────────────────────────────────────

@pytest.fixture()
def one_claimed_question(monkeypatch):
    """One question waiting, and a record of what answered it on what."""
    answered: list[tuple] = []

    async def _claim(conn, goal_id, limit):
        # `limit` is what makes the deferral real: the drain now takes
        # only as many questions as it can answer, because taking more
        # marked them ACCEPTED and then dropped them.
        assert limit >= 1, "the drain claimed with no room to answer"
        return [{"proposal_id": 11, "execution_id": 3, "actor_id": "a",
                 "question": "why", "asked_by": "someone",
                 "state": "RUNNING"}][:limit]

    async def _reap(conn, goal_id):
        return []

    async def _fallback(conn, goal_id):
        # Stubbed for the same reason `_reap` is: this fixture models
        # "one question waiting", and the ask-to-question handover is
        # machinery it does not have a database for.
        return 0

    async def _answer_one(conn, claim):
        answered.append((conn, claim["proposal_id"]))
        return {"execution_id": claim["execution_id"], "action": "answered"}

    monkeypatch.setattr(deliver, "_claim", _claim)
    monkeypatch.setattr(deliver, "_reap", _reap)
    monkeypatch.setattr(deliver, "_fallback", _fallback)
    monkeypatch.setattr(deliver, "answer_one", _answer_one)
    return answered


def test_an_answer_runs_on_a_connection_the_pool_handed_out(
        one_claimed_question):
    """Beside the run, and counted. The pass keeps its own connection --
    asyncpg refuses two operations on one -- so the answerer needs its
    own; POOL_MAX is what decides how many of those may exist."""
    answered = one_claimed_question
    pool, pass_conn = _FakePool(), object()

    async def go():
        actions = await deliver.deliver_questions(
            pass_conn, None, acquire=pool.acquire)
        await asyncio.gather(*list(deliver._LIVE))
        return actions

    actions = _run(go())

    assert [a["action"] for a in actions] == ["answering"]
    assert len(answered) == 1, "the question was never answered"
    conn, _ = answered[0]
    assert isinstance(conn, _PooledConn), (
        "the answer ran on a connection that did not come from the pool")
    assert conn is not pass_conn, "it borrowed the pass's connection"
    assert pool.live == 0, "the connection was never given back"
    assert pool.peak == 1


def test_a_question_is_answered_inline_when_the_caller_has_no_pool(
        one_claimed_question):
    """The degrade is slowness, not a private connection.

    `adjudicate_pass` and `worker_tick` both fall back to serial when the
    caller supplies no `acquire`. Falling back to `asyncpg.connect`
    instead is the bypass, and it is worse than being slow because
    nothing reports it."""
    answered = one_claimed_question
    pass_conn = object()

    actions = _run(deliver.deliver_questions(pass_conn, None))

    assert [a["action"] for a in actions] == ["answered"]
    assert answered and answered[0][0] is pass_conn
    assert not deliver._LIVE, "it detached a task with nowhere to run it"


# ── site 2: the credential sidecar ───────────────────────────────────────

class _FakeRuntime:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    def call(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)


def test_the_sidecar_mints_on_a_pooled_connection(monkeypatch):
    """It claimed to share `assistant/api.py`'s shape and did not share
    the half that matters: one loop, one pool, a connection per request
    taken from it."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    pool, seen = _FakePool(), []

    async def _mint(conn, body):
        seen.append((conn, body))
        return {"token": "redacted", "expires_in": 60}

    monkeypatch.setattr(sidecar_http, "handle_mint", _mint)
    runtime = _FakeRuntime(pool)
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a: sidecar_http.Handler(runtime, *a))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/mint",
            data=b'{"execution_id": 7}',
            headers={"Authorization": "Bearer s3cret",
                     "Content-Type": "application/json"})
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
    finally:
        srv.shutdown()
        srv.server_close()

    assert body["token"] == "redacted", "the mint did not get through"
    assert len(seen) == 1
    assert isinstance(seen[0][0], _PooledConn), (
        "the authorising query ran on a connection of the sidecar's own")
    assert pool.live == 0, "the connection was never given back"


def test_the_sidecar_opens_one_pool_for_the_process_not_one_per_request(
        monkeypatch):
    built: list[str] = []

    class _Runtime:
        def __init__(self, dsn):
            built.append(dsn)

    class _Server:
        def __init__(self, addr, factory):
            self.addr = addr

        def serve_forever(self):
            return None

    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    monkeypatch.setattr(sidecar_http, "LoopRuntime", _Runtime)
    monkeypatch.setattr(sidecar_http, "ThreadingHTTPServer", _Server)

    sidecar_http.serve("postgresql://unused", 0)

    assert built == ["postgresql://unused"]


def test_the_sidecar_refuses_before_it_opens_anything(monkeypatch):
    """The token check must stay in front of the pool. A minting service
    that connects and only then discovers it cannot authenticate anybody
    has already been pointed at by a worker."""
    built: list[str] = []

    class _Runtime:
        def __init__(self, dsn):
            built.append(dsn)

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(sidecar_http, "LoopRuntime", _Runtime)

    with pytest.raises(RuntimeError):
        sidecar_http.serve("postgresql://unused", 0)
    assert built == []


# ── site 3: the killable leg runner ──────────────────────────────────────

def _leg_args(**over):
    base = dict(
        execution_id=5, generation=0, actor_id="t", dsn="postgresql://unused",
        bridge_port=9100, kind=None, domain=None, adapter="mock",
        envelope_json='{"writable_scope": ["/w"], "workspace_path": "/w"}',
        messages_json='[{"role": "user", "content": "work"}]',
        tools_json="[]", mock_steps=1, token_budget=None, step_budget=10,
        lease_clock=600.0, no_rehydrate=True, progress_path=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_a_leg_runs_on_a_pooled_connection_and_gives_it_back(
        monkeypatch, capsys):
    """A leg keeps its connection for the whole leg -- `leg_lock` is
    connection-scoped -- so it is right that no other leg shares it. It
    is not right that POOL_MAX cannot see it."""
    import kernel.harness.loop as harness_loop
    import kernel.loopbridge as loopbridge

    pool, seen = _FakePool(), []

    async def _make_pool(dsn):
        seen.append(dsn)
        return pool

    async def _run_leg(conn, **kw):
        seen.append(conn)
        return {"ok": True, "execution_id": kw["execution_id"]}

    monkeypatch.setattr(loopbridge, "make_pool", _make_pool)
    monkeypatch.setattr(harness_loop, "run_leg", _run_leg)

    _run(leg_runner.run(_leg_args()))

    assert seen[0] == "postgresql://unused", "it did not build the kernel pool"
    assert isinstance(seen[1], _PooledConn), (
        "the leg ran on a connection it opened for itself")
    assert pool.live == 0, "the leg never released its connection"
    assert pool.closed, "the process exited with its pool still open"
    # D2: the result is a marked, versioned line rather than the whole of
    # stdout. `kernel/harness/loop_wire.py` argues each property; the
    # short version is that the reader may be on a different deploy from
    # the pod, and that stdout is shared with anything that prints.
    assert decode(capsys.readouterr().out)["ok"] is True


def test_the_leg_takes_its_lock_on_the_connection_it_runs_on(monkeypatch):
    """§10's whole reason for putting a database in the pod.

    `leg_lock` is an advisory lock and it dies with the SESSION that took
    it -- which is what makes a crashed worker release a run instead of
    stranding it, and what makes a CONDUCTOR deploy release a lock
    protecting a leg still running in a pod if the lock stays behind. So
    "the pod holds the lock" is not enough on its own: the lock, the leg
    and the sub-agent writes must all be the SAME connection, and one
    pooled connection out of a pod's two is what that costs.
    """
    import kernel.harness.loop as harness_loop
    import kernel.loopbridge as loopbridge

    pool, during = _FakePool(), []

    async def _make_pool(dsn):
        return pool

    async def _run_leg(conn, **kw):
        during.append((conn, list(conn.locked_here)))
        return {"ok": True}

    monkeypatch.setattr(loopbridge, "make_pool", _make_pool)
    monkeypatch.setattr(harness_loop, "run_leg", _run_leg)

    _run(leg_runner.run(_leg_args()))

    assert len(pool.handed_out) == 1, "a second connection is a second claim"
    conn, held = during[0]
    assert conn is pool.handed_out[0]
    assert held == [5], "the leg ran without its own execution's lock held"
    assert conn.locked_here == [], "the lock outlived the leg"


def test_a_leg_refused_by_the_gate_costs_no_model_call(monkeypatch, capsys):
    """D4. A frozen goal stops a RUNNING story at its next leg, and in the
    pod 'the next leg' is this process. The refusal travels in the shape
    `loop_result` reserves for it, so a reader sees `stop_reason` rather
    than a leg that ran and stopped talking."""
    import kernel.harness.loop as harness_loop
    import kernel.loopbridge as loopbridge

    pool, ran = _FakePool(), []

    async def _make_pool(dsn):
        return pool

    async def _run_leg(conn, **kw):
        ran.append(conn)
        return {"ok": True}

    async def _frozen(conn, goal_id=None, story_id=None):
        return {"scope": "system", "scope_id": "", "reason": "incident",
                "paused_by": "abhishek"}

    monkeypatch.setattr(loopbridge, "make_pool", _make_pool)
    monkeypatch.setattr(harness_loop, "run_leg", _run_leg)
    monkeypatch.setattr(pauses, "frozen", _frozen)

    _run(leg_runner.run(_leg_args()))

    assert ran == [], "the kill switch did not reach the pod's leg"
    assert decode(capsys.readouterr().out)["stop_reason"] == "frozen"
    assert pool.handed_out[0].locked_here == [], "the lock was not released"
    assert pool.closed
