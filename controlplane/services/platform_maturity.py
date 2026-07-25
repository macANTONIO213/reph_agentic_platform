from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Model
from django.utils import timezone

from controlplane.models import AgentRun, AuditLog, BudgetAlert, WorkflowRun


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = int(round((pct / 100) * (len(ordered) - 1)))
    return ordered[max(0, min(idx, len(ordered) - 1))]


def _check_status(pass_condition: bool, warn_condition: bool | None = None) -> str:
    if pass_condition:
        return "pass"
    if warn_condition is None or warn_condition:
        return "warn"
    return "fail"


def _score(check_status: str) -> float:
    if check_status == "pass":
        return 1.0
    if check_status == "warn":
        return 0.5
    return 0.0


def _tier(score_pct: float) -> str:
    if score_pct >= 85:
        return "enterprise-ready"
    if score_pct >= 70:
        return "managed"
    if score_pct >= 50:
        return "developing"
    return "at-risk"


def maturity_snapshot(*, window_hours: int = 24) -> dict:
    now = timezone.now()
    since = now - timedelta(hours=max(1, int(window_hours)))

    recent_runs = AgentRun.objects.filter(started_at__gte=since)
    total_runs = recent_runs.count()
    completed_runs = recent_runs.filter(status=AgentRun.Status.COMPLETED).count()
    failed_runs = recent_runs.filter(status=AgentRun.Status.FAILED).count()
    success_rate = ((completed_runs / total_runs) * 100.0) if total_runs else 100.0
    p95_latency_ms = _percentile(
        list(
            recent_runs.filter(status=AgentRun.Status.COMPLETED)
            .values_list("latency_ms", flat=True)
        ),
        95,
    )

    pending_workflows = WorkflowRun.objects.filter(status=WorkflowRun.Status.PENDING).count()
    stale_running_workflows = WorkflowRun.objects.filter(
        status=WorkflowRun.Status.RUNNING,
        started_at__lt=now - timedelta(minutes=settings.PLATFORM_QUEUE_STALE_MINUTES),
    ).count()
    active_budget_alerts = BudgetAlert.objects.filter(resolved=False).count()
    has_release_gate = Path(settings.BASE_DIR / ".github" / "workflows" / "release-gate.yml").exists()

    checks = [
        {
            "name": "release_gate",
            "status": "pass" if has_release_gate else "fail",
            "detail": "Release gate workflow is configured.",
            "value": has_release_gate,
            "target": True,
        },
        {
            "name": "runtime_success_rate",
            "status": _check_status(
                success_rate >= settings.PLATFORM_SLO_SUCCESS_RATE_TARGET,
                warn_condition=total_runs == 0 or success_rate >= 95.0,
            ),
            "detail": "Completed run ratio over the lookback window.",
            "value": round(success_rate, 2),
            "target": float(settings.PLATFORM_SLO_SUCCESS_RATE_TARGET),
        },
        {
            "name": "runtime_latency_p95_ms",
            "status": _check_status(
                p95_latency_ms <= settings.PLATFORM_SLO_P95_LATENCY_MS_TARGET,
                warn_condition=p95_latency_ms <= (settings.PLATFORM_SLO_P95_LATENCY_MS_TARGET * 1.5),
            ),
            "detail": "P95 latency over completed runs in the lookback window.",
            "value": p95_latency_ms,
            "target": int(settings.PLATFORM_SLO_P95_LATENCY_MS_TARGET),
        },
        {
            "name": "workflow_queue_backlog",
            "status": _check_status(
                pending_workflows <= settings.PLATFORM_QUEUE_PENDING_WARN_THRESHOLD and stale_running_workflows == 0,
                warn_condition=stale_running_workflows == 0,
            ),
            "detail": "Workflow queue pending depth and stale running detection.",
            "value": {
                "pending": pending_workflows,
                "stale_running": stale_running_workflows,
            },
            "target": {
                "pending_lte": int(settings.PLATFORM_QUEUE_PENDING_WARN_THRESHOLD),
                "stale_running_eq": 0,
            },
        },
        {
            "name": "active_budget_alerts",
            "status": _check_status(
                active_budget_alerts <= settings.PLATFORM_ACTIVE_BUDGET_ALERTS_WARN_THRESHOLD,
                warn_condition=active_budget_alerts <= (settings.PLATFORM_ACTIVE_BUDGET_ALERTS_WARN_THRESHOLD * 2),
            ),
            "detail": "Unresolved budget alerts across all agents.",
            "value": active_budget_alerts,
            "target": int(settings.PLATFORM_ACTIVE_BUDGET_ALERTS_WARN_THRESHOLD),
        },
    ]

    score_pct = round((sum(_score(c["status"]) for c in checks) / len(checks)) * 100.0, 2)
    unready = any(c["status"] == "fail" for c in checks)

    return {
        "generated_at": now.isoformat(),
        "window_hours": int(window_hours),
        "summary": {
            "score_pct": score_pct,
            "tier": _tier(score_pct),
            "unready": unready,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "failed_runs": failed_runs,
        },
        "checks": checks,
    }


def enterprise_success_criteria(*, window_hours: int = 24) -> dict:
    snapshot = maturity_snapshot(window_hours=window_hours)
    checks = {c["name"]: c for c in snapshot["checks"]}

    controls = {
        "release_gate": bool(checks.get("release_gate", {}).get("value", False)),
        "has_retention_command": Path(
            settings.BASE_DIR / "controlplane" / "management" / "commands" / "enforce_retention.py"
        ).exists(),
        "has_compliance_export_command": Path(
            settings.BASE_DIR / "controlplane" / "management" / "commands" / "export_compliance_evidence.py"
        ).exists(),
        "audit_log_immutable": (
            AuditLog.save is not Model.save and AuditLog.delete is not Model.delete
        ),
    }

    criteria = [
        {
            "id": "maturity-score",
            "description": "Platform maturity score meets enterprise threshold.",
            "passed": snapshot["summary"]["score_pct"] >= settings.PLATFORM_ENTERPRISE_MIN_SCORE,
            "value": snapshot["summary"]["score_pct"],
            "target": float(settings.PLATFORM_ENTERPRISE_MIN_SCORE),
        },
        {
            "id": "no-failing-operational-checks",
            "description": "No failing operational checks in readiness scorecard.",
            "passed": not snapshot["summary"]["unready"],
            "value": snapshot["summary"]["unready"],
            "target": False,
        },
        {
            "id": "governance-and-compliance-controls-present",
            "description": "Audit immutability and compliance commands are present.",
            "passed": all(controls.values()),
            "value": controls,
            "target": {
                "release_gate": True,
                "has_retention_command": True,
                "has_compliance_export_command": True,
                "audit_log_immutable": True,
            },
        },
    ]

    enterprise_grade = all(item["passed"] for item in criteria)
    return {
        "generated_at": snapshot["generated_at"],
        "window_hours": snapshot["window_hours"],
        "enterprise_grade": enterprise_grade,
        "criteria": criteria,
        "maturity_summary": snapshot["summary"],
        "maturity_checks": snapshot["checks"],
    }
