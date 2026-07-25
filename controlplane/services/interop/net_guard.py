"""
Shared SSRF destination guard — Phase 1 interop.

Extracted from ``RestConnector._validate_destination`` so governed outbound calls
(REST connector, MCP client) enforce one consistent policy.  Behaviour is
identical to the original REST guard:

  - only http/https schemes are allowed;
  - literal loopback / link-local / multicast / unspecified / reserved IPs and the
    ``localhost`` family are blocked;
  - private ranges (10/8, 192.168/16, …) and ordinary hostnames are ALLOWED — by
    design, because legitimate enterprise endpoints live on internal networks
    (see the SSRF regression test in ``test_inspection_fixes``).

Callers pass their own ``error_cls`` so failures surface as their domain error.
"""
from __future__ import annotations

import ipaddress
import urllib.parse


class BlockedDestinationError(ValueError):
    """Raised when a URL fails the SSRF policy (default error type)."""


_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def validate_destination(url: str, *, error_cls: type[Exception] = BlockedDestinationError) -> None:
    """Raise ``error_cls`` if ``url`` is not a permitted outbound destination."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise error_cls("URL must use http or https.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise error_cls("URL must include a host.")
    if host in _BLOCKED_HOSTNAMES:
        raise error_cls("URL host is not allowed.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # Hostname (non-literal IP) — allowed; DNS is not resolved here.
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise error_cls(f"URL resolves to blocked address ({ip}).")
