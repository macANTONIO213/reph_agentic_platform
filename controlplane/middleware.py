from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse


class ApiVersionHeadersMiddleware:
    """
    Freezes the /api/v1 contract by emitting explicit version/deprecation headers.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/"):
            response["X-API-Version"] = "v1"
            response["X-API-Policy"] = "Versioned; additive-only changes in v1"
            response["X-API-Deprecation-Policy"] = "90-day notice minimum"
        return response


class ApiKeyAuthMiddleware:
    """
    Session-less /api/v1 authentication via ``X-API-Key`` (IN-1).

    The presented key is SHA-256 hashed and matched against active ``ApiKey``
    rows; on match the request is authenticated as the key's owner and CSRF is
    waived (header-carried credentials are not CSRF-forgeable). Runs after
    AuthenticationMiddleware so session logins always take precedence.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.headers.get("X-API-Key", "")
        if raw and request.path.startswith("/api/") and not request.user.is_authenticated:
            import hashlib

            from django.utils import timezone

            from controlplane.models import ApiKey

            digest = hashlib.sha256(raw.encode()).hexdigest()
            key = (
                ApiKey.objects.select_related("user")
                .filter(key_hash=digest, is_active=True, user__is_active=True)
                .first()
            )
            if key:
                request.user = key.user
                request._dont_enforce_csrf_checks = True
                ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return self.get_response(request)


class ApiGlobalRateLimitMiddleware:
    """
    Request-level throttle for authenticated API traffic.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.window_seconds = int(getattr(settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60))
        self.limit = int(getattr(settings, "API_RATE_LIMIT_REQUESTS_PER_WINDOW", 120))
        # Cover the frozen API, the run endpoints, the dashboard API, AND the
        # external interop surface (/a2a/, token-auth, mutating) — the last was
        # previously unthrottled, enabling token brute-force and abuse.
        self.protected_prefixes = (
            "/api/v1/", "/api/agents/", "/api/runs/", "/api/telemetry/",
            "/api/monitoring/", "/api/org/", "/a2a/",
        )

    def __call__(self, request):
        if request.path.startswith(self.protected_prefixes):
            scope = self._scope(request)
            if scope and self._is_limited(scope):
                return JsonResponse(
                    {"error": "Global API rate limit exceeded. Try again shortly."},
                    status=429,
                )
        return self.get_response(request)

    def _scope(self, request) -> str:
        if request.user.is_authenticated:
            return f"user:{request.user.id}"
        ip = request.META.get("REMOTE_ADDR", "unknown")
        return f"ip:{ip}"

    def _is_limited(self, scope: str) -> bool:
        key = f"rl:global:{scope}"
        # Atomic increment avoids the get-then-set TOCTOU race where concurrent
        # requests undercount and slip past the limit. add() seeds the window
        # (and its TTL) exactly once; incr() is atomic on real cache backends.
        try:
            if cache.add(key, 1, timeout=self.window_seconds):
                return False
            count = cache.incr(key)
        except ValueError:
            # Key expired between add() and incr(): reseed the window.
            cache.add(key, 1, timeout=self.window_seconds)
            return False
        return count > self.limit
