"""Ec4d — serving the sidecar. `python3 -m kernel.credentials.sidecar_http`.

THE ONE PROCESS ON THE HOST PERMITTED TO MINT. It runs in its own
network namespace with the metadata server reachable, while the worker's
namespace -- which the sandbox shares -- has 169.254.169.254 blocked.
That separation is what makes the grant the boundary rather than a
suggestion.

SAME SHAPE AS `kernel/assistant/api.py`: ThreadingHTTPServer, a `serve`
that takes a DSN, and a `__main__` that reads it from the environment.
Deliberately the same, because a second serving pattern in one codebase
is a second set of mistakes -- and this one already learned that binding
0.0.0.0 without a token is how a surface becomes open.

IT SAID THAT AND THEN KEPT ITS OWN CONNECTION. The half of the shape
that matters most was the half it did not copy: `api.py` holds a
`LoopRuntime`, which is one asyncio loop on a background thread and ONE
pool, and it exists precisely because `http.server` is threaded and
asyncpg is not. This module instead held a single `asyncpg.connect`
behind a lock and ran each request under its own `asyncio.run` -- a new
event loop per POST, reusing a connection made on a loop that had since
been closed. Three separate things followed: the connection was outside
`loopbridge`'s pool and therefore uncounted, it never ran `SET ROLE
agentos_worker` so it authorised grants as the superuser, and its
`search_path` was a second copy of a setting `make_pool` owns.

IT REFUSES TO START WITHOUT ITS SECRET. Every other component in this
build degrades politely when it is misconfigured; this one must not. An
unauthenticated minting service in the namespace that can mint is worse
than no service, because everything downstream would report success.
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kernel.credentials.sidecar_server import (SidecarRefused, check_auth,
                                               error_body, handle_mint)
from kernel.loopbridge import LoopRuntime
from kernel.obs import configure

logger = logging.getLogger(__name__)

PORT = 8795
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    def __init__(self, runtime, *args):
        self.runtime = runtime
        super().__init__(*args)

    async def _mint(self, body: dict) -> dict:
        """One pooled connection, held only for the authorising query.

        The old shape argued that "a pool here would be machinery for a
        load that does not exist". Rarity was never the question: one
        uncounted connection is still one nobody can see, and the
        machinery already exists and is shared -- `LoopRuntime` is what
        the other two stdlib servers in this build use, and taking it
        costs this module a class it no longer has to own.
        """
        async with self.runtime.pool.acquire() as conn:
            return await handle_mint(conn, body)

    def do_POST(self):                                      # noqa: N802
        if self.path.rstrip("/") != "/mint":
            return self._refuse(404, "only POST /mint is served")
        try:
            check_auth(self.headers.get("Authorization"))
        except SidecarRefused as refused:
            # The 401 is logged WITHOUT the presented value. A rejected
            # token in a log is still a token somebody can try elsewhere.
            logger.warning("sidecar: refused an unauthenticated caller")
            return self._send(refused.status, error_body(refused))

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._refuse(413, "request body is too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._refuse(400, "body must be JSON")
        if not isinstance(body, dict):
            return self._refuse(400, "body must be a JSON object")

        try:
            minted = self.runtime.call(self._mint(body))
        except SidecarRefused as refused:
            logger.warning("sidecar: %s", refused.message)
            return self._send(refused.status, error_body(refused))
        except Exception:                                   # noqa: BLE001
            # NEVER THE EXCEPTION TEXT. A mint failure can carry a target
            # principal, a project, and occasionally a token fragment;
            # the detail belongs in this process's log and nowhere else.
            logger.exception("sidecar: minting failed")
            return self._refuse(500, "minting failed; see the sidecar log")

        return self._send(200, json.dumps(minted).encode("utf-8"))

    def do_GET(self):                                       # noqa: N802
        """Liveness only, and deliberately says nothing else.

        A health endpoint that reported which identities are reachable
        would be a map of the blast radius, served unauthenticated.
        """
        if self.path.rstrip("/") in ("/health", ""):
            return self._send(200, b'{"ok":true}')
        return self._refuse(404, "not found")

    def _refuse(self, status: int, message: str):
        return self._send(status, json.dumps({"refused": message}).encode())

    def _send(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("sidecar: " + fmt, *args)


def serve(dsn: str, port: int = PORT):
    # REFUSED AT START, not at the first request. A sidecar that accepts
    # connections and only then discovers it cannot authenticate anybody
    # is one somebody has already pointed a worker at.
    if not (os.environ.get("AGENTOS_CREDENTIAL_SIDECAR_TOKEN") or "").strip():
        raise RuntimeError(
            "AGENTOS_CREDENTIAL_SIDECAR_TOKEN is not set. This service mints "
            "cloud credentials and runs in the one namespace that can; it "
            "will not start without a way to authenticate its caller.")

    # AFTER the token check and not before: a sidecar that cannot
    # authenticate anybody must refuse without having opened anything.
    # Connecting here rather than at the first request is the same
    # argument one paragraph up -- a misconfigured DSN should stop the
    # deploy, not surface as a 500 the first time a run needs a token.
    runtime = LoopRuntime(dsn)
    server = ThreadingHTTPServer(("0.0.0.0", port), lambda *a: Handler(runtime, *a))
    logger.info("credential sidecar on 0.0.0.0:%d (authenticated)", port)
    server.serve_forever()


if __name__ == "__main__":
    configure(component="credential-sidecar")
    serve(os.environ["DATABASE_URL"])
