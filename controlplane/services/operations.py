"""
Phase 2 operational jobs — run by the ``controlplane.maintenance`` beat task.

  - dispatch_scheduled_workflows: interval-triggered workflow runs (OE-2)
  - check_slos: SLO breach detection → alert fan-out (OE-3)
  - send_digest: daily operational digest (AR-4)
  - export_evidence: tamper-evident compliance bundle (GV-4)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


def dispatch_scheduled_workflows() -> int:
    """Enqueue every active workflow whose interval schedule is due (OE-2)."""
    from controlplane.models import Workflow
    from controlplane.services.workflow_queue import workflow_queue

    now = timezone.now()
    dispatched = 0
    due = Workflow.objects.filter(
        status=Workflow.Status.ACTIVE, run_interval_minutes__gt=0
    )
    for w in due:
        if w.next_run_at is None:
            # First sighting: anchor the schedule, don't fire immediately.
            w.next_run_at = now + timedelta(minutes=w.run_interval_minutes)
            w.save(update_fields=["next_run_at", "updated_at"])
            continue
        if w.next_run_at > now:
            continue
        workflow_queue.enqueue(
            w, inputs={}, triggered_by="scheduler",
            idempotency_key=f"sched:{w.id}:{w.next_run_at.isoformat()}",
        )
        w.next_run_at = now + timedelta(minutes=w.run_interval_minutes)
        w.save(update_fields=["next_run_at", "updated_at"])
        dispatched += 1
    return dispatched


def check_slos() -> list[str]:
    """Evaluate platform SLOs over the last hour; alert on breach (OE-3)."""
    from controlplane.models import AgentRun, WorkflowRun
    from controlplane.services.alerts import send_alert

    since = timezone.now() - timedelta(hours=1)
    runs = AgentRun.objects.filter(started_at__gte=since)
    total = runs.count()
    breaches: list[str] = []

    if total >= 10:  # too little traffic ⇒ noise, not signal
        completed = runs.filter(status="completed")
        success_rate = completed.count() / total * 100
        target = float(getattr(settings, "PLATFORM_SLO_SUCCESS_RATE_TARGET", 99.0))
        if success_rate < target:
            breaches.append(f"Success rate {success_rate:.1f}% < target {target}% ({total} runs/1h)")

        latencies = sorted(completed.exclude(latency_ms=None).values_list("latency_ms", flat=True))
        if latencies:
            p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            p95_target = int(getattr(settings, "PLATFORM_SLO_P95_LATENCY_MS_TARGET", 2000))
            if p95 > p95_target:
                breaches.append(f"p95 latency {p95}ms > target {p95_target}ms")

    pending = WorkflowRun.objects.filter(status=WorkflowRun.Status.PENDING).count()
    threshold = int(getattr(settings, "PLATFORM_QUEUE_PENDING_WARN_THRESHOLD", 100))
    if pending > threshold:
        breaches.append(f"Workflow queue backlog {pending} > threshold {threshold}")

    for breach in breaches:
        # One alert per breach type per hour — pages, not spam.
        key = "slo:alerted:" + hashlib.sha256(breach.split(">")[0].split("<")[0].encode()).hexdigest()[:16]
        if cache.add(key, 1, timeout=3600):
            send_alert("SLO breach", breach, category="ops")
    return breaches


def send_digest() -> dict:
    """Daily operational digest to admins/email/webhook (AR-4)."""
    from django.db.models import Sum

    from controlplane.models import (
        AgentRun, BudgetAlert, GovernanceReview, WorkflowRun,
    )
    from controlplane.services.alerts import send_alert

    now = timezone.now()
    day_ago = now - timedelta(days=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    runs = AgentRun.objects.filter(started_at__gte=day_ago)
    stats = {
        "runs_24h": runs.count(),
        "failed_24h": runs.filter(status="failed").count(),
        "cost_mtd_usd": float(
            AgentRun.objects.filter(started_at__gte=month_start).aggregate(t=Sum("cost_usd"))["t"] or 0
        ),
        "pending_reviews": GovernanceReview.objects.filter(status="pending").count(),
        "active_budget_alerts": BudgetAlert.objects.filter(resolved=False).count(),
        "dead_letters": WorkflowRun.objects.filter(status=WorkflowRun.Status.DEAD_LETTER).count(),
    }
    send_alert(
        "Daily platform digest",
        (
            f"Runs (24h): {stats['runs_24h']} ({stats['failed_24h']} failed)\n"
            f"Cost (MTD): ${stats['cost_mtd_usd']:.2f}\n"
            f"Pending governance reviews: {stats['pending_reviews']}\n"
            f"Active budget alerts: {stats['active_budget_alerts']}\n"
            f"Dead-lettered runs: {stats['dead_letters']}"
        ),
        category="ops",
        link="/console/",
    )
    return stats


def export_evidence() -> dict:
    """Scheduled, tamper-evident compliance evidence bundle (GV-4)."""
    from django.core.management import call_command

    from controlplane.models import AuditLog

    out_dir = settings.BASE_DIR / "var" / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"evidence-{timezone.now():%Y%m%d}.json"
    call_command("export_compliance_evidence", "--output", str(path))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # The append-only AuditLog row is the tamper-evidence anchor: recomputing
    # the file hash must match this recorded digest.
    AuditLog.objects.create(
        actor="system:export_evidence",
        action="compliance.evidence_exported",
        resource_type="EvidenceBundle",
        resource_id=path.name,
        payload={"path": str(path), "sha256": digest},
    )
    return {"path": str(path), "sha256": digest}


def factory_feedback() -> int:
    """
    AI-6 (start): write runtime telemetry back onto the blueprint that built
    each agent, closing the discover → build → learn loop. The snapshot feeds
    the portfolio ranking (/api/v1/factory/portfolio/).
    """
    from django.db.models import Avg, Count, Q, Sum

    from controlplane.models import AgentBlueprint, AgentRun

    since = timezone.now() - timedelta(days=30)
    updated = 0
    for bp in AgentBlueprint.objects.filter(built_agent__isnull=False).select_related("built_agent"):
        stats = AgentRun.objects.filter(agent=bp.built_agent, started_at__gte=since).aggregate(
            runs=Count("id"),
            failed=Count("id", filter=Q(status="failed")),
            cost=Sum("cost_usd"),
            rating=Avg("feedback__rating"),
        )
        runs = stats["runs"] or 0
        bp.runtime_feedback = {
            "runs_30d": runs,
            "failure_rate": round((stats["failed"] or 0) / runs, 3) if runs else 0.0,
            "avg_rating": round(float(stats["rating"]), 2) if stats["rating"] else None,
            "cost_30d_usd": float(stats["cost"] or 0),
            "computed_at": timezone.now().isoformat(),
        }
        bp.save(update_fields=["runtime_feedback", "updated_at"])
        updated += 1
    return updated


def escalate_stale_items() -> list[str]:
    """
    OE-5 + GV-6: escalate governance reviews pending beyond the SLA and
    overdue risk-register reviews. One alert per item per day (cache dedupe).
    """
    from controlplane.models import GovernanceReview, RiskItem
    from controlplane.services.alerts import send_alert

    escalated: list[str] = []
    sla_days = int(getattr(settings, "REVIEW_ESCALATION_DAYS", 3))
    cutoff = timezone.now() - timedelta(days=sla_days)

    for review in GovernanceReview.objects.filter(status="pending", created_at__lt=cutoff).select_related("agent"):
        key = f"escalation:review:{review.id}"
        if cache.add(key, 1, timeout=86400):
            age = (timezone.now() - review.created_at).days
            send_alert(
                f"Governance review overdue: {review.agent.name}",
                f"Pending for {age} days (SLA {sla_days}d). Decide it in the console.",
                category="governance", link="/console/",
            )
            escalated.append(str(review.id))

    today = timezone.now().date()
    for risk in RiskItem.objects.filter(status__in=["open", "mitigating"], review_by__lt=today):
        key = f"escalation:risk:{risk.id}"
        if cache.add(key, 1, timeout=86400):
            send_alert(
                f"Risk review overdue: {risk.title}",
                f"Severity {risk.severity}; review was due {risk.review_by}.",
                category="governance",
            )
            escalated.append(str(risk.id))
    return escalated
