"""The part of a Kubernetes API error a person can act on.

WHY THIS EXISTS, measured 2026-09-18 while building Q1.

`kubernetes.client.exceptions.ApiException.__str__` renders in this
order:

    (403)
    Reason: Forbidden
    HTTP response headers: {...}
    HTTP response body: {"kind":"Status",...,"message":"...",...}

The headers come FIRST, and on GKE they are not small -- `Audit-Id`,
two `X-Kubernetes-Pf-*` uids, `Date`, `Cache-Control`, `Content-Type`,
`Content-Length`. `runner.dispatch_pass` stored
`f"dispatch failed: {type(e).__name__}: {e}"[:500]`, and on a
reconstructed GKE quota refusal the full string is **698 characters
with the message beginning at offset 521**. The 500-character cut lands
inside `{"kind":"Status","apiVersion":"v1","metadata":{},"status":`.

So `stall_reason` held a header dump and the word `Forbidden`, and
NONE of `exceeded quota`, `requested:`, `used:` or `limited:` survived.
The operator was told the request was refused and not one thing about
what to change.

THIS MATTERS MORE THAN A TIDIER MESSAGE, and plan 11.5i explains why.
D5 measured that sixteen `xlarge` executions cost 132 vCPU and 520 GiB
against a namespace quota of 128 and 512Gi -- over on both axes.
ResourceQuota is an ADMISSION plugin, so the pod that crosses the line
is more likely **refused at `create` with a 403** than left Pending. The
403 path is therefore the LIKELIER half of that trap, and it was the
half that said nothing useful. `kernel/conductor/unscheduled.py` handles
the Pending half and records the same finding from its side.

WHY DUCK-TYPED RATHER THAN `except ApiException`. `kernel/` must stay
importable without the kubernetes package -- the driver imports it
lazily and the test suite runs with no cluster and no client library.
An `isinstance` check here would make this module unimportable in
exactly the environment that tests it, so it reads `.body` off whatever
it is handed and falls back when the shape is not there.

NEVER RAISES. It is called from an `except` clause on a best-effort
write, and `runner.py` is explicit that "a dispatch that failed must not
also fail to say so". A formatter that throws inside an error handler
replaces a bad message with no message.
"""

from __future__ import annotations

import json

# Long enough for the API server's own sentence, short enough to leave
# room for the caller's prefix under the 500-character column write.
# GKE's quota refusal -- the longest real one measured -- is 219
# characters of `message`.
MAX_SENTENCE = 400


def _body_text(exc) -> str:
    """The response body as text, whatever the client handed us."""
    body = getattr(exc, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    if isinstance(body, str):
        return body
    return ""


def api_message(exc) -> str:
    """The `message` field of a Kubernetes `Status` body, or "".

    The API server puts the actionable sentence here and nowhere else:
    the quota name, the resource, what was requested, what is used and
    what the limit is. Everything around it in `str(exc)` is transport.
    """
    text = _body_text(exc).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Not JSON -- some proxies return HTML or a bare string. The raw
        # body is still better than the header dump, so hand it back
        # rather than discarding it.
        return text[:MAX_SENTENCE]
    if not isinstance(parsed, dict):
        return text[:MAX_SENTENCE]
    message = parsed.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:MAX_SENTENCE]
    return ""


def sentence(exc) -> str:
    """What to write where a person looks. Never raises, never empty.

    Preference order, and each step is a real case:

      1. the `Status.message` -- the only place the numbers live
      2. `reason` plus `status` -- a 403 with no body, which is what a
         webhook rejection can look like
      3. `str(exc)` -- anything that is not an API error at all, which
         is most of what `dispatch_pass` can catch
    """
    try:
        message = api_message(exc)
        if message:
            return message
        reason = getattr(exc, "reason", None)
        status = getattr(exc, "status", None)
        if reason and status:
            return f"{status} {reason}"
        if reason:
            return str(reason)
    except Exception:                                       # noqa: BLE001
        # See the module docstring: this runs inside an error handler.
        pass
    # AND NEVER EMPTY, which is not the same as never raising -- the
    # first draft satisfied one and not the other, and its own test
    # caught it. `str()` on an exception raised with no arguments is
    # `""`, so the row would have read "dispatch failed: Hostile: " and
    # named a type with no sentence. The class name alone is thin, and
    # it is the difference between "something went wrong here" and a
    # `stall_reason` a person reads as truncated or corrupt.
    return str(exc)[:MAX_SENTENCE] or f"{type(exc).__name__} with no message"
