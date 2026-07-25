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
        self.protected_prefixes = ("/api/v1/", "/api/agents/")

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
        count = cache.get(key, 0)
        if count >= self.limit:
            return True
        cache.set(key, count + 1, timeout=self.window_seconds)
        return False
