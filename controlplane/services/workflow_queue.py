"""
Workflow queue utilities for durable background execution.

This replaces request-thread execution with database-backed queue semantics:
  - enqueue() creates a pending WorkflowRun
  - claim_next_pending_run() atomically claims the oldest pending run
  - process_pending_runs() drains N runs in worker context
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from controlplane.models import WorkflowRun
from controlplane.services.orchestrator import orchestrator

logger = logging.getLogger(__name__)


class WorkflowQueueService:
    def enqueue(self, workflow, *, inputs: dict | None = None, triggered_by: str = "system") -> WorkflowRun:
        run = orchestrator.start(workflow, inputs=inputs or {}, triggered_by=triggered_by)
        self.dispatch_run(run)
        return run

    def dispatch_run(self, run: WorkflowRun) -> None:
        """
        Hand a pending run to the active execution backend.

        Under EXECUTION_BACKEND=celery the run is dispatched to a worker
        immediately.  Otherwise it stays PENDING for the ``process_workflow_runs``
        management command (the legacy DB-backed queue) to drain — so callers get
        durable execution either way.
        """
        from controlplane.services.agent_tasks import _celery_enabled
        if not _celery_enabled():
            return
        from controlplane.tasks import execute_workflow_run
        execute_workflow_run.delay(str(run.id))

    def claim_next_pending_run(self) -> WorkflowRun | None:
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update()
                .filter(status=WorkflowRun.Status.PENDING)
                .order_by("started_at")
                .first()
            )
            if run is None:
                return None
            run.status = WorkflowRun.Status.RUNNING
            run.save(update_fields=["status"])
            return run

    def recover_stale_running_runs(self, *, stale_after_seconds: int = 900) -> int:
        cutoff = timezone.now() - timedelta(seconds=stale_after_seconds)
        stale_qs = WorkflowRun.objects.filter(
            status=WorkflowRun.Status.RUNNING,
            started_at__lt=cutoff,
            completed_at__isnull=True,
        )
        count = 0
        for run in stale_qs:
            run.status = WorkflowRun.Status.FAILED
            run.error = (
                "Recovered by workflow worker: run exceeded stale threshold "
                f"({stale_after_seconds}s) while in RUNNING state."
            )
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "error", "completed_at"])
            count += 1
        return count

    def process_pending_runs(self, *, limit: int = 20) -> dict:
        processed = 0
        failed = 0
        while processed < limit:
            run = self.claim_next_pending_run()
            if run is None:
                break
            try:
                orchestrator.execute(run)
            except Exception:
                logger.exception("Workflow worker failed executing run %s", run.id)
                failed += 1
            processed += 1
        return {"processed": processed, "failed": failed}


workflow_queue = WorkflowQueueService()
