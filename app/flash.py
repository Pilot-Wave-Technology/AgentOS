"""Shared helper for the `?_flash=`/`?_flash_type=` toast-redirect convention.

A 303 redirect can't carry a response body, so a one-off message for the page
the redirect lands on rides in the query string instead. The reading side is
app/templates/base.html's shared IIFE, which pops `_flash`/`_flash_type` off
the URL on page load and calls showToast() — the `FlashType` values below
must keep matching that function's `styles` map keys (base.html), or a type
this module accepts will render as an unstyled 'info' toast client-side.

Before this helper existed, every router hand-rolled its own quote()/
quote_plus() call plus an f-string, which let the two encoders drift across
call sites for no functional reason (both percent-encode-space and
plus-encode-space decode identically server- and client-side) and gave each
caller its own chance to pass a `flash_type` showToast doesn't recognize.
"""
import logging
from typing import Literal, get_args
from urllib.parse import quote, urlsplit

from fastapi.responses import RedirectResponse

_log = logging.getLogger(__name__)

FlashType = Literal["success", "warning", "error", "info"]
VALID_FLASH_TYPES: set[str] = set(get_args(FlashType))


def flash_redirect(url: str, message: str, *, flash_type: FlashType = "success", status_code: int = 303) -> RedirectResponse:
    """Redirect to `url`, carrying `message` for base.html's shared toast reader.

    Raises ValueError immediately if `flash_type` isn't one showToast styles —
    fail at redirect-construction time, not silently as a mis-colored toast.
    The Literal type hint catches most typos at review/IDE time; this check
    is the runtime backstop for values that aren't literal string constants.
    """
    if flash_type not in VALID_FLASH_TYPES:
        _log.error("flash_redirect: unknown flash_type=%r for url=%s", flash_type, url)
        raise ValueError(f"Unknown flash_type {flash_type!r}; must be one of {sorted(VALID_FLASH_TYPES)}")

    # urlsplit (not a naive "?" in url substring check) so a literal '?' inside
    # a path segment — e.g. a percent-decoded path param — can't be mistaken
    # for the start of an existing query string.
    sep = "&" if urlsplit(url).query else "?"
    target = f"{url}{sep}_flash={quote(message)}"
    if flash_type != "success":
        target += f"&_flash_type={flash_type}"
    _log.debug("flash_redirect: type=%s url=%s", flash_type, target)
    return RedirectResponse(url=target, status_code=status_code)
