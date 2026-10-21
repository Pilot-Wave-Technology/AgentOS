"""H1c — a bridge port per sandbox, allocated rather than assumed.

`Driver.exec` has defaulted to 9100 since ES3, with a docstring describing
a container label that nothing set and nothing read. That was survivable
while one sandbox existed at a time. It is not survivable now: every
sandbox joins the WORKER's network namespace, so two concurrent executions
would both try to bind 9100 — the second shim dies on startup, and the
first execution's tool calls answer the second execution's questions.

The H1a test had to hand-pick 9137 to avoid exactly this, which is the
clearest possible sign the default was doing real work by accident.

WHY BIND-TESTING RATHER THAN ARITHMETIC. `9100 + execution_id % 100` is
tempting and wrong: execution ids are not dense, two live executions can
collide modulo anything, and a collision is silent — the port is taken, the
shim exits, and dispatch reports a container that started. Asking the
kernel which port is free is the only answer that is true at the moment it
is asked. Sandboxes share the worker's namespace, so a bind test here tests
the same stack the shim will bind in.

The remaining race — two allocations between the test and the bind — is
closed by the label: whoever ends up on a port has it recorded, and
`resolve_bridge_port` reads the container rather than recomputing.
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)

# Range for sandbox bridges. Above the default so an old hardcoded 9100
# cannot silently land inside it and look allocated.
PORT_BASE = 9200
PORT_COUNT = 200
BRIDGE_PORT_LABEL = "agentos.bridge-port"


class NoFreePortError(Exception):
    """Every port in the range is taken."""


def _is_free(port: int) -> bool:
    """True if nothing holds this port in OUR namespace.

    SO_REUSEADDR is deliberately NOT set: it would let the bind succeed
    against a socket in TIME_WAIT and report a port free that the shim
    will then fail to claim.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def allocate_bridge_port(taken: set[int] | None = None) -> int:
    """First free port in the range. Raises rather than falling back.

    `taken` lets a caller exclude ports it has allocated but not yet bound —
    the window between this returning and the shim starting.
    """
    taken = taken or set()
    for port in range(PORT_BASE, PORT_BASE + PORT_COUNT):
        if port in taken:
            continue
        if _is_free(port):
            logger.info("allocated bridge port %d", port)
            return port
    raise NoFreePortError(
        f"no free port in {PORT_BASE}-{PORT_BASE + PORT_COUNT - 1}; "
        f"{len(taken)} held by this worker. A sandbox that cannot get a "
        "port must not silently share one."
    )
