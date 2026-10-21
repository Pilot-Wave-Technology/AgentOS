"""Ec4d — the three rails the design rested on and did not have.

The credential road was built and wired, and three things it assumed
were still missing: a service to serve the sidecar, a block stopping the
worker's namespace reaching the metadata server, and anything at all
calling the sweep. A design whose load-bearing parts are unbuilt is a
design that reads as safe.
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── the sidecar is actually served ───────────────────────────────────────

def test_the_sidecar_refuses_to_start_without_its_secret():
    """Every other component degrades politely when misconfigured. This
    one must not: an unauthenticated minting service, in the one
    namespace that can mint, is worse than no service -- everything
    downstream would report success."""
    import os

    from kernel.credentials.sidecar_http import serve

    was = os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_TOKEN", None)
    try:
        try:
            serve("postgresql://unused", 0)
            raise AssertionError("served without a secret")
        except RuntimeError as exc:
            assert "SIDECAR_TOKEN" in str(exc)
    finally:
        if was is not None:
            os.environ["AGENTOS_CREDENTIAL_SIDECAR_TOKEN"] = was


def test_health_says_nothing_about_what_is_reachable():
    """A health endpoint listing the identities it can mint would be a
    map of the blast radius, served unauthenticated."""
    src = _read("kernel/credentials/sidecar_http.py")
    body = src[src.index("def do_GET"):]
    body = body[:body.index("\n    def ", 10)]
    assert '{"ok":true}' in body
    for leaked in ("sa_email", "account", "project", "identity", "granted"):
        assert leaked not in body, f"health leaks {leaked}"


def test_a_failed_mint_never_returns_its_exception_text():
    """A mint failure can carry a target principal, a project, and
    occasionally a token fragment. The detail belongs in the sidecar's
    own log and nowhere else."""
    src = _read("kernel/credentials/sidecar_http.py")
    i = src.index("except Exception:")
    tail = src[i:i + 500]
    assert "logger.exception" in tail
    assert "see the sidecar log" in tail
    # The caught exception must not be interpolated into the response.
    assert "{exc" not in tail


# ── the metadata block ───────────────────────────────────────────────────

def test_the_metadata_guard_fails_closed_when_it_cannot_probe():
    """THE BUG THIS FILE EXISTS TO PREVENT, made by me while writing it.
    The probe produced no output -- `docker exec` without `-i` does not
    forward stdin, so the heredoc never reached python -- and the
    catch-all branch read that silence as success.

    "The probe did not run" and "the probe said no" are different facts
    and only one is safe to act on."""
    src = _read("infra/e0/metadata-guard.sh")
    assert "docker exec -i" in src, (
        "the probe's stdin is not forwarded, so it cannot run at all")
    assert "UNKNOWN" in src and "Treating as NOT blocked" in src


def test_the_guard_separates_blocked_from_routable_but_closed():
    """`ConnectionRefused` means the address is ROUTABLE and nothing is
    listening -- what a laptop looks like, not what a DROP rule does. A
    rule produces a timeout. Conflating them reports a laptop as
    protected."""
    src = _read("infra/e0/metadata-guard.sh")
    assert "socket.timeout" in src
    assert "ConnectionRefusedError" in src
    assert "norule" in src, (
        "routable-but-closed is not distinguished, so an unprotected "
        "host reads as a protected one")


def test_the_guard_blocks_one_address_for_every_cloud():
    """169.254.169.254 is the metadata address on GCP, AWS and Azure
    alike, so there is no per-cloud rule to forget."""
    src = _read("infra/e0/metadata-guard.sh")
    assert "169.254.169.254" in src
    assert "OUTPUT" in src and "FORWARD" in src


# ── the sweep actually runs ──────────────────────────────────────────────

def test_the_sweep_is_scheduled():
    """It was written, tested, and called by nothing."""
    # Moved out of the console: the loop IS the execution plane and
    # was being started as a side effect of binding a debug port.
    # Then out of `service.py` too (D2a) -- that file is now the
    # PROCESS (entrypoint, signals, startup order) and `run_forever`
    # is its own module. The end of the slice went with it:
    # `run_forever` is the only function in `service_tick.py`, so the
    # body runs to the end of the file and there is nothing to cut.
    body = _read("kernel/worker/service_tick.py")
    body = body[body.index("async def run_forever"):]
    assert "credentials.sweep" in body, "nothing calls the credential sweep"
    assert "SWEEP_EVERY_TICKS" in body


def test_a_failing_sweep_cannot_stop_the_worker():
    """The runs are the product and the sweep is hygiene. Letting
    hygiene take down the product is how a safety measure becomes an
    outage.

    Anchored on the `try:` that encloses the call rather than on a
    character window around it. The window version broke the moment F4
    added a paragraph of comment, which says nothing about whether the
    sweep is still guarded -- a test that fails when the prose grows is
    measuring the prose."""
    # Moved out of the console, then out of `service.py` (D2a): see
    # the test above for why the end of the slice went away with it.
    body = _read("kernel/worker/service_tick.py")
    body = body[body.index("async def run_forever"):]
    # ANCHOR ON THE CALL, not the import. `credentials.sweep` now appears
    # first at the top-of-function import, which sits outside the guard
    # and always will -- so matching that measured the import's position
    # rather than the call's, and reported a correctly-guarded sweep as
    # unguarded the moment the import moved.
    i = body.index("await credential_sweep(")
    # The guarding `try:` opens before the call and its handler closes
    # after it; both must exist on the same side of the call.
    assert "try:" in body[:i], "the sweep call is not inside a try"
    assert "credential sweep failed" in body[i:], (
        "nothing catches a failing sweep, so hygiene can stop the worker")
