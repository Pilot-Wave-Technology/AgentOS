"""SSRF guard for outbound HTTP calls made by custom tools.

Applied before every HTTP request from http_action and openapi tier execution.
Blocks private IP ranges, the GCP metadata endpoint, and other link-local addresses.
Hostname resolution returns every A/AAAA record — if ANY resolved IP is in a
blocked range we refuse the request (defends against multi-record DNS attacks
where one record is public-safe and another points at internal infra).
"""
import ipaddress
import logging
import socket
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),    # link-local + GCP metadata (169.254.169.254)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private (ULA)
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def assert_safe_url(url: str) -> None:
    """Raise ValueError if the URL resolves to any blocked IP range.

    Uses getaddrinfo to enumerate every IP the hostname resolves to (v4 + v6)
    and rejects if any one of them is in a blocked range. This catches the
    common attack where a malicious host returns multiple A records — one
    legitimate, one pointing at internal infrastructure — relying on the
    HTTP client to pick a different IP than our validator.

    Note: a small race window remains between this resolution and the
    HTTP client's own resolution. The DNS rebinding window is bounded by
    the record TTL; for defense-in-depth, callers should also constrain
    egress at the network layer.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme '{parsed.scheme}': only http/https allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve hostname '{hostname}': {exc}") from exc

    resolved_ips: list[str] = []
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        resolved_ips.append(ip_str)
        addr = ipaddress.ip_address(ip_str)
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                log.warning(
                    "SSRF blocked: %s resolves to %s (in %s); all resolved=%s",
                    url, ip_str, network, resolved_ips,
                )
                raise ValueError(
                    f"URL '{url}' resolves to a blocked address ({ip_str})"
                )

    if not resolved_ips:
        raise ValueError(f"could not resolve hostname '{hostname}'")

    log.debug("SSRF check passed for %s → %s", hostname, resolved_ips)
