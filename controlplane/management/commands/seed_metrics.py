"""
seed_metrics — generate a few weeks of realistic AgentRun / AgentFeedback history.
Run after seed_demo. Safe to re-run (adds rows; won't duplicate if --clear is passed).
"""
import random
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from controlplane.models import Agent, AgentFeedback, AgentRun, AgentToolCall, TelemetryEvent
from controlplane.services.pricing import price_run


class Command(BaseCommand):
    help = "Seed historical AgentRun/Feedback/Telemetry rows for monitoring charts."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=35, help="Days of history to generate")
        parser.add_argument("--clear", action="store_true", help="Delete existing seeded runs first")

    def handle(self, *args, **options):
        if options["clear"]:
            AgentRun.objects.all().delete()
            self.stdout.write("Cleared existing runs.")

        agents = list(Agent.objects.all())
        if not agents:
            self.stderr.write("No agents found — run seed_demo first.")
            return

        days = options["days"]
        now = timezone.now()
        total = 0

        model_choices = [
            ("claude-opus-4-8", 0.25),
            ("claude-sonnet-4-6", 0.45),
            ("claude-haiku-4-5", 0.15),
            ("gpt-4o", 0.10),
            ("fake", 0.05),
        ]
        models, model_weights = zip(*model_choices)

        users = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "admin"]
        error_types = ["timeout", "rate_limit", "context_too_long", "tool_error"]

        for day_offset in range(days, 0, -1):
            day_start = now - timedelta(days=day_offset)
            # More runs on weekdays
            weekday = day_start.weekday()  # 0=Mon
            base_runs = 8 if weekday < 5 else 3
            daily_runs = int(random.gauss(base_runs, 2))
            daily_runs = max(1, daily_runs)

            for _ in range(daily_runs):
                agent = random.choice(agents)
                model_id = random.choices(models, weights=model_weights)[0]
                user = random.choice(users)

                # Random time within the day
                run_start = day_start + timedelta(
                    hours=random.uniform(7, 22),
                    minutes=random.uniform(0, 59),
                )

                # 85% success rate; Tier-4 agents have lower success
                fail_prob = 0.30 if agent.risk_tier >= 4 else 0.12
                failed = random.random() < fail_prob

                latency = int(random.gauss(1800 if failed else 1200, 400))
                latency = max(200, latency)

                input_tokens = random.randint(200, 1800)
                output_tokens = 0 if failed else random.randint(150, 1200)

                cost = price_run(input_tokens, output_tokens, model_id)

                run = AgentRun(
                    agent=agent,
                    user_label=user,
                    channel=random.choice(["web", "web", "api", "slack"]),
                    input_text="[seeded prompt]",
                    output_text="" if failed else "[seeded output]",
                    status=AgentRun.Status.FAILED if failed else AgentRun.Status.COMPLETED,
                    latency_ms=latency,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_id=model_id,
                    cost_usd=cost,
                    completed_at=run_start + timedelta(milliseconds=latency),
                )
                run.save()
                # Override auto_now_add
                AgentRun.objects.filter(pk=run.pk).update(started_at=run_start)

                # Tool calls (1-3 per successful run, 30% chance)
                if not failed and random.random() < 0.45:
                    num_tools = random.randint(1, 3)
                    tool_names = ["registry_search", "risk_classifier", "deployment_gate_builder"]
                    for _ in range(num_tools):
                        AgentToolCall.objects.create(
                            run=run,
                            tool_name=random.choice(tool_names),
                            input_payload={"query": "seeded"},
                            output_payload={"result": "seeded"},
                            duration_ms=random.randint(50, 400),
                        )

                # Feedback (40% of completed runs)
                if not failed and random.random() < 0.40:
                    # Skew ratings toward 4-5 for good agents
                    if agent.risk_tier <= 2:
                        rating = random.choices([1, 2, 3, 4, 5], weights=[3, 5, 10, 35, 47])[0]
                    else:
                        rating = random.choices([1, 2, 3, 4, 5], weights=[8, 12, 20, 35, 25])[0]
                    fb = AgentFeedback(
                        run=run,
                        rating=rating,
                        comment="[seeded feedback]" if rating <= 2 else "",
                        submitted_by=user,
                    )
                    fb.save()
                    AgentFeedback.objects.filter(pk=fb.pk).update(
                        created_at=run_start + timedelta(minutes=random.randint(1, 30))
                    )

                # Telemetry
                if agent.telemetry_enabled:
                    evt = TelemetryEvent(
                        agent=agent,
                        run=run,
                        event_type="task_failed" if failed else "task_completed",
                        actor=user,
                        business_unit=agent.business_unit,
                        payload={
                            "latency_ms": latency,
                            "model_id": model_id,
                            "seeded": True,
                        },
                    )
                    evt.save()
                    TelemetryEvent.objects.filter(pk=evt.pk).update(created_at=run_start)

                total += 1

        # Recompute satisfaction scores
        for agent in agents:
            from django.db.models import Avg
            avg = AgentFeedback.objects.filter(run__agent=agent).aggregate(avg=Avg("rating"))["avg"]
            if avg is not None:
                Agent.objects.filter(pk=agent.pk).update(satisfaction_score=round(avg, 2))

        self.stdout.write(self.style.SUCCESS(f"Seeded {total} runs across {days} days."))
