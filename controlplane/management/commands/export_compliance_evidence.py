import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from controlplane.models import Approval, AuditLog, EvalRun, GovernanceReview, WorkflowRun


class Command(BaseCommand):
    help = "Export governance/compliance evidence summary (SOC2/ISO control evidence snapshot)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Lookback window in days for evidence counts.",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Optional file path to write evidence JSON.",
        )

    def handle(self, *args, **options):
        days = max(1, int(options["days"]))
        since = timezone.now() - timedelta(days=days)

        action_rows = (
            AuditLog.objects.filter(created_at__gte=since)
            .values("action")
            .annotate(total=Count("id"))
        )
        action_counts = {row["action"]: row["total"] for row in action_rows}

        evidence = {
            "generated_at": timezone.now().isoformat(),
            "window_days": days,
            "controls": {
                "access_change_control": {
                    "approvals_created": Approval.objects.filter(created_at__gte=since).count(),
                    "active_approvals": Approval.objects.filter(
                        is_consumed=False, expires_at__gt=timezone.now()
                    ).count(),
                    "governance_reviews_approved": GovernanceReview.objects.filter(
                        created_at__gte=since, status=GovernanceReview.Status.APPROVED
                    ).count(),
                    "governance_reviews_rejected": GovernanceReview.objects.filter(
                        created_at__gte=since, status=GovernanceReview.Status.REJECTED
                    ).count(),
                },
                "model_quality_controls": {
                    "eval_runs_total": EvalRun.objects.filter(executed_at__gte=since).count(),
                    "eval_runs_passed": EvalRun.objects.filter(executed_at__gte=since, passed=True).count(),
                    "eval_runs_failed_or_error": EvalRun.objects.filter(executed_at__gte=since).exclude(
                        passed=True
                    ).count(),
                },
                "operational_governance": {
                    "workflow_runs_total": WorkflowRun.objects.filter(started_at__gte=since).count(),
                    "workflow_runs_failed": WorkflowRun.objects.filter(
                        started_at__gte=since, status=WorkflowRun.Status.FAILED
                    ).count(),
                    "forced_transitions": action_counts.get("agent.transition.forced", 0),
                },
                "audit_integrity": {
                    "audit_events_total": AuditLog.objects.filter(created_at__gte=since).count(),
                    "top_actions": sorted(
                        [{"action": k, "count": v} for k, v in action_counts.items()],
                        key=lambda x: x["count"],
                        reverse=True,
                    )[:25],
                },
            },
        }

        out = json.dumps(evidence, indent=2, sort_keys=True)
        output_path = options.get("output", "").strip()
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(out)
            self.stdout.write(self.style.SUCCESS(f"Evidence written to {output_path}"))
            return
        self.stdout.write(out)
