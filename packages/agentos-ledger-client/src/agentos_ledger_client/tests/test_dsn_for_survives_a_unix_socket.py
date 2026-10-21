"""The URI helper, tested against the shape that broke it.

WHY THIS TEST EXISTS AND WHY IT USES LITERALS. The bug it pins is
invisible on the machine most of this was written on: `pgserver` hands
back a TCP URI on Windows and a UNIX-SOCKET URI on Linux, and the broken
implementation -- `uri.rsplit("/", 1)[0] + "/" + dbname` -- is exactly
correct on the first and silently wrong on the second.

So a test that asks the local `pgserver` for a URI proves nothing: on
Windows it would pass against the broken version too. The Linux shape is
written out as a literal, which is the only way this is checkable
everywhere.

It was found by an executor pod running the suite on Linux for the first
time, after the suite had been declared green on Windows.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from agentos_ledger_client.testing import dsn_for

# What `pgserver.get_server(...).get_uri()` actually returns, per platform.
LINUX_SOCKET = "postgresql://postgres:@/postgres?host=/tmp/agentos-suite-pgdata"
WINDOWS_TCP = "postgresql://postgres:@127.0.0.1:49778/postgres"


def test_the_socket_directory_survives():
    """The whole bug in one assertion.

    The old `rsplit` produced `...?host=/tmp/agentos_test` -- it appended
    the database name to the SOCKET DIRECTORY and left the database as
    `postgres`. The connection then failed with "No such file or
    directory", which reads as "there is no database here".
    """
    out = dsn_for(LINUX_SOCKET, "agentos_test")
    assert parse_qs(urlparse(out).query)["host"] == ["/tmp/agentos-suite-pgdata"], (
        f"the socket directory was altered: {out}")
    assert urlparse(out).path == "/agentos_test", (
        f"the database name did not land on the path: {out}")


def test_the_tcp_shape_still_works():
    """The platform the broken version was right on must stay right."""
    assert dsn_for(WINDOWS_TCP, "agentos_test") == (
        "postgresql://postgres:@127.0.0.1:49778/agentos_test")


def test_the_old_implementation_would_fail_this():
    """Pinned so nobody reintroduces the slice as a 'simplification'.

    Asserting the BROKEN behaviour is deliberate: it documents what the
    shortcut does, and it fails loudly if someone decides the two are
    equivalent after all.
    """
    broken = LINUX_SOCKET.rsplit("/", 1)[0] + "/agentos_test"
    assert broken != dsn_for(LINUX_SOCKET, "agentos_test")
    assert urlparse(broken).path == "/postgres", (
        "the slice leaves the database as `postgres` -- that is the bug")
    assert parse_qs(urlparse(broken).query)["host"] == ["/tmp/agentos_test"], (
        "the slice moves the socket directory -- that is the other half")


@pytest.mark.parametrize("dbname", ["agentos_test", "/agentos_test"])
def test_a_leading_slash_is_not_doubled(dbname):
    assert urlparse(dsn_for(LINUX_SOCKET, dbname)).path == "/agentos_test"
