import json

from django.core.management.base import BaseCommand, CommandError

from controlplane.services.platform_maturity import maturity_snapshot


class Command(BaseCommand):
    help = "Compute a platform engineering maturity scorecard snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--window-hours", type=int, default=24, help="Lookback window in hours.")
        parser.add_argument("--json", action="store_true", help="Emit JSON output.")
        parser.add_argument(
            "--fail-on-unready",
            action="store_true",
            help="Exit non-zero when any check is in fail state.",
        )

    def handle(self, *args, **options):
        payload = maturity_snapshot(window_hours=max(1, int(options["window_hours"])))
        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        else:
            s = payload["summary"]
            self.stdout.write(
                f"platform_maturity score={s['score_pct']} tier={s['tier']} "
                f"unready={s['unready']} runs={s['total_runs']}"
            )
            for check in payload["checks"]:
                self.stdout.write(f" - {check['name']}: {check['status']} value={check['value']}")

        if options["fail_on_unready"] and payload["summary"]["unready"]:
            raise CommandError("Platform maturity contains failing checks.")
