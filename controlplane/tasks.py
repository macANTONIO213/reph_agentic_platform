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
