"""
Management command: compute_baselines

Computes rolling 7-day quality baselines for every agent and flags
quality_alert=True when satisfaction drops >20% below the prior baseline.

Run manually:
    python manage.py compute_baselines

Schedule (cron or Render cron job) to run nightly:
    0 2 * * *  python manage.py compute_baselines

Output:
    Per-agent: baseline score, current score, delta, alert status.
    Summary: total agents checked, alerts raised/cleared.
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg
from django.utils import timezone

from controlplane.models import Agent, AgentFeedback, AuditLog

logger = logging.getLogger(__name__)

# Drift threshold: alert if current score is this far below baseline (absolute points)
DRIFT_THRESHOLD = 1.0   # on a 1–5 scale, 1.0 pt = 20% of max range

# Minimum feedback samples needed before alerting (avoid noise on sparse agents)
MIN_SAMPLES = 3

# Rolling window for baseline (days)
BASELINE_WINDOW_DAYS = 7

# Recent window to compare against baseline (days)
RECENT_WINDOW_DAYS = 2


class Command(BaseCommand):
    help = (
        "Compute rolling quality baselines for all agents and flag "
        "quality_alert=True where satisfaction has drifted >20% below baseline."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print results without writing to the database.",
        )
        parser.add_argument(
            "--agent",
            type=str,
            default=None,
            help="Limit to a single agent slug.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        agent_slug = options.get("agent")

        now = timezone.now()
        baseline_since = now - timedelta(days=BASELINE_WINDOW_DAYS)
        recent_since   = now - timedelta(days=RECENT_WINDOW_DAYS)

        agents = Agent.objects.filter(status__in=["pilot", "production"])
        if agent_slug:
            agents = agents.filter(slug=agent_slug)

        total = alerts_raised = alerts_cleared = 0

        for agent in agents:
            total += 1

            # 7-day rolling baseline  (feedback linked via run__agent)
            baseline_agg = (
                AgentFeedback.objects
                .filter(run__agent=agent, created_at__gte=baseline_since)
                .aggregate(avg=Avg("rating"))
            )
            baseline_score = baseline_agg["avg"]

            if baseline_score is None:
                self.stdout.write(
                    f"  {agent.slug:<40} — skipped (no feedback in {BASELINE_WINDOW_DAYS}d)"
                )
                continue

            # Recent 2-day score
            recent_qs = AgentFeedback.objects.filter(
                run__agent=agent, created_at__gte=recent_since
            )
            recent_count = recent_qs.count()
            recent_score = recent_qs.aggregate(avg=Avg("rating"))["avg"]

            if recent_score is None or recent_count < MIN_SAMPLES:
                self.stdout.write(
                    f"  {agent.slug:<40} — skipped (insufficient recent samples: {recent_count})"
                )
                continue

            delta = recent_score - baseline_score
            should_alert = delta <= -DRIFT_THRESHOLD
            prev_alert   = agent.quality_alert

            status_str = (
                f"baseline={baseline_score:.2f}  recent={recent_score:.2f}  "
                f"delta={delta:+.2f}  alert={'YES' if should_alert else 'no'}"
            )
            self.stdout.write(f"  {agent.slug:<40} {status_str}")

            if not dry_run and should_alert != prev_alert:
                agent.quality_alert = should_alert
                agent.save(update_fields=["quality_alert", "updated_at"])

                action = "quality.drift_detected" if should_alert else "quality.drift_resolved"
                AuditLog.objects.create(
                    actor="system:compute_baselines",
                    action=action,
                    resource_type="Agent",
                    resource_id=str(agent.id),
                    payload={
                        "baseline_score": round(float(baseline_score), 3),
                        "recent_score":   round(float(recent_score), 3),
                        "delta":          round(float(delta), 3),
                        "threshold":      DRIFT_THRESHOLD,
                        "recent_samples": recent_count,
                    },
                )

                if should_alert:
                    alerts_raised += 1
                    logger.warning(
                        "Quality drift detected for agent=%s delta=%.2f",
                        agent.slug, delta,
                    )
                else:
                    alerts_cleared += 1

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes written."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDone. {total} agents checked. "
                    f"{alerts_raised} alert(s) raised, {alerts_cleared} cleared."
                )
            )
