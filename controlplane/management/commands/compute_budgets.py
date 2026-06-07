"""
Management command: compute_budgets

Checks every agent that has a monthly budget cap (budget_usd_monthly is set)
against their month-to-date spend.  Sets the budget_alert flag and creates
BudgetAlert records when thresholds are exceeded.

Run:
    python manage.py compute_budgets
    python manage.py compute_budgets --dry-run
    python manage.py compute_budgets --agent my-agent-slug

Schedule nightly (or hourly for tight budgets):
    0 * * * *  python manage.py compute_budgets
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone


class Command(BaseCommand):
    help = "Check agent monthly budget caps and set budget_alert flags (Phase D)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to the database.",
        )
        parser.add_argument(
            "--agent",
            type=str,
            default=None,
            help="Limit to a single agent slug.",
        )

    def handle(self, *args, **options):
        from controlplane.models import Agent, AgentRun, AuditLog, BudgetAlert

        dry_run = options["dry_run"]
        agent_slug = options.get("agent")

        now = timezone.now()
        period = now.strftime("%Y-%m")
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        agents = Agent.objects.filter(budget_usd_monthly__isnull=False)
        if agent_slug:
            agents = agents.filter(slug=agent_slug)

        counts = {"breached": 0, "resolved": 0, "ok": 0}

        for agent in agents:
            total = AgentRun.objects.filter(
                agent=agent,
                started_at__gte=month_start,
                status="completed",
            ).aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")

            over_budget = total > agent.budget_usd_monthly
            pct = float(total / agent.budget_usd_monthly * 100) if agent.budget_usd_monthly else 0
            line = (
                f"  {agent.slug:40s}  "
                f"${float(total):8.4f} / ${float(agent.budget_usd_monthly):8.2f}  "
                f"({pct:.1f}%)"
            )

            if over_budget and not agent.budget_alert:
                overage = total - agent.budget_usd_monthly
                self.stdout.write(self.style.ERROR(f"BREACH  {line}"))
                counts["breached"] += 1
                if not dry_run:
                    BudgetAlert.objects.update_or_create(
                        agent=agent,
                        period_month=period,
                        defaults={
                            "budget_usd": agent.budget_usd_monthly,
                            "actual_usd": total,
                            "overage_usd": overage,
                            "resolved": False,
                        },
                    )
                    AuditLog.objects.create(
                        actor="system:compute_budgets",
                        action="budget.breach_detected",
                        resource_type="Agent",
                        resource_id=str(agent.id),
                        payload={
                            "period": period,
                            "budget_usd": float(agent.budget_usd_monthly),
                            "actual_usd": float(total),
                            "overage_usd": float(overage),
                        },
                    )
                    agent.budget_alert = True
                    agent.save(update_fields=["budget_alert"])

            elif not over_budget and agent.budget_alert:
                self.stdout.write(self.style.WARNING(f"RESOLVED {line}"))
                counts["resolved"] += 1
                if not dry_run:
                    BudgetAlert.objects.filter(agent=agent, period_month=period).update(resolved=True)
                    AuditLog.objects.create(
                        actor="system:compute_budgets",
                        action="budget.breach_resolved",
                        resource_type="Agent",
                        resource_id=str(agent.id),
                        payload={"period": period, "actual_usd": float(total)},
                    )
                    agent.budget_alert = False
                    agent.save(update_fields=["budget_alert"])

            else:
                self.stdout.write(f"OK      {line}")
                counts["ok"] += 1

        dry_label = " (DRY RUN — no writes)" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone{dry_label}. "
                f"{counts['breached']} new breach(es), "
                f"{counts['resolved']} resolved, "
                f"{counts['ok']} within budget."
            )
        )
