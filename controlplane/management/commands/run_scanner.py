"""Run a platform scanner and catalog discovered agents into the federated registry."""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Crawl a cloud agent platform and catalog discovered agents (as 'discovered')."

    def add_arguments(self, parser):
        parser.add_argument("platform", help="Scanner platform, e.g. 'bedrock'.")

    def handle(self, *args, **options):
        from controlplane.services.scanners import service as scanner_service
        from controlplane.services.scanners.base import ScannerError
        platform = options["platform"]
        try:
            result = scanner_service.run_scan(platform, by="cli")
        except ScannerError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"scanner: platform={result['platform']} discovered={result['discovered']}")
