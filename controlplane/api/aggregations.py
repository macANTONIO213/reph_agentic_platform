"""
On-the-fly metric aggregations over AgentRun, AgentFeedback, and AgentToolCall.
All functions accept optional filter kwargs and a `window` string ("24h"|"7d"|"30d").
"""
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDay, TruncHour
from django.utils import timezone

from controlplane.models import (
    AgentFeedback,
    AgentRun,
    GovernanceReview,
)


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile. q is 0–100."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    idx = (q / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sorted_values[-1])
    frac = idx - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


def _since(window: str):
    now = timezone.now()
    delta = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}.get(window, timedelta(days=30))
    return now - delta


def _apply_filters(qs, filters: dict):
    if filters.get("agent_id"):
        qs = qs.filter(agent_id=filters["agent_id"])
    if filters.get("platform"):
        qs = qs.filter(agent__platform=filters["platform"])
    if filters.get("business_unit_id"):
        qs = qs.filter(agent__org_unit_id=filters["business_unit_id"])
    if filters.get("division_id"):
        qs = qs.filter(agent__org_division_id=filters["division_id"])
    if filters.get("work_stream_id"):
        qs = qs.filter(agent__org_work_stream_id=filters["work_stream_id"])
    if filters.get("process_id"):
        qs = qs.filter(agent__org_process_id=filters["process_id"])
    return qs


def monitoring_summary(window: str = "30d", **filters) -> dict[str, Any]:
    since = _since(window)
    period = timezone.now() - since
    prev_since = since - period

    qs = _apply_filters(AgentRun.objects.filter(started_at__gte=since), filters)
    prev_qs = _apply_filters(
        AgentRun.objects.filter(started_at__gte=prev_since, started_at__lt=since), filters
    )

    # Pull minimal fields to avoid loading large text blobs
    runs = list(qs.values("status", "latency_ms", "cost_usd", "input_tokens", "output_tokens", "user_label"))
    total_runs = len(runs)
    prev_total = prev_qs.count()

    completed = [r for r in runs if r["status"] == "completed"]
    failed_count = sum(1 for r in runs if r["status"] == "failed")
    success_rate = (len(completed) / total_runs * 100) if total_runs else 0.0

    latencies = sorted(r["latency_ms"] for r in completed if r["latency_ms"] > 0)

    total_cost = sum(float(r["cost_usd"] or 0) for r in runs)
    total_input = sum(r["input_tokens"] for r in runs)
    total_output = sum(r["output_tokens"] for r in runs)
    active_users = len({r["user_label"] for r in runs})

    fb_qs = AgentFeedback.objects.filter(run__started_at__gte=since)
    if filters.get("agent_id"):
        fb_qs = fb_qs.filter(run__agent_id=filters["agent_id"])
    avg_sat = fb_qs.aggregate(avg=Avg("rating"))["avg"] or 0

    pending_reviews = GovernanceReview.objects.filter(status=GovernanceReview.Status.PENDING).count()

    prev_success_rate = 0.0
    if prev_total:
        prev_succeeded = prev_qs.filter(status="completed").count()
        prev_success_rate = prev_succeeded / prev_total * 100

    return {
        "total_runs": total_runs,
        "prev_total_runs": prev_total,
        "run_delta_pct": _delta_pct(total_runs, prev_total),
        "succeeded": len(completed),
        "failed": failed_count,
        "success_rate": round(success_rate, 1),
        "prev_success_rate": round(prev_success_rate, 1),
        "p50_latency_ms": round(percentile(latencies, 50)),
        "p95_latency_ms": round(percentile(latencies, 95)),
        "p99_latency_ms": round(percentile(latencies, 99)),
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "avg_satisfaction": round(float(avg_sat), 2),
        "active_users": active_users,
        "pending_reviews": pending_reviews,
    }


def runs_timeseries(window: str = "30d", bucket: str = "day", **filters) -> list[dict]:
    since = _since(window)
    trunc = TruncDay if bucket != "hour" else TruncHour
    qs = _apply_filters(AgentRun.objects.filter(started_at__gte=since), filters)
    rows = (
        qs.annotate(bucket=trunc("started_at"))
        .values("bucket")
        .annotate(
            total=Count("id"),
            succeeded=Count("id", filter=Q(status="completed")),
            failed=Count("id", filter=Q(status="failed")),
            cost=Sum("cost_usd"),
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
        )
        .order_by("bucket")
    )
    return [
        {
            "date": r["bucket"].strftime("%Y-%m-%d") if bucket == "day" else r["bucket"].isoformat(),
            "total": r["total"],
            "succeeded": r["succeeded"],
            "failed": r["failed"],
            "cost_usd": round(float(r["cost"] or 0), 4),
            "input_tokens": r["input_tokens"] or 0,
            "output_tokens": r["output_tokens"] or 0,
        }
        for r in rows
    ]


