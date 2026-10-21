"""`is_reachable` — the guard between a placeholder DSN and nine errors.

WHAT IT IS FOR. `test_a_bare_database_becomes_a_ledger.py` needs a real
Postgres and skips without one. It used to decide that on
`not os.environ["DATABASE_URL"]`, which is a question about a STRING,
not about a server.

`app/services/model_registry/tests/conftest.py` sets

    os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost:5432/x")

at import time, and rightly: `app.config.get_settings()` must construct
before any `app.*` module imports and that field is required. It is a
SETTINGS PLACEHOLDER. Run the package's tests alone and the variable is
unset, so everything skipped and looked fine. Run the whole suite in one
process and the placeholder arrived first, the guard believed it, and
nine tests ERRORED against a server that was never there -- found by an
executor pod, not by us.

THE SECOND VERSION WAS ALSO WRONG, which is why these tests are
specific. A socket probe on `localhost:5432` passes on any developer
machine running its own Postgres -- the port answers, the credentials
then fail, and the nine errors come back unchanged. "Is a port open" is
not the question. "Can these tests use this database" is.
"""
from __future__ import annotations

import socket
import threading

import pytest

from agentos_ledger_client.testing import is_reachable


def test_an_empty_dsn_is_not_reachable():
    assert is_reachable("") is False
    assert is_reachable(None or "") is False


def test_nothing_listening_is_not_reachable():
    """A closed port, bound and released so the number is really free."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert is_reachable(f"postgresql://x:x@127.0.0.1:{port}/x",
                        timeout=0.5) is False


def test_a_port_that_answers_but_is_not_postgres_is_not_reachable():
    """THE CASE A SOCKET PROBE GETS WRONG, and the reason this opens a
    real connection.

    Something is listening -- the TCP handshake completes -- but it will
    never speak the protocol. The probe version returned True here, and
    the caller then failed at connect time, which is exactly the error
    the guard exists to prevent.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    stop = threading.Event()

    def _accept():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conn.close()                       # accept, say nothing, hang up

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        assert is_reachable(f"postgresql://x:x@127.0.0.1:{port}/x",
                            timeout=1.0) is False
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2)


def test_a_genuinely_working_database_is_not_dismissed():
    """The other direction, and it needs a REAL server to mean anything.

    A guard that answers False too readily turns every database test
    into a silent skip -- the same failure wearing the other face.

    THIS ASKED THE WRONG QUESTION FIRST. It read `DATABASE_URL` from the
    environment and asserted it was reachable, which is tautological
    when the value is good and a FAILURE when someone runs with a DSN
    that happens to point nowhere -- the test then reports that
    `is_reachable` is broken, when `is_reachable` was right and the
    environment was wrong. Caught by running the negative case.

    So it brings up its own server and asks about that.
    """
    pgserver = pytest.importorskip(
        "pgserver", reason="no embedded Postgres available to prove a True")
    import pathlib
    import tempfile

    from agentos_ledger_client.testing import dsn_for

    data = pathlib.Path(tempfile.gettempdir()) / "agentos-suite-pgdata"
    data.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(data))
    assert is_reachable(dsn_for(server.get_uri(), "postgres")) is True


@pytest.mark.parametrize("junk", [
    "not-a-url",
    "postgresql://",
    "postgresql://host-that-does-not-exist.invalid:5432/x",
])
def test_junk_does_not_raise(junk):
    """It is a predicate, not a parser. Anything unusable is False, and
    nothing it is handed may escape as an exception -- it runs inside a
    `skipif`, where a raise is a collection error for the whole module.
    """
    assert is_reachable(junk, timeout=0.5) is False
