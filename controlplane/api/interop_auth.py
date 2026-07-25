"""
Shared authentication helpers for the external interop surfaces (/a2a/).

Two hardening properties both surfaces (A2A JSON-RPC and the MCP server) need:

- Bearer tokens are compared in constant time (``hmac.compare_digest``) so a
  remote caller cannot brute-force a token byte-by-byte via timing.
- The JSON-RPC POST views are ``csrf_exempt`` for bearer-token callers (who
  cannot be CSRF'd — browsers refuse to attach cross-site Authorization
  headers without a CORS preflight we never grant), but a *session*-
  authenticated caller is a logged-in browser and MUST still pass Django's
  CSRF check, otherwise a malicious page can force task submission or tool
  calls with the victim's cookie.
"""
from __future__ import annotations

import hmac

from django.middleware.csrf import CsrfViewMiddleware


def bearer_token_matches(request, tokens) -> bool:
    """Constant-time check of the Authorization: Bearer header against tokens."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    presented = auth[len("Bearer "):].strip()
    if not presented:
        return False
    # Compare against every candidate (no early exit) to keep timing flat.
    matched = False
    for candidate in tokens or []:
        if hmac.compare_digest(presented, candidate):
            matched = True
    return matched


def session_csrf_failure(request):
    """
    Enforce CSRF for session-authenticated callers of csrf_exempt POST views.

    Returns a rejection HttpResponse when the caller is a session user whose
    CSRF token is missing/invalid; returns None when the request may proceed
    (bearer-token/external callers, GETs, or a valid CSRF token).
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if not request.user.is_authenticated:
        return None
    # A valid bearer token means a programmatic caller; CSRF does not apply.
    return CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
