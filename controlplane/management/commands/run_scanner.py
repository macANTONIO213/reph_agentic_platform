"""Run a platform scanner and catalog discovered agents into the federated registry."""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Crawl a cloud agent platform and catalog discovered agents (as 'discovered')."

    def add_arguments(self, parser):
        parser.add_argument("platform", help="Scanner platform (e.g. 'bedrock'), or 'all'.")

    def handle(self, *args, **options):
        from controlplane.services.scanners import service as scanner_service
        from controlplane.services.scanners.base import ScannerError
        platform = options["platform"]

        if platform == "all":
            result = scanner_service.run_all_scans(by="cli")
            for r in result["results"]:
                if "error" in r:
                    self.stdout.write(f"  {r['platform']}: skipped ({r['error']})")
                else:
                    self.stdout.write(f"  {r['platform']}: discovered={r['discovered']}")
            self.stdout.write(f"scanner: total_discovered={result['total_discovered']}")
            return

        try:
            result = scanner_service.run_scan(platform, by="cli")
        except ScannerError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"scanner: platform={result['platform']} discovered={result['discovered']}")
