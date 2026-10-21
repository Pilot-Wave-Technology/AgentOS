"""`python -m kernel.verifier.service` does the four things a loop must.

WHY THIS IS NOT COVERED BY THE MANIFEST TEST. The manifest can only say
which command is run. Everything that made `kernel/worker/service.py`
survivable is INSIDE the process and invisible from YAML: the executor
pool sized before anything awaits, the database wait that spans the
cloud-sql-proxy's first second, the per-pass liveness file the probe
reads from outside, the drain that keeps a SIGTERM from discarding work
already paid for, and the stack dumper that finally named the wedge.

Each of those was a defect once, and the second entrypoint is where they
get silently left out -- a new loop looks correct with none of them,
right up until its first rolling update or its first hung call.

AND THE PASSES ARE IMPORTED, NOT REIMPLEMENTED. The expensive lesson in
this repository is the second copy: `kernel/console/server.py` grew a
second `worker_tick`, it drifted, and `deliver_answers`,
`deliver_cancellations` and `adjudicate_pass` reached only one of them --
so a parked run could not be answered and NOTHING EVER ADJUDICATED. A
verifier Deployment is exactly the moment that could happen again.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

from kernel.obs import alive
from kernel.verifier import service


# ── fakes, the same shape the other loop tests use ────────────────────

class _Conn:
    def __init__(self):
        self.statements = []

    async def execute(self, *args, **kwargs):
        self.statements.append(args)
        return "OK"


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        conn = _Conn()
        self.pool.handed_out.append(conn)
        return conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self):
        self.handed_out = []
        self.closed = False

    def acquire(self):
        return _Acquire(self)

    async def close(self):
        self.closed = True


class _Driver:
    def __init__(self):
        self.preflighted = False

    def preflight(self):
        self.preflighted = True


# ── the pass is the real one, and it writes a heartbeat ───────────────

@pytest.mark.asyncio
async def test_one_tick_runs_the_verifier_pass(monkeypatch, tmp_path):
    """ONE PASS, and it is adjudication. There were two until E9 retired
    the dry run (plan 5.6); what this still guards is the pattern that
    made a second one necessary to assert -- a pass that is written and
    never called is the defect this project keeps rediscovering."""
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    calls = []
    stopping = asyncio.Event()

    async def _adjudicate(conn, driver, goal_id=None, acquire=None,
                          supervisor=None):
        calls.append(("adjudicate", conn, acquire is not None,
                      supervisor is not None))
        # ONE TICK. The dry-run stub used to stop the loop after the
        # second pass; with one pass left this is where it stops.
        stopping.set()
        return [{"action": "verdict"}]

    monkeypatch.setattr("kernel.verifier.adjudicator.adjudicate_pass",
                        _adjudicate)
    driver = _Driver()
    monkeypatch.setattr("kernel.drivers.registry.make", lambda *a, **k: driver)

    pool = _Pool()
    await service.run_forever(pool, interval=0.01, stopping=stopping)

    assert [c[0] for c in calls] == ["adjudicate"]
    # AND IT COMES FROM THE POOL. An uncounted `asyncpg.connect` per pass
    # is how the pool came to be exhausted by consumers nothing was
    # counting.
    assert calls[0][1] in pool.handed_out
    assert driver.preflighted, (
        "a driver whose client is missing must fail the rollout, not log "
        "ModuleNotFoundError once a tick for ever")


@pytest.mark.asyncio
async def test_adjudication_is_given_what_makes_it_concurrent(monkeypatch,
                                                              tmp_path):
    """`adjudicate_pass` is serial unless it gets BOTH `acquire` and
    `supervisor`, and serial means one slow suite holds this loop for as
    long as it runs -- which is what it used to do to the conductor's
    whole tick, before E12 took it out of there."""
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    seen = {}
    stopping = asyncio.Event()

    async def _adjudicate(conn, driver, goal_id=None, acquire=None,
                          supervisor=None):
        seen["acquire"] = acquire
        seen["supervisor"] = supervisor
        stopping.set()
        return []

    monkeypatch.setattr("kernel.verifier.adjudicator.adjudicate_pass",
                        _adjudicate)
    monkeypatch.setattr("kernel.drivers.registry.make",
                        lambda *a, **k: _Driver())

    await service.run_forever(_Pool(), interval=0.01, stopping=stopping)
    assert seen["acquire"] is not None
    assert seen["supervisor"] is not None


@pytest.mark.asyncio
async def test_the_pass_records_liveness_the_probe_can_read(monkeypatch,
                                                            tmp_path):
    """THE FILE, NOT THE DATABASE. The probe has to work when the process
    is wedged, which is when a round trip is least trustworthy -- and the
    row is written by the loop that has stopped. `kernel/obs/alive.py`
    exists because both conductors sat READY 2/2 doing nothing for thirty
    minutes."""
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    stopping = asyncio.Event()

    async def _adjudicate(conn, driver, goal_id=None, acquire=None,
                          supervisor=None):
        stopping.set()
        return []

    monkeypatch.setattr("kernel.verifier.adjudicator.adjudicate_pass",
                        _adjudicate)
    monkeypatch.setattr("kernel.drivers.registry.make",
                        lambda *a, **k: _Driver())

    await service.run_forever(_Pool(), interval=0.01, stopping=stopping)

    written = [p.name for p in tmp_path.iterdir()]
    assert service.VERIFY_LOOP in written, written
    # The probe judges this loop against its OWN declared cadence, which
    # is the second field of the file. Without it, one stalled loop among
    # healthy ones is invisible.
    assert alive._declared(service.VERIFY_LOOP) == service.INTERVAL_S


@pytest.mark.asyncio
async def test_a_failing_pass_does_not_stop_the_loop(monkeypatch, tmp_path):
    """A verification that raises is one story's problem. The loop
    carrying on is what keeps it from becoming every story's -- and after
    E the log is the only witness, which §9.2 names as what E costs."""
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    ticks = []
    stopping = asyncio.Event()

    async def _adjudicate(conn, driver, goal_id=None, acquire=None,
                          supervisor=None):
        ticks.append(1)
        if len(ticks) >= 2:
            stopping.set()
        raise RuntimeError("a criterion blew up")

    monkeypatch.setattr("kernel.verifier.adjudicator.adjudicate_pass",
                        _adjudicate)
    monkeypatch.setattr("kernel.drivers.registry.make",
                        lambda *a, **k: _Driver())

    await service.run_forever(_Pool(), interval=0.01, stopping=stopping)
    assert len(ticks) >= 2, "one bad pass killed the loop"


# ── shutdown, which is where work gets thrown away ────────────────────

def test_the_loop_drains_before_it_returns():
    """The worker's version of this defect was a documented function with
    no caller, and every rolling update discarded whatever was in
    flight. An adjudication is a criterion container already paid for."""
    assert ".drain(" in inspect.getsource(service.drain)
    assert "drain(" in inspect.getsource(service.run_forever)


@pytest.mark.asyncio
async def test_a_running_adjudication_finishes(tmp_path):
    from kernel.worker.supervisor import LegSupervisor

    done = []

    async def _slow():
        await asyncio.sleep(0.05)
        done.append("finished")
        return []

    sup = LegSupervisor()
    assert sup.spawn(1, _slow())
    await service.drain(sup)
    assert done == ["finished"]
    assert not sup.in_flight()


def test_sigterm_marks_the_process_as_draining(tmp_path, monkeypatch):
    """A DRAINING PROCESS IS NOT A WEDGED ONE. The loop stops ticking the
    moment SIGTERM lands, and the drain may run for AGENTOS_DRAIN_MAX_S
    (180s) under a 210s grace period -- long enough for the liveness
    probe to fire and SIGKILL the pod mid-drain, which turns the safety
    measure into the thing that discards the work."""
    monkeypatch.setattr(alive, "BEAT_DIR", str(tmp_path))
    stopping = asyncio.Event()
    service._on_signal(stopping, "SIGTERM")
    assert stopping.is_set()
    healthy, why = alive.verdict(0.0)
    assert healthy, why
    alive.draining(False)


# ── the things that are invisible from the manifest ───────────────────

def test_the_executor_pool_is_sized_before_anything_awaits():
    """Until `executors.install` runs, every `to_thread` in this process
    lands in a pool sized from the NODE's cpu count rather than this
    container's limit. This is the process Phase R sized `infra` for:
    tar slicing, git archive and Kubernetes calls are all threaded."""
    src = inspect.getsource(service.main)
    install = src.index("executors.install")
    assert install < src.index("wait_for_pool"), (
        "the pool is built before the executor is sized")
    assert "executors.shutdown()" in src


def test_it_waits_for_the_database_rather_than_exiting():
    """The cloud-sql-proxy is a native sidecar, so this container starts
    once the proxy has STARTED -- not once it is accepting connections.
    The gap is under a second and it cost a RESTARTS=1 on every pod of
    every deploy, and a smoke check that failed a healthy cluster."""
    assert "wait_for_pool" in inspect.getsource(service.main)


def test_the_stack_dumper_is_registered():
    """A wedged process tells you it is waiting and not what for, and
    `py-spy` cannot attach because the container drops the capability it
    needs to ptrace. The probe fires SIGUSR1 on its way to failing, so
    the dump lands in the log seconds before the kill with nobody
    watching."""
    src = inspect.getsource(service._dump_stacks_on_sigusr1)
    assert "faulthandler.register" in src and "SIGUSR1" in src
    assert "_dump_stacks_on_sigusr1()" in inspect.getsource(service.main)


def test_the_pool_close_is_bounded():
    """`Pool.close()` waits for every connection to be released, and a
    cancelled adjudication is exactly the case where one is not.
    Unbounded, the process then sits until SIGKILL on every rolling
    update -- spending the grace period the drain was meant to use."""
    src = inspect.getsource(service.main)
    assert "POOL_CLOSE_MAX_S" in src and "pool.terminate()" in src


# ── the two house rules this file is the natural place to check ───────

def test_the_verifier_reimplements_its_pass():
    """The pass is IMPORTED. A second copy is how `worker_tick` came to
    exist twice, and the copy that drifted was the one nobody was
    watching. It said "neither pass" while there were two; E9 retired the
    dry run and E12 made this deployment the ONLY caller of the one
    left -- which makes a drifted copy here unobservable from anywhere
    else, so the rule matters more than it did, not less."""
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    assert "from kernel.verifier.adjudicator import adjudicate_pass" in src
    assert "def adjudicate_pass" not in src


def test_it_does_not_import_from_app():
    """`kernel/` may never import `app/`: the kernel runs in a container
    the control panel is not in."""
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    assert "import app" not in src and "from app" not in src


def test_no_invisible_default_in_the_environment():
    """`os.environ.get(x, default)` is banned under kernel/ -- a default
    nobody can see is a policy nobody reviewed. The one read here is
    DATABASE_URL, which must raise rather than resolve to something."""
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "environ.get(" not in code
    assert 'os.environ["DATABASE_URL"]' in code
