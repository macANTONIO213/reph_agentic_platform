"""
Shared SSRF destination guard — Phase 1 interop (+ adapters).

One consistent outbound-destination policy for the REST connector, MCP client,
A2A client, scanner clients, and the HTTP-API adapter.  Behaviour:

  - only http/https schemes are allowed;
  - literal loopback / link-local / multicast / unspecified / reserved IPs and the
    ``localhost`` family are blocked;
  - private ranges (10/8, 192.168/16, …) and ordinary hostnames are ALLOWED — by
    design, because legitimate enterprise endpoints live on internal networks
    (see the SSRF regression test in ``test_inspection_fixes``).

``resolve=True`` additionally resolves the host via DNS and checks every resolved
address against the same blocklist — defeating hostname → internal-IP SSRF.  It is
opt-in (the HTTP-API adapter uses it) so hostname-only checks stay dependency- and
latency-free for the connector/MCP paths.

Callers pass their own ``error_cls`` so failures surface as their domain error.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse


class BlockedDestinationError(ValueError):
    """Raised when a URL fails the SSRF policy (default error type)."""


_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_blocked_ip(ip) -> bool:
    return (
        ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_unspecified or ip.is_reserved
    )


def _validate_resolved(host: str, parsed, error_cls: type[Exception]) -> None:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise error_cls(f"Could not resolve host '{host}': {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise error_cls(f"Host '{host}' resolves to a blocked address ({ip}).")


def validate_destination(
    url: str,
    *,
    error_cls: type[Exception] = BlockedDestinationError,
    resolve: bool = False,
) -> None:
    """Raise ``error_cls`` if ``url`` is not a permitted outbound destination."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise error_cls("URL must use http or https.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise error_cls("URL must include a host.")
    if host in _BLOCKED_HOSTNAMES:
        raise error_cls("URL host is not allowed.")

    if resolve:
        _validate_resolved(host, parsed, error_cls)
        return

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # Hostname (non-literal IP) — allowed; DNS is not resolved here.
    if _is_blocked_ip(ip):
        raise error_cls(f"URL resolves to blocked address ({ip}).")
