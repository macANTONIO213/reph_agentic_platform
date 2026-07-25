"""Backfill/refresh the federated RegistryEntry catalog from current sources."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Project published first-party agents and active MCP servers into the federated registry."

    def handle(self, *args, **options):
        from controlplane.services.interop import federation
        result = federation.sync_all()
        self.stdout.write(
            f"registry-sync: agents={result['agents']} mcp_servers={result['mcp_servers']}"
        )
