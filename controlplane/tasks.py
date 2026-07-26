"""
Celery tasks — Phase 0 durable execution.

Thin wrappers only: the real work lives in the orchestrator and the
``agent_tasks`` service so the synchronous / DB-backed fallback paths run exactly
the same code.  These tasks are discovered by the Celery app
(``agentic_platform/celery.py``) and are only *dispatched* when
``settings.EXECUTION_BACKEND == "celery"``.

Models are imported lazily inside each task to keep module import (which Celery
does at worker startup via autodiscovery) free of Django-app side effects.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="controlplane.execute_workflow_run",
    bind=True,
    acks_late=True,
    max_retries=0,
)
def execute_workflow_run(self, run_id: str) -> dict:
    """
    Execute a queued WorkflowRun by id.

    Idempotent: the run is claimed atomically (PENDING → RUNNING under a row
    lock), so a redelivered message (acks_late) or a concurrent DB worker
    finds it already claimed and skips — one run executes exactly once.
    """
    from controlplane.services.orchestrator import orchestrator
    from controlplane.services.workflow_queue import workflow_queue

    run = workflow_queue.try_claim_run(run_id)
    if run is None:
        logger.info("execute_workflow_run: run %s already claimed/terminal or missing — skipping", run_id)
        return {"run_id": run_id, "status": "skipped", "skipped": True}

    orchestrator.execute(run)
    run.refresh_from_db()
    return {"run_id": run_id, "status": run.status}


@shared_task(name="controlplane.maintenance", bind=True, max_retries=0)
def maintenance(self, job: str) -> dict:
    """
    Scheduled operational jobs (OE-1) — dispatched by Celery beat
    (``CELERY_BEAT_SCHEDULE``) so retention/budgets/baselines/recovery no longer
    depend on external cron. Each job reuses the existing command/service code.
    """
    from django.core.management import call_command

    if job == "recover_stale":
        from controlplane.services.workflow_queue import workflow_queue

        recovered = workflow_queue.recover_stale_running_runs()
        stale_tasks = workflow_queue.recover_stale_working_tasks()
        return {"job": job, "runs": recovered, "tasks": stale_tasks}
    if job == "purge_memory":
        from controlplane.services.memory import memory_service

        return {"job": job, "purged": memory_service.purge_expired()}
    if job in ("compute_budgets", "compute_baselines", "enforce_retention", "export_spans"):
        call_command(job)
        return {"job": job, "status": "ok"}
    if job == "dispatch_scheduled":
        from controlplane.services.operations import dispatch_scheduled_workflows

        return {"job": job, "dispatched": dispatch_scheduled_workflows()}
    if job == "check_slos":
        from controlplane.services.operations import check_slos

        return {"job": job, "breaches": check_slos()}
    if job == "send_digest":
        from controlplane.services.operations import send_digest

        return {"job": job, **send_digest()}
    if job == "export_evidence":
        from controlplane.services.operations import export_evidence

        return {"job": job, **export_evidence()}
    if job == "factory_feedback":
        from controlplane.services.operations import factory_feedback

        return {"job": job, "blueprints_updated": factory_feedback()}
    if job == "escalate_stale":
        from controlplane.services.operations import escalate_stale_items

        return {"job": job, "escalated": escalate_stale_items()}
    logger.warning("maintenance: unknown job %r", job)
    return {"job": job, "status": "unknown"}


@shared_task(
    name="controlplane.execute_agent_task",
    bind=True,
    acks_late=True,
    max_retries=0,
)
def execute_agent_task(self, task_id: str) -> dict:
    """Execute a submitted AsyncAgentTask by id (the A2A invocation primitive)."""
    from controlplane.services.agent_tasks import run_agent_task_inline

    result = run_agent_task_inline(task_id)
    return {"task_id": task_id, "state": result}
