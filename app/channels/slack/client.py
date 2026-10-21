"""How a Slack client is built, and what its failures mean.

TWENTY-THREE PLACES CONSTRUCTED `WebClient(token=...)` BARE. slack_sdk
ships no retry handler by default, so a 429 was an exception, and the
exception met one of eight identical `except SlackApiError -> warning ->
return` blocks. A rate-limited post was a dropped message. A
rate-limited channel create was a channel that never existed. Neither
raised an alarm; both logged at WARNING and carried on.

`conversations.create` is Tier-2 -- roughly 20 a minute -- which was
survivable at three channels per goal and is not survivable at one
channel per thread. So every client comes from here now, and here is
where the retry handlers are attached.

The second half of the file is `classify()`. All those except blocks
treated `channel_not_found`, `not_in_channel`, `invalid_auth`,
`token_revoked` and `ratelimited` identically, which is the same as
treating none of them: a revoked token produced an endless stream of
identical warnings with no status change and no admin surface, and a
bot kicked from a channel left a live binding that failed forever
with no attempt to rejoin. The caller needs to know WHICH kind of
failure it had, and there should be exactly one place that decides.
"""

from __future__ import annotations

import logging
from enum import Enum

_log = logging.getLogger(__name__)


# 3 attempts: enough for a Tier-2 burst to drain, few enough that a
# genuinely rate-limited caller finds out rather than hanging.
_RATE_LIMIT_RETRIES = 3
_CONNECTION_RETRIES = 2


def _retry_handlers():
    """Sync handlers, for a sync client."""
    from slack_sdk.http_retry.builtin_handlers import (
        ConnectionErrorRetryHandler, RateLimitErrorRetryHandler)
    return [RateLimitErrorRetryHandler(max_retry_count=_RATE_LIMIT_RETRIES),
            ConnectionErrorRetryHandler(max_retry_count=_CONNECTION_RETRIES)]


def _async_retry_handlers():
    """ASYNC handlers, which are a different class hierarchy entirely.

    An AsyncWebClient calls `can_retry_async` on each handler, and the
    sync ones do not have it -- so handing them over produced
    `'RateLimitErrorRetryHandler' object has no attribute
    'can_retry_async'` on the FIRST Slack call, which is to say channel
    creation stopped working altogether.

    The test that was supposed to catch this asked whether a handler's
    class name contained "RateLimit". It does, for both. A test that
    cannot tell the two apart is the reason this reached a deployment.
    """
    from slack_sdk.http_retry.builtin_async_handlers import (
        AsyncConnectionErrorRetryHandler, AsyncRateLimitErrorRetryHandler)
    return [AsyncRateLimitErrorRetryHandler(max_retry_count=_RATE_LIMIT_RETRIES),
            AsyncConnectionErrorRetryHandler(max_retry_count=_CONNECTION_RETRIES)]


def make_client(token: str):
    """A sync WebClient that honours Retry-After.

    Prefer `make_async_client` in anything running on the event loop --
    a sync call blocks it, and Slack's p99 is not fast.
    """
    from slack_sdk import WebClient
    return WebClient(token=token, retry_handlers=_retry_handlers())


def make_async_client(token: str):
    from slack_sdk.web.async_client import AsyncWebClient
    return AsyncWebClient(token=token, retry_handlers=_async_retry_handlers())


# ── what a failure means ─────────────────────────────────────────────────────

class Failure(str, Enum):
    """The only five answers a caller needs."""

    DEAD_CHANNEL = "dead_channel"    # gone, archived, or never existed
    NOT_A_MEMBER = "not_a_member"    # we are not in it; rejoining may work
    DEAD_TOKEN = "dead_token"        # auth is finished; the integration is down
    RATE_LIMITED = "rate_limited"    # retried already and still limited
    OTHER = "other"


_DEAD_CHANNEL = {"channel_not_found", "is_archived", "channel_is_archived"}
_NOT_A_MEMBER = {"not_in_channel"}
_DEAD_TOKEN = {"invalid_auth", "token_revoked", "tokens_revoked",
               "account_inactive", "app_uninstalled", "not_authed"}
_RATE_LIMITED = {"ratelimited", "rate_limited"}


def error_code(exc) -> str:
    """The Slack error string, or "" if this is not a SlackApiError."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        return str(resp.get("error", "") or "")
    except Exception:                                          # noqa: BLE001
        return ""


def classify(exc) -> Failure:
    code = error_code(exc)
    if code in _DEAD_CHANNEL:
        return Failure.DEAD_CHANNEL
    if code in _NOT_A_MEMBER:
        return Failure.NOT_A_MEMBER
    if code in _DEAD_TOKEN:
        return Failure.DEAD_TOKEN
    if code in _RATE_LIMITED:
        return Failure.RATE_LIMITED
    return Failure.OTHER


async def on_failure(exc, *, pool=None, integration_id: str = "",
                     binding_id: str = "", where: str = "") -> Failure:
    """Classify, record, and tell the caller what happened.

    The recording is the part that was missing. A dead channel marks its
    binding archived so the next thousand messages do not retry it; a
    dead token marks the integration inactive so somebody can see that
    Slack has stopped working, instead of inferring it from silence.
    """
    kind = classify(exc)
    code = error_code(exc) or str(exc)

    if kind is Failure.DEAD_CHANNEL and pool is not None and binding_id:
        try:
            await pool.execute(
                "UPDATE platform_channel_bindings SET archived_at = now(), "
                "updated_at = now() WHERE id = $1 AND archived_at IS NULL",
                binding_id)
            _log.warning("Slack %s: channel is gone (%s) — binding %s archived",
                         where, code, binding_id)
            return kind
        except Exception as inner:                             # noqa: BLE001
            _log.warning("Slack %s: could not archive binding %s: %s",
                         where, binding_id, inner)

    if kind is Failure.DEAD_TOKEN and pool is not None and integration_id:
        try:
            await pool.execute(
                # 'revoked' is already in the status CHECK and means exactly
                # this. Inventing 'auth_failed' would have needed a migration
                # to say something the vocabulary already said.
                "UPDATE platform_integrations SET status = 'revoked', "
                "updated_at = now() WHERE id = $1 AND status = 'active'",
                integration_id)
            _log.error(
                "Slack %s: authentication is dead (%s) — integration %s marked "
                "revoked. Somebody must reinstall the app.",
                where, code, integration_id)
            return kind
        except Exception as inner:                             # noqa: BLE001
            _log.warning("Slack %s: could not deactivate integration %s: %s",
                         where, integration_id, inner)

    _log.warning("Slack %s failed (%s): %s", where, kind.value, code)
    return kind
