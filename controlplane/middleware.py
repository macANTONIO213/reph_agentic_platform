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
