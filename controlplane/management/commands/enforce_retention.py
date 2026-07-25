from datetime import timedelta

from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentRun,
    AuditLog,
    ConversationSession,
    OtelSpan,
    TelemetryEvent,
)


class Command(BaseCommand):
    help = "Enforce data-retention windows and record purge evidence in immutable audit log."

    def add_arguments(self, parser):
        parser.add_argument("--actor", type=str, default="system", help="Audit actor for purge events.")
        parser.add_argument("--dry-run", action="store_true", help="Report purge counts without deleting.")
        parser.add_argument("--telemetry-days", type=int, default=settings.RETENTION_TELEMETRY_DAYS)
        parser.add_argument("--spans-days", type=int, default=settings.RETENTION_SPANS_DAYS)
        parser.add_argument("--sessions-days", type=int, default=settings.RETENTION_SESSIONS_DAYS)
        parser.add_argument("--runs-days", type=int, default=settings.RETENTION_RUNS_DAYS)

    def handle(self, *args, **options):
        now = timezone.now()
        actor = options["actor"] or "system"
        dry_run = bool(options["dry_run"])

        windows = {
            "telemetry_days": max(1, int(options["telemetry_days"])),
            "spans_days": max(1, int(options["spans_days"])),
            "sessions_days": max(1, int(options["sessions_days"])),
            "runs_days": max(1, int(options["runs_days"])),
        }

        cutoffs = {
            "telemetry": now - timedelta(days=windows["telemetry_days"]),
            "spans": now - timedelta(days=windows["spans_days"]),
            "sessions": now - timedelta(days=windows["sessions_days"]),
            "runs": now - timedelta(days=windows["runs_days"]),
        }

        queryset_specs = {
            "telemetry": TelemetryEvent.objects.filter(created_at__lt=cutoffs["telemetry"]),
            "spans": OtelSpan.objects.filter(start_time__lt=cutoffs["spans"]),
            "sessions": ConversationSession.objects.filter(created_at__lt=cutoffs["sessions"]),
            "runs": AgentRun.objects.filter(started_at__lt=cutoffs["runs"]),
        }

        deleted = {}
        for key, qs in queryset_specs.items():
            count = qs.count()
            deleted[key] = count
            if not dry_run and count:
                qs.delete()

        summary = {
            "dry_run": dry_run,
            "cutoffs": {k: v.isoformat() for k, v in cutoffs.items()},
            "deleted": deleted,
        }
        self.stdout.write(f"Retention summary: {summary}")

        if dry_run:
            return

        system_agent, _ = Agent.objects.get_or_create(
            slug="system-retention",
            defaults={
                "name": "System Retention",
                "platform": Agent.Platform.CUSTOM,
                "business_unit": "Platform",
                "owner": "platform",
                "technical_owner": "platform",
                "purpose": "Retention enforcement actor",
                "system_prompt": "N/A",
                "status": Agent.Status.DRAFT,
                "model_id": "n/a",
                "tool_names": [],
                "version": "1.0.0",
            },
        )
        AuditLog.objects.create(
            actor=actor,
            action="retention.enforced",
            resource_type="Agent",
            resource_id=str(system_agent.id),
            payload={
                "reason": "Automated retention enforcement run",
                "cutoffs": {k: v.isoformat() for k, v in cutoffs.items()},
                "deleted": deleted,
            },
        )
        self.stdout.write(self.style.SUCCESS("Retention enforcement completed and audited."))
