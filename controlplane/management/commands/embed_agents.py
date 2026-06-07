"""
Management command: embed_agents

Generates or refreshes vector embeddings for all registered agents.
Skips agents whose text hash hasn't changed.

Run manually:
    python manage.py embed_agents

Run after bulk agent import or platform update:
    python manage.py embed_agents --force

Schedule nightly to catch incremental updates:
    0 3 * * *  python manage.py embed_agents
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate or refresh vector embeddings for all agents (C1 semantic search)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed even if text hash is unchanged.",
        )
        parser.add_argument(
            "--agent",
            type=str,
            default=None,
            help="Limit to a single agent slug.",
        )

    def handle(self, *args, **options):
        from controlplane.models import Agent, AgentEmbedding
        from controlplane.services.embeddings import embedding_service

        force = options["force"]
        agent_slug = options.get("agent")

        agents = Agent.objects.all()
        if agent_slug:
            agents = agents.filter(slug=agent_slug)

        if force:
            # Clear hashes to force re-embed
            AgentEmbedding.objects.filter(agent__in=agents).update(text_hash="")

        counts = {"embedded": 0, "skipped": 0, "failed": 0}

        for agent in agents:
            try:
                written = embedding_service.embed_agent(agent)
                key = "embedded" if written else "skipped"
                counts[key] += 1
                symbol = "✓" if written else "–"
                self.stdout.write(f"  {symbol} {agent.slug}")
            except Exception as exc:
                counts["failed"] += 1
                self.stdout.write(
                    self.style.ERROR(f"  ✗ {agent.slug}: {exc}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {counts['embedded']} embedded, "
                f"{counts['skipped']} skipped, {counts['failed']} failed."
            )
        )
