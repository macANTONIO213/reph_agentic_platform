import time

from django.core.management.base import BaseCommand

from controlplane.services.workflow_queue import workflow_queue


class Command(BaseCommand):
    help = "Process queued WorkflowRun items (pending -> running -> terminal)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
        parser.add_argument("--limit", type=int, default=20, help="Max runs to process per batch.")
        parser.add_argument(
            "--poll-seconds",
            type=int,
            default=5,
            help="Sleep duration between batches when running continuously.",
        )
        parser.add_argument(
            "--max-loops",
            type=int,
            default=0,
            help="Optional safety cap for loop count in continuous mode (0 = unlimited).",
        )
        parser.add_argument(
            "--recover-stale-seconds",
            type=int,
            default=900,
            help="Mark RUNNING runs older than this threshold as FAILED before processing.",
        )

    def handle(self, *args, **options):
        once = options["once"]
        limit = max(1, int(options["limit"]))
        poll_seconds = max(1, int(options["poll_seconds"]))
        max_loops = max(0, int(options["max_loops"]))
        stale_after = max(60, int(options["recover_stale_seconds"]))

        loops = 0
        while True:
            loops += 1
            recovered = workflow_queue.recover_stale_running_runs(stale_after_seconds=stale_after)
            result = workflow_queue.process_pending_runs(limit=limit)
            self.stdout.write(
                f"workflow-worker: recovered={recovered} processed={result['processed']} failed={result['failed']}"
            )

            if once:
                break
            if max_loops and loops >= max_loops:
                break
            time.sleep(poll_seconds)
