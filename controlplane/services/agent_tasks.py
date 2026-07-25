"""
Async agent task service — Phase 0 durable execution.

The durable, pollable primitive behind Phase 1 A2A ``message/send``:

    task = agent_tasks.submit(agent, "summarise this ticket", submitted_by="a2a:acme")
    ...            # returns immediately; a worker runs the agent
    task.refresh_from_db()
    task.output_text            # once task.state == "completed"

Execution ALWAYS goes through ``PlatformAgentRuntime.stream`` — the same path as
any interactive run — so guardrails, telemetry, pricing, the OTel span and the
AgentRun record all happen with no bypass.  This service only owns *where* that
run executes:

  EXECUTION_BACKEND == "celery"  → dispatch to a Celery worker (async, durable)
  EXECUTION_BACKEND == "db"      → run inline, synchronously (no broker needed)

Either way the AsyncAgentTask row is the source of truth for state and result.
"""
from __future__ import annotations

import logging

from django.conf import settings

from controlplane.services.orchestrator import OrchestratorError, _parse_sse

logger = logging.getLogger(__name__)


def _celery_enabled() -> bool:
    """True only when the backend is explicitly celery AND Celery is importable."""
    if getattr(settings, "EXECUTION_BACKEND", "db") != "celery":
        return False
    try:
        import celery  # noqa: F401
        return True
    except ImportError:
        logger.warning("EXECUTION_BACKEND=celery but Celery is not installed; running inline.")
        return False


def run_agent_task_inline(task_id: str) -> str:
    """
    Execute one AsyncAgentTask to completion in the current process.

    Shared by the Celery task and the synchronous fallback.  Never raises for an
    agent/guardrail failure — the failure is recorded on the task and the
    terminal state string is returned. Returns the final state value.
    """
    from django.db import transaction

    from controlplane.models import AgentRun, AsyncAgentTask
    from controlplane.services.agent_runtime import PlatformAgentRuntime

    # Atomic claim: SUBMITTED → WORKING under a row lock. With acks_late a
    # redelivered Celery message would otherwise let two workers both pass the
    # is_terminal gate and run the agent twice (double LLM spend/side-effects).
    with transaction.atomic():
        task = (
            AsyncAgentTask.objects.select_for_update()
            .select_related("agent")
            .filter(id=task_id)
            .first()
        )
        if task is None:
            logger.warning("run_agent_task_inline: task %s not found", task_id)
            return "missing"
        if task.state != AsyncAgentTask.State.SUBMITTED:
            logger.info(
                "run_agent_task_inline: task %s already %s — skipping duplicate delivery",
                task_id, task.state,
            )
            return task.state
        task.attempts += 1
        task.save(update_fields=["attempts", "updated_at"])
        task.mark_working()

    runtime = PlatformAgentRuntime(
        agent=task.agent,
        user_label=task.submitted_by or "a2a",
        channel=task.channel or "a2a",
    )

    output_parts: list[str] = []
    run_id: str | None = None
    try:
        for block in runtime.stream(task.input_message):
            event, payload = _parse_sse(block)
            if event is None:
                continue
            if event in ("status", "done"):
                run_id = payload.get("run_id") or run_id
            elif event == "token":
                output_parts.append(payload.get("text", ""))
            elif event == "error":
                raise OrchestratorError(payload.get("message", "Agent run failed."))
    except Exception as exc:  # noqa: BLE001 — record, never crash the worker
        logger.warning("Async agent task %s failed: %s", task_id, exc)
        agent_run = _lookup_run(AgentRun, run_id)
        if agent_run is not None:
            task.run = agent_run
        task.mark_failed(str(exc))
        return task.state

    output_text = "".join(output_parts)
    agent_run = _lookup_run(AgentRun, run_id)
    if agent_run is not None and not output_text:
        output_text = agent_run.output_text
    task.mark_completed(output_text, run=agent_run)
    return task.state


def _lookup_run(AgentRun, run_id):
    if not run_id:
        return None
    try:
        return AgentRun.objects.get(id=run_id)
    except AgentRun.DoesNotExist:
        return None


class AgentTaskService:
    """Submit and retrieve durable asynchronous agent invocations."""

    def submit(
        self,
        agent,
        message: str,
        *,
        submitted_by: str = "system",
        context_id: str = "",
        channel: str = "a2a",
        idempotency_key: str | None = None,
    ):
        """
        Create an AsyncAgentTask and dispatch it for execution.

        Returns the AsyncAgentTask immediately.  Under the celery backend the row
        is SUBMITTED and a worker will run it; under the db backend it is executed
        inline before returning (so its state is already terminal).

        When ``idempotency_key`` is supplied, a duplicate submit returns the
        existing task instead of executing the agent twice.
        """
        from django.db import IntegrityError

        from controlplane.models import AsyncAgentTask

        if idempotency_key:
            existing = AsyncAgentTask.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing
        try:
            task = AsyncAgentTask.objects.create(
                agent=agent,
                input_message=message,
                submitted_by=submitted_by,
                context_id=context_id,
                channel=channel,
                idempotency_key=idempotency_key or None,
            )
        except IntegrityError:
            # Lost a concurrent race on the unique key — return the winner's task.
            existing = AsyncAgentTask.objects.filter(idempotency_key=idempotency_key).first()
            if existing is not None:
                return existing
            raise

        if _celery_enabled():
            from controlplane.tasks import execute_agent_task
            execute_agent_task.delay(str(task.id))
        else:
            # No broker: run synchronously in-process. The row still records state.
            run_agent_task_inline(str(task.id))
            task.refresh_from_db()

        return task

    def get(self, task_id):
        from controlplane.models import AsyncAgentTask
        return AsyncAgentTask.objects.filter(id=task_id).first()


agent_tasks = AgentTaskService()
