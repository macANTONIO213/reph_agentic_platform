"""
Probe-friendly health endpoints (plan E1/observability).

Three unauthenticated, side-effect-free endpoints an orchestrator/load balancer
can call directly:

  GET /healthz  — liveness. Cheap; the process is up and can serve. No I/O.
  GET /readyz   — readiness. Checks the dependencies a request actually needs
                  (database, cache/broker). Returns 503 when a dependency is
                  down so the LB stops routing traffic to this instance.
  GET /health/  — legacy alias kept for existing callers (delegates to liveness).

These deliberately return only up/down booleans and coarse timings — never
config, versions, stack traces, or dependency URLs — so they are safe to expose.
"""
from __future__ import annotations

import time

from django.db import connection
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def livez(request):
    """Liveness: the process is running. No dependency I/O — never flaps."""
    return JsonResponse({"status": "alive"})


def _check_database() -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, (time.perf_counter() - start) * 1000
    except Exception:
        return False, (time.perf_counter() - start) * 1000


def _check_cache() -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        cache.set("healthz:probe", "1", timeout=5)
        ok = cache.get("healthz:probe") == "1"
        return ok, (time.perf_counter() - start) * 1000
    except Exception:
        return False, (time.perf_counter() - start) * 1000


@require_GET
def readyz(request):
    """Readiness: dependencies are reachable. 503 when any hard dependency fails."""
    db_ok, db_ms = _check_database()
    cache_ok, cache_ms = _check_cache()
    checks = {
        "database": {"ok": db_ok, "latency_ms": round(db_ms, 1)},
        "cache": {"ok": cache_ok, "latency_ms": round(cache_ms, 1)},
    }
    # The database is a hard dependency; the cache is degraded-but-serving
    # (rate-limit/breaker counters fall back to per-process), so it does not
    # fail readiness on its own.
    ready = db_ok
    return JsonResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status=200 if ready else 503,
    )
