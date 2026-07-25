import json

from django.core.management.base import BaseCommand, CommandError

from controlplane.services.platform_maturity import enterprise_success_criteria


class Command(BaseCommand):
    help = "Evaluate enterprise-grade success criteria for the platform."

    def add_arguments(self, parser):
        parser.add_argument("--window-hours", type=int, default=24, help="Lookback window in hours.")
        parser.add_argument("--json", action="store_true", help="Emit JSON output.")
        parser.add_argument(
            "--fail-on-miss",
            action="store_true",
            help="Exit non-zero when enterprise success criteria are not fully met.",
        )

    def handle(self, *args, **options):
        payload = enterprise_success_criteria(window_hours=max(1, int(options["window_hours"])))
        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        else:
            self.stdout.write(
                f"enterprise_grade={payload['enterprise_grade']} "
                f"score={payload['maturity_summary']['score_pct']} "
                f"tier={payload['maturity_summary']['tier']}"
            )
            for item in payload["criteria"]:
                self.stdout.write(
                    f" - {item['id']}: {'pass' if item['passed'] else 'fail'} "
                    f"value={item['value']}"
                )

        if options["fail_on_miss"] and not payload["enterprise_grade"]:
            raise CommandError("Enterprise success criteria are not fully met.")
