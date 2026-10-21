"""The workspace server is in-cluster, so no call to it may be proxied.

MEASURED ON EXECUTION 224. Publish failed three generations running on

    'heads' was REFUSED by the workspace at 10.48.128.82: HTTP 403.
    that token may read this workspace and not change it

and the message is wrong in an expensive way. podfs's own gate CANNOT
refuse `heads`: it is in `READ_ONLY_OPS`, so a read-only token permits
it, and a full token permits everything. The 403 never came from podfs.

IT CAME FROM SQUID. `k8s_egress` says pod-to-pod "is not the proxy's
business either" and arranges it by putting `10.0.0.0/8` in `NO_PROXY`.
That works for anything reading the list as a CIDR and not for
`urllib`, whose bypass matches hostnames and suffixes. So a pod IP is
sent to the egress proxy, which refuses a private destination with 403.

THE CONDUCTOR NEVER HIT IT because the conductor has no proxy variables
at all -- the same reason `k8s_egress` records that the metadata-server
failure "only ever bit the Jobs". The leg-in-pod cutover made a proxied
process the client, which is the third time today that move has found
an assumption that only held on the conductor.

AND THE CLIENT MISTRANSLATED IT, which is why it cost a run rather than
a minute: `call` turns ANY 403 into a scope refusal, so a network denial
arrives dressed as an authorisation bug and sends the reader to
`podfs_auth.py`, where nothing is wrong.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_opener_will_not_route_through_a_proxy():
    """`build_opener(ProxyHandler({}))` drops proxying from the chain --
    an empty handler contributes no `*_open` method, so no ProxyHandler
    survives in `handlers` at all. That absence IS the property."""
    from urllib.request import ProxyHandler

    from kernel.workspace.podfs_client import _DIRECT

    proxying = [h for h in _DIRECT.handlers
                if isinstance(h, ProxyHandler) and h.proxies]
    assert not proxying, (
        f"the podfs opener would route through {proxying}; an in-cluster "
        f"address sent to the egress proxy comes back 403")


def test_nothing_dials_podfs_with_the_bare_urlopen():
    """A second call site using module-level `urlopen` would pick the
    environment's proxy back up, and the failure would look like an
    authorisation bug again."""
    body = _src("kernel/workspace/podfs_client.py")

    assert "urlopen(" not in body, (
        "a podfs call bypasses the direct opener, so it inherits "
        "HTTP_PROXY from whatever process it runs in")
    assert "_DIRECT.open(" in body


def test_the_reason_is_recorded_where_the_next_reader_will_be():
    """The next person here will have been sent by a 403 that says
    `podfs_auth.py`, which is the wrong file."""
    body = _src("kernel/workspace/podfs_client.py")
    i = body.index("_DIRECT = build_opener")
    window = body[max(0, i - 2200):i]

    assert "403" in window and "squid" in window.lower(), (
        "nothing here says a 403 can come from the proxy rather than "
        "from the scope gate")
    assert "READ_ONLY_OPS" in window, (
        "the note does not say why podfs itself cannot be the source")


def test_heads_could_never_have_been_a_scope_refusal():
    """The fact that made the message a lie, pinned so it stays true."""
    from kernel.workspace.podfs_auth import READ_ONLY_OPS, permits

    assert "heads" in READ_ONLY_OPS
    assert permits("readonly", "heads")
    assert permits("full", "heads")
