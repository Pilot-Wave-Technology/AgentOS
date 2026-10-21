"""What kind of failure was that, and is repeating the call worth doing?

ONE TAXONOMY, BOTH PROVIDERS. The SDKs raise different exception types
with different attributes for the same condition, and every place that
decided "is this retryable?" inline got a slightly different answer. G4's
tool taxonomy settled the same argument for tools: `retryable` is a
property of the CODE, not of the call site, because an author deciding it
per raise gets it wrong and two callers then disagree about the same
condition.

WHAT MUST NEVER BE RETRIED, and why each is here rather than in the
retryable set:

  auth          a 401 will be a 401 in eight seconds. Retrying spends
                the budget to arrive at the same place, and hides a
                misconfiguration behind what looks like an outage.
  bad_request   the request is wrong. Repeating it unchanged is the
                definition of not fixing it.
  overflow      a predicate, not a fault — LR2 is explicit that context
                overflow ends the leg and is never retried, because the
                next identical call overflows identically.

Everything else — rate limits, 5xx, timeouts, dropped connections — is
the provider having a moment, and the request is fine.

Classification reads the status code where the SDK offers one, and falls
back to matching the message. The fallback is deliberately narrow: a
message match that is too eager turns a permanent fault into an infinite
retry, which is worse than not retrying at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Kinds that carry a distinct meaning to the loop. `unknown` is retryable
# ONCE by the policy rather than never: a transport error nobody has
# classified yet is more often transient than permanent, and the attempt
# ceiling bounds the cost of being wrong.
RATE_LIMIT = "rate_limit"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
CONNECTION = "connection"
OVERFLOW = "context_overflow"
AUTH = "auth"
BAD_REQUEST = "bad_request"
UNKNOWN = "unknown"

_RETRYABLE = {RATE_LIMIT, SERVER_ERROR, TIMEOUT, CONNECTION, UNKNOWN}

# Substrings that identify a condition when no status code is available.
# Matched against a lowercased message.
_MESSAGE_HINTS = (
    # Overflow first: some providers report it as a 400, and a 400 that
    # is really an overflow must reach LR2's predicate rather than the
    # generic "your request is wrong" path.
    (OVERFLOW, ("context length", "context window", "prompt is too long",
                "maximum context", "too many tokens", "exceeds the maximum"
                )),
    (RATE_LIMIT, ("rate limit", "429", "quota", "resource_exhausted",
                  "too many requests", "overloaded")),
    (TIMEOUT, ("timeout", "timed out", "deadline exceeded")),
    (CONNECTION, ("connection", "econnreset", "broken pipe",
                  "server disconnected", "remote end closed")),
    (AUTH, ("api key", "unauthorized", "unauthenticated",
            "permission denied", "forbidden", "invalid_api_key")),
    (SERVER_ERROR, ("internal error", "internal server", "bad gateway",
                    "service unavailable", "unavailable", "502", "503",
                    "504")),
)


@dataclass(frozen=True)
class Fault:
    kind: str
    retryable: bool
    detail: str
    # The provider's own answer to "when should I come back". Honoured
    # over our arithmetic, because a 429 that says 20 seconds means 20.
    retry_after_s: Optional[float] = None
    status: Optional[int] = None


def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after_of(exc: BaseException) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
        try:
            raw = headers.get(key)
        except AttributeError:
            return None
        if raw is None:
            continue
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            # A date-formatted Retry-After is valid HTTP and we do not
            # parse it: falling back to the backoff curve is correct and
            # bounded, where a misparse could wait until the heat death.
            return None
    return None


def _kind_from_status(status: int) -> Optional[str]:
    if status == 429:
        return RATE_LIMIT
    if status in (401, 403):
        return AUTH
    if status == 408:
        return TIMEOUT
    if status >= 500:
        return SERVER_ERROR
    if status == 400:
        return BAD_REQUEST          # refined by message below
    return None


def classify(exc: BaseException) -> Fault:
    """One typed fault from any provider exception."""
    message = f"{type(exc).__name__}: {exc}"
    lowered = str(exc).lower()
    status = _status_of(exc)

    kind = _kind_from_status(status) if status is not None else None

    # The message can OVERRIDE a 400, and only a 400. A provider that
    # reports context overflow as a bad request must still reach LR2 —
    # and nothing else is allowed to talk its way out of a status code.
    if kind in (None, BAD_REQUEST):
        for candidate, needles in _MESSAGE_HINTS:
            if any(n in lowered for n in needles):
                if kind == BAD_REQUEST and candidate != OVERFLOW:
                    continue
                kind = candidate
                break

    if kind is None:
        kind = UNKNOWN
    return Fault(kind=kind, retryable=kind in _RETRYABLE, detail=message,
                 retry_after_s=_retry_after_of(exc), status=status)