def latency_timeseries(window: str = "30d", bucket: str = "day", **filters) -> list[dict]:
    since = _since(window)
    trunc = TruncDay if bucket != "hour" else TruncHour
    qs = _apply_filters(
        AgentRun.objects.filter(started_at__gte=since, status="completed", latency_ms__gt=0),
        filters,
    )
    rows = (
        qs.annotate(bucket=trunc("started_at"))
        .values("bucket")
        .annotate(avg_latency=Avg("latency_ms"))
        .order_by("bucket")
    )
    return [
        {
            "date": r["bucket"].strftime("%Y-%m-%d") if bucket == "day" else r["bucket"].isoformat(),
            "avg_latency_ms": round(float(r["avg_latency"] or 0)),
        }
        for r in rows
    ]


def runs_by_platform(window: str = "30d") -> list[dict]:
    since = _since(window)
    rows = (
        AgentRun.objects.filter(started_at__gte=since)
        .values("agent__platform")
        .annotate(count=Count("id"), cost=Sum("cost_usd"))
        .order_by("-count")
    )
    return [
        {"platform": r["agent__platform"] or "unknown", "count": r["count"], "cost_usd": round(float(r["cost"] or 0), 4)}
        for r in rows
    ]


def runs_by_agent(window: str = "30d") -> list[dict]:
    since = _since(window)
    rows = (
        AgentRun.objects.filter(started_at__gte=since)
        .values("agent__id", "agent__name")
        .annotate(
            count=Count("id"),
            succeeded=Count("id", filter=Q(status="completed")),
            cost=Sum("cost_usd"),
            avg_latency=Avg("latency_ms"),
        )
        .order_by("-count")
    )
    return [
        {
            "agent_id": str(r["agent__id"]),
            "agent_name": r["agent__name"],
            "count": r["count"],
            "succeeded": r["succeeded"],
            "cost_usd": round(float(r["cost"] or 0), 4),
            "avg_latency_ms": round(float(r["avg_latency"] or 0)),
        }
        for r in rows
    ]


def rating_distribution(window: str = "30d", **filters) -> list[dict]:
    since = _since(window)
    qs = AgentFeedback.objects.filter(created_at__gte=since)
    if filters.get("agent_id"):
        qs = qs.filter(run__agent_id=filters["agent_id"])
    rows = qs.values("rating").annotate(count=Count("id")).order_by("rating")
    dist = {r["rating"]: r["count"] for r in rows}
    return [{"rating": i, "count": dist.get(i, 0)} for i in range(1, 6)]


def low_rated_runs(window: str = "30d", threshold: int = 2) -> list[dict]:
    since = _since(window)
    feedbacks = (
        AgentFeedback.objects.filter(created_at__gte=since, rating__lte=threshold)
        .select_related("run__agent")
        .order_by("-created_at")[:50]
    )
    return [
        {
            "run_id": str(f.run_id),
            "agent_name": f.run.agent.name,
            "rating": f.rating,
            "comment": f.comment,
            "submitted_by": f.submitted_by,
            "created_at": f.created_at.isoformat(),
        }
        for f in feedbacks
    ]


def agent_catalog_telemetry(window: str = "30d") -> dict[str, dict]:
    """Per-agent telemetry summary for the catalog table. Returns dict keyed by agent_id."""
    since = _since(window)
    rows = (
        AgentRun.objects.filter(started_at__gte=since)
        .values("agent_id")
        .annotate(
            runs=Count("id"),
            succeeded=Count("id", filter=Q(status="completed")),
            cost=Sum("cost_usd"),
            avg_latency=Avg("latency_ms"),
            last_run=Count("id"),  # placeholder; we fetch max separately
        )
    )
    result = {}
    for r in rows:
        total = r["runs"] or 0
        success_rate = (r["succeeded"] / total * 100) if total else 0.0
        result[str(r["agent_id"])] = {
            "runs_period": total,
            "success_rate": round(success_rate, 1),
            "cost_period": round(float(r["cost"] or 0), 4),
            "avg_latency_ms": round(float(r["avg_latency"] or 0)),
        }

    # Satisfaction per agent
    fb_rows = (
        AgentFeedback.objects.filter(created_at__gte=since)
        .values("run__agent_id")
        .annotate(avg_rating=Avg("rating"))
    )
    for r in fb_rows:
        aid = str(r["run__agent_id"])
        if aid in result:
            result[aid]["avg_satisfaction"] = round(float(r["avg_rating"] or 0), 2)

    # Last run timestamp
    last_rows = (
        AgentRun.objects.filter(started_at__gte=since)
        .values("agent_id")
        .annotate(last=Count("id"))
        .order_by()
    )
    # Use a separate query for max started_at
    from django.db.models import Max
    last_ts = (
        AgentRun.objects.filter(started_at__gte=since)
        .values("agent_id")
        .annotate(last_run_at=Max("started_at"))
    )
    for r in last_ts:
        aid = str(r["agent_id"])
        if aid in result:
            result[aid]["last_run_at"] = r["last_run_at"].isoformat() if r["last_run_at"] else None

    return result


def _delta_pct(current: int | float, previous: int | float) -> float | None:
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)
