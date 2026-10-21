"""web_fetch — retrieve a URL, with SSRF as the first-class concern.

Worker-side, not sandbox-side. Not because the sandbox lacks the network —
it shares the worker's namespace and has exactly the same reach — but
because this is where the guard belongs and where it can be tested without
a container.

WHY THE GUARD IS THE FEATURE. An unguarded fetch tool is a credential-theft
primitive. The worker holds ANTHROPIC_API_KEY and GEMINI_API_KEY; on a
cloud VM 169.254.169.254 hands instance credentials to anything that asks;
localhost carries the bridge shim on 9100 and Postgres on 5432. A model
that can name a URL can reach all three unless something stops it.

So the tool is a deny-by-default network client:

  * http and https only. file:// reads the worker's disk, gopher:// and
    friends are protocol-smuggling vectors.
  * Every hostname is resolved and every resulting address checked. Private,
    loopback, link-local, reserved and multicast ranges are refused —
    169.254.169.254 is link-local, which is the case that matters most.
  * Redirects are followed MANUALLY and re-checked at each hop. A public URL
    that 302s to the metadata endpoint is the standard bypass, and
    urlopen's default redirect handling would follow it.
  * No credential ever leaves: the request carries no header we did not set.

KNOWN LIMIT, stated rather than implied: this resolves the hostname, checks
the addresses, then lets urllib resolve it again to connect. A DNS entry
that changes between the two answers (rebinding) defeats the check. Closing
it means connecting to a pinned IP with a Host header, which urllib does not
express cleanly. Recorded as a gap for Stage H's egress work, where the
network-level restriction belongs.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_BYTES = 128 * 1024
TIMEOUT_S = 20
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http", "https")
USER_AGENT = "AgentOS-executor/1.0"


class BlockedURLError(Exception):
    """The URL is refused before any connection is attempted."""


def _addresses_for(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise BlockedURLError(f"cannot resolve {host!r}: {e}") from None
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise BlockedURLError(f"no usable address for {host!r}")
    return out


def check_url(url: str) -> str:
    """Raise BlockedURLError unless this URL is safe to fetch. Returns it."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(
            f"scheme {parsed.scheme or '(none)'!r} is not allowed; "
            f"use {' or '.join(ALLOWED_SCHEMES)}"
        )
    if not parsed.hostname:
        raise BlockedURLError("URL has no host")

    for addr in _addresses_for(parsed.hostname):
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast
                or addr.is_unspecified):
            raise BlockedURLError(
                f"{parsed.hostname} resolves to {addr}, which is a private, "
                "loopback, link-local or reserved address. Refused: these "
                "reach cloud metadata, the bridge and the database."
            )
    return url


_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _to_text(body: bytes, content_type: str) -> str:
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        text = _TAG.sub(" ", text)
        text = _ANY_TAG.sub(" ", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = _WS.sub("\n\n", text)
    return text.strip()


def fetch(url: str) -> tuple[str, str | None]:
    """Fetch a URL. Returns (text, error). Never raises."""
    try:
        current = check_url(url)
    except BlockedURLError as e:
        return "", str(e)

    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(
            current, headers={"User-Agent": USER_AGENT}, method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=TIMEOUT_S) as resp:
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    target = resp.headers.get("Location")
                    if not target:
                        return "", f"redirect {status} with no Location"
                    current = urllib.parse.urljoin(current, target)
                    try:
                        # Re-check EVERY hop: a public URL redirecting to the
                        # metadata endpoint is the standard bypass.
                        check_url(current)
                    except BlockedURLError as e:
                        return "", f"redirect blocked: {e}"
                    continue
                body = resp.read(MAX_BYTES + 1)
                truncated = len(body) > MAX_BYTES
                text = _to_text(body[:MAX_BYTES],
                                resp.headers.get("Content-Type", ""))
                if truncated:
                    text += "\n\n[... truncated at 128 KB]"
                return text, None
        except urllib.error.HTTPError as e:
            return "", f"HTTP {e.code} for {current}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            return "", f"cannot fetch {current}: {e}"

    return "", f"too many redirects (>{MAX_REDIRECTS})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand redirects back to us instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
