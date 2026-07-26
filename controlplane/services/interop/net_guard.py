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
import urllib.request

from django.conf import settings


class BlockedDestinationError(ValueError):
    """Raised when a URL fails the SSRF policy (default error type)."""


_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::]"}


def _resolve_default() -> bool:
    """
    Whether to DNS-resolve and re-check by default.

    Off by default so hostname-only checks stay latency-free and the documented
    private-range design (and its regression tests) hold.  Set
    ``NET_GUARD_RESOLVE_DNS=True`` in production to defeat hostname → internal-IP
    SSRF on every outbound path at once.
    """
    return bool(getattr(settings, "NET_GUARD_RESOLVE_DNS", False))


def _block_private() -> bool:
    """When True, RFC1918/private ranges are rejected too (opt-in for prod)."""
    return bool(getattr(settings, "NET_GUARD_BLOCK_PRIVATE", False))


def _is_blocked_ip(ip) -> bool:
    blocked = (
        ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_unspecified or ip.is_reserved
    )
    if _block_private():
        blocked = blocked or ip.is_private
    return blocked


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
    resolve: bool | None = None,
) -> None:
    """
    Raise ``error_cls`` if ``url`` is not a permitted outbound destination.

    ``resolve`` defaults to the ``NET_GUARD_RESOLVE_DNS`` setting when not
    explicitly given, so production can enable DNS-rechecking platform-wide
    without touching every call site.
    """
    if resolve is None:
        resolve = _resolve_default()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise error_cls("URL must use http or https.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise error_cls("URL must include a host.")
    if host in _BLOCKED_HOSTNAMES:
        raise error_cls("URL host is not allowed.")

    # Always check a literal-IP host, regardless of resolve.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _is_blocked_ip(ip):
        raise error_cls(f"URL resolves to blocked address ({ip}).")

    if resolve and ip is None:
        _validate_resolved(host, parsed, error_cls)


class _RevalidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF policy on every redirect hop before following it.

    Plain ``urlopen`` follows 3xx transparently, so a validated destination can
    bounce the client to an internal/metadata address. This handler closes that
    hole by validating each ``Location`` against the same policy.
    """

    def __init__(self, error_cls: type[Exception], resolve: bool | None):
        super().__init__()
        self._error_cls = error_cls
        self._resolve = resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_destination(newurl, error_cls=self._error_cls, resolve=self._resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def install_safe_opener() -> None:
    """Install a process-wide opener that re-validates every redirect hop.

    Because ``urllib.request.urlopen`` honours the installed opener, this closes
    the SSRF-via-redirect hole on *every* outbound urllib path at once — the REST
    connector, MCP/A2A clients, scanners, and the HTTP-API adapter — without
    touching each call site. Called once from ``ControlplaneConfig.ready``.
    """
    opener = urllib.request.build_opener(_RevalidatingRedirectHandler(BlockedDestinationError, None))
    urllib.request.install_opener(opener)
