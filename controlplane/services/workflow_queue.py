"""
Workflow queue utilities for durable background execution.

This replaces request-thread execution with database-backed queue semantics:
  - enqueue() creates a pending WorkflowRun (idempotent when a key is supplied)
  - claim_next_pending_run() / try_claim_run() atomically claim a pending run,
    so a Celery redelivery and a DB worker can never double-execute one run
  - process_pending_runs() drains N runs in worker context
  - recover_stale_running_runs() requeues crashed runs and dead-letters
    poison runs after WORKFLOW_RUN_MAX_ATTEMPTS
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from controlplane.models import AsyncAgentTask, WorkflowRun
from controlplane.services.orchestrator import orchestrator

logger = logging.getLogger(__name__)


def _max_run_attempts() -> int:
    return int(getattr(settings, "WORKFLOW_RUN_MAX_ATTEMPTS", 3))


class WorkflowQueueService:
    def enqueue(
        self,
        workflow,
        *,
        inputs: dict | None = None,
        triggered_by: str = "system",
        idempotency_key: str | None = None,
    ) -> WorkflowRun:
        """
        Create and dispatch a WorkflowRun.

        When ``idempotency_key`` is supplied, a duplicate enqueue (retried HTTP
        request, double-click, redelivered message) returns the existing run
        instead of creating a second execution.
        """
        if idempotency_key:
            existing = WorkflowRun.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing
        try:
            with transaction.atomic():
                run = orchestrator.start(
                    workflow, inputs=inputs or {}, triggered_by=triggered_by,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            # Lost a concurrent race on the unique key — the winner's run stands.
            existing = WorkflowRun.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing
            raise
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

    def try_claim_run(self, run_id) -> WorkflowRun | None:
        """
        Atomically claim a specific PENDING run (PENDING → RUNNING, attempts+1).

        Returns None when the run is missing, already claimed by another worker,
        or terminal — the caller must then skip execution.  This is the guard
        that makes Celery redelivery (acks_late) safe.
        """
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update()
                .filter(id=run_id, status=WorkflowRun.Status.PENDING)
                .first()
            )
            if run is None:
                return None
            run.status = WorkflowRun.Status.RUNNING
            run.attempts += 1
            run.save(update_fields=["status", "attempts", "updated_at"])
            return run

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
            run.attempts += 1
            run.save(update_fields=["status", "attempts", "updated_at"])
            return run

    def recover_stale_running_runs(self, *, stale_after_seconds: int = 900) -> int:
        """
        Recover RUNNING runs whose worker died.

        Staleness is judged on ``updated_at`` (touched by every wave merge), not
        ``started_at``, so a long-but-alive run is not falsely recovered.  A
        recovered run is requeued (PENDING) while attempts remain, and parked in
        DEAD_LETTER once ``WORKFLOW_RUN_MAX_ATTEMPTS`` is exhausted — a poison
        run can never loop forever.
        """
        cutoff = timezone.now() - timedelta(seconds=stale_after_seconds)
        max_attempts = _max_run_attempts()
        recovered_ids: list = []
        with transaction.atomic():
            stale = list(
                WorkflowRun.objects.select_for_update()
                .filter(
                    status=WorkflowRun.Status.RUNNING,
                    updated_at__lt=cutoff,
                    completed_at__isnull=True,
                )
            )
            for run in stale:
                if run.attempts >= max_attempts:
                    run.status = WorkflowRun.Status.DEAD_LETTER
                    run.error = (
                        f"Dead-lettered: {run.attempts} execution attempts each went "
                        f"stale (> {stale_after_seconds}s without progress). "
                        "Inspect and requeue manually via requeue_dead_letter()."
                    )
                    run.completed_at = timezone.now()
                    run.save(update_fields=["status", "error", "completed_at", "updated_at"])
                    logger.error("Workflow run %s dead-lettered after %s attempts", run.id, run.attempts)
                    from controlplane.services.alerts import send_alert

                    send_alert(
                        f"Workflow run dead-lettered: {run.id}",
                        run.error,
                        category="ops",
                    )
                else:
                    run.status = WorkflowRun.Status.PENDING
                    run.error = (
                        f"Requeued by stale-run recovery (attempt {run.attempts} stalled "
                        f"> {stale_after_seconds}s)."
                    )
                    run.save(update_fields=["status", "error", "updated_at"])
                    logger.warning("Workflow run %s requeued by stale recovery", run.id)
                    recovered_ids.append(run.id)
        # Redispatch outside the lock so a Celery worker can pick them up.
        for run_id in recovered_ids:
            run = WorkflowRun.objects.filter(id=run_id).first()
            if run is not None:
                self.dispatch_run(run)
        return len(stale)

    def recover_stale_working_tasks(self, *, stale_after_seconds: int = 900) -> int:
        """
        Recover AsyncAgentTasks stuck WORKING after a worker crash.

        Requeues (→ SUBMITTED + redispatch) while attempts remain, then parks the
        task in DEAD_LETTER.  Without this, a crashed worker left tasks WORKING
        forever and pollers hung indefinitely.
        """
        cutoff = timezone.now() - timedelta(seconds=stale_after_seconds)
        max_attempts = int(getattr(settings, "ASYNC_TASK_MAX_ATTEMPTS", 3))
        requeued: list = []
        count = 0
        with transaction.atomic():
            stale = list(
                AsyncAgentTask.objects.select_for_update()
                .filter(state=AsyncAgentTask.State.WORKING, updated_at__lt=cutoff)
            )
            for task in stale:
                count += 1
                if task.attempts >= max_attempts:
                    task.state = AsyncAgentTask.State.DEAD_LETTER
                    task.error = (
                        f"Dead-lettered: {task.attempts} execution attempts each went "
                        f"stale (> {stale_after_seconds}s in WORKING)."
                    )
                    task.completed_at = timezone.now()
                    task.save(update_fields=["state", "error", "completed_at", "updated_at"])
                    logger.error("Async task %s dead-lettered after %s attempts", task.id, task.attempts)
                    from controlplane.services.alerts import send_alert

                    send_alert(
                        f"Agent task dead-lettered: {task.id}",
                        task.error,
                        category="ops",
                    )
                else:
                    task.state = AsyncAgentTask.State.SUBMITTED
                    task.error = (
                        f"Requeued by stale-task recovery (attempt {task.attempts} stalled)."
                    )
                    task.save(update_fields=["state", "error", "updated_at"])
                    requeued.append(str(task.id))
        from controlplane.services.agent_tasks import _celery_enabled
        if _celery_enabled():
            from controlplane.tasks import execute_agent_task
            for task_id in requeued:
                execute_agent_task.delay(task_id)
        return count

    def list_dead_letters(self, *, limit: int = 50) -> dict:
        """Operator view of parked poison work (runs + tasks)."""
        runs = list(
            WorkflowRun.objects.filter(status=WorkflowRun.Status.DEAD_LETTER)
            .order_by("-updated_at")[:limit]
        )
        tasks = list(
            AsyncAgentTask.objects.filter(state=AsyncAgentTask.State.DEAD_LETTER)
            .order_by("-updated_at")[:limit]
        )
        return {"workflow_runs": runs, "agent_tasks": tasks}

    def requeue_dead_letter(self, run: WorkflowRun) -> WorkflowRun:
        """Operator action: give a dead-lettered run a fresh set of attempts."""
        if run.status != WorkflowRun.Status.DEAD_LETTER:
            raise ValueError("Only DEAD_LETTER runs can be requeued.")
        run.status = WorkflowRun.Status.PENDING
        run.attempts = 0
        run.error = ""
        run.completed_at = None
        run.save(update_fields=["status", "attempts", "error", "completed_at", "updated_at"])
        self.dispatch_run(run)
        return run

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
