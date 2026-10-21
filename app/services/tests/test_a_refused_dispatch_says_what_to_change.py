"""The 403 half of the xlarge quota trap, which said nothing useful.

WHAT Q1 FOUND AND COULD NOT FIX. Item D5 measured that sixteen `xlarge`
executions cost 132 vCPU and 520 GiB against a namespace quota of 128
and 512Gi -- over on BOTH axes (plan 11.5i). Q1 then made the Pending
half legible. But `ResourceQuota` is an ADMISSION plugin, so the pod
that crosses the line is more likely **refused at `create` with a 403**
than left Pending -- and that is the likelier half.

`runner.dispatch_pass` did store it, as
`f"dispatch failed: {type(e).__name__}: {e}"[:500]`. The trouble is
`ApiException.__str__`, which renders the HTTP RESPONSE HEADERS BEFORE
the body. On GKE those headers are an `Audit-Id`, two `X-Kubernetes-Pf-*`
uids, a `Date`, `Cache-Control`, `Content-Type` and `Content-Length`.
Measured against a reconstructed refusal: the whole string is **698
characters and the message starts at offset 521**, so the 500-character
cut landed inside `{"kind":"Status","apiVersion":"v1",...,"status":`.

The operator got `Reason: Forbidden` and a header dump. Not one of
`exceeded quota`, `requested:`, `used:` or `limited:` survived -- so
they were told the request was refused, and nothing about what to
change. Both halves of the D5 trap misled, for unrelated reasons.

THE FIRST TEST HERE IS THE NON-VACUITY PROOF: it asserts the OLD
expression loses the message and the new one keeps it, against the same
object. Without that pair this file would pass just as happily against
the bug.
"""
from __future__ import annotations

from kernel.drivers import k8s_error

# The API server's own sentence -- the only place the numbers live.
MESSAGE = ('pods "exec-42" is forbidden: exceeded quota: '
           'agentos-staging-quota, requested: requests.cpu=8250m, '
           'used: requests.cpu=120, limited: requests.cpu=128')

BODY = ('{"kind":"Status","apiVersion":"v1","metadata":{},'
        '"status":"Failure","message":"' + MESSAGE.replace('"', '\\"') +
        '","reason":"Forbidden","details":{"kind":"pods"},"code":403}')

# GKE's real header set. The size is the whole point, so it is not
# trimmed to something tidy.
HEADERS = {
    "Audit-Id": "b" * 36,
    "Cache-Control": "no-cache, private",
    "Content-Type": "application/json",
    "X-Kubernetes-Pf-Flowschema-Uid": "a" * 36,
    "X-Kubernetes-Pf-Prioritylevel-Uid": "c" * 36,
    "Date": "Thu, 18 Sep 2026 10:00:00 GMT",
    "Content-Length": "412",
}


class FakeApiException(Exception):
    """`ApiException` without the dependency.

    `kernel/` stays importable with no kubernetes package -- the driver
    imports it lazily and this suite runs with no cluster and no client
    library. `k8s_error` is duck-typed for exactly that reason, so the
    honest way to test it is an object of the same SHAPE, with
    `__str__` reproducing the library's own render order.
    """

    def __init__(self, status, reason, body, headers):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.body = body
        self.headers = headers

    def __str__(self):
        out = f"({self.status})\nReason: {self.reason}\n"
        if self.headers:
            out += f"HTTP response headers: {self.headers}\n"
        if self.body:
            out += f"HTTP response body: {self.body}\n"
        return out


def _refusal():
    return FakeApiException(403, "Forbidden", BODY, HEADERS)


def test_the_old_expression_loses_the_message_and_the_new_one_keeps_it():
    """THE PAIR. Either half alone proves nothing.

    The first assertion is the bug, reproduced. If a future kubernetes
    release puts the body first, it fails -- and that is correct: the
    fix would no longer be load-bearing and somebody should find out
    from a test rather than by deleting it and hoping.
    """
    exc = _refusal()
    old = f"dispatch failed: {type(exc).__name__}: {exc}"[:500]
    assert "exceeded quota" not in old, (
        "the header dump no longer crowds out the message, so this "
        "file's premise has changed -- re-measure before trusting it")

    new = (f"dispatch failed: {type(exc).__name__}: "
           f"{k8s_error.sentence(exc)}")[:500]
    assert "exceeded quota" in new
    assert "requests.cpu=128" in new, "the LIMIT is what tells them what to do"
    assert "requests.cpu=8250m" in new, "and what was asked for"


def test_the_stored_reason_fits_the_column():
    """`stall_reason` is written with `[:500]`. A sentence that needs
    truncating is a sentence that loses its own numbers again."""
    exc = _refusal()
    stored = (f"dispatch failed: {type(exc).__name__}: "
              f"{k8s_error.sentence(exc)}")
    assert len(stored) <= 500
    assert stored.endswith("requests.cpu=128")


def test_a_refusal_with_no_body_still_says_something():
    """A webhook rejection can arrive with no `Status` body at all.
    Falling through to `str(exc)` there would hand back the header dump
    this module exists to remove."""
    exc = FakeApiException(403, "Forbidden", None, HEADERS)
    said = k8s_error.sentence(exc)
    assert said == "403 Forbidden"
    assert "Audit-Id" not in said


def test_a_body_that_is_not_json_is_returned_rather_than_dropped():
    """Proxies return HTML. It is poor, and it beats nothing."""
    exc = FakeApiException(502, "Bad Gateway", "<html>upstream</html>", {})
    assert "upstream" in k8s_error.sentence(exc)


def test_an_ordinary_exception_is_unchanged():
    """Most of what `dispatch_pass` catches is not an API error, and
    those messages were never broken."""
    assert k8s_error.sentence(ValueError("no workspace host")) == \
        "no workspace host"


def test_it_never_raises_inside_an_error_handler():
    """It is called from an `except` clause on a best-effort write, and
    `runner.py` is explicit that a dispatch which failed must not also
    fail to say so. A formatter that throws replaces a bad message with
    no message and an unrecorded stall."""

    class Hostile(Exception):
        @property
        def body(self):
            raise RuntimeError("body explodes")

        @property
        def reason(self):
            raise RuntimeError("reason explodes")

    assert k8s_error.sentence(Hostile()) != ""


def test_dispatch_pass_routes_its_failure_through_the_formatter():
    """The guard that keeps the two ends joined.

    `k8s_error` can be perfect and `runner.py` can still interpolate the
    raw exception -- which is what it did. Asserted on the source
    because the alternative is a real dispatch failure against a real
    cluster.
    """
    import pathlib

    src = pathlib.Path("kernel/worker/runner.py").read_text(encoding="utf-8")
    assert "k8s_error.sentence(e)" in src, (
        "dispatch_pass no longer formats its failure through "
        "k8s_error, so a 403's message is being cut off again")
    assert 'f"dispatch failed: {type(e).__name__}: {e}"' not in src, (
        "the raw-exception interpolation is back")
