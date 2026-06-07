"""
OrchestratorService — Phase E Multi-Agent Workflow Execution

Executes a WorkflowRun by walking the task DAG and dispatching each
step to the correct agent via the existing agent runtime.

DAG execution algorithm:
  1. Build adjacency map: step_name → WorkflowTask
  2. Repeat until no pending tasks remain:
     a. Find all tasks whose dependencies are fully completed
     b. Execute each ready task (sequentially for now — parallel in Phase E+)
     c. Store outputs in WorkflowRun.outputs[step_name]
  3. If a task fails and retry_limit > 0, retry up to retry_limit times
  4. Mark WorkflowRun as completed or failed

Template substitution:
  input_template tokens: {{inputs.key}}, {{outputs.STEP.key}}
  Resolved before the agent is invoked.

Usage::
    from controlplane.services.orchestrator import orchestrator

    run = orchestrator.start(workflow, inputs={"topic": "Q2 revenue"}, triggered_by="user:bob")
    result = orchestrator.execute(run)   # synchronous; returns WorkflowRun
"""
from __future__ import annotations

import json as _json
import logging
import re
import time
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

# Simple {{token}} substitution regex
_TOKEN_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _parse_sse(block: str) -> tuple[str | None, dict]:
    """
    Parse a single SSE block produced by RuntimeEvent.to_sse(), which has the
    shape ``"event: <name>\\ndata: <json>\\n\\n"``.

    Returns (event_name, data_dict). Returns (None, {}) if the block has no
    recognisable event line or its data payload is not valid JSON.
    """
    event: str | None = None
    data_str: str | None = None
    for line in block.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_str = line[len("data:"):].strip()
    if event is None or data_str is None:
        return None, {}
    try:
        payload = _json.loads(data_str)
    except (ValueError, TypeError):
        return None, {}
    return event, (payload if isinstance(payload, dict) else {})


def _resolve_template(template: str, context: dict) -> str:
    """
    Replace {{inputs.key}} and {{outputs.step.key}} tokens.
    Missing tokens are left as-is (not raised) so callers can inspect.
    """
    def _lookup(match):
        path = match.group(1)  # e.g. "outputs.step_a.summary"
        parts = path.split(".")
        node = context
        try:
            for part in parts:
                node = node[part]
            return str(node) if not isinstance(node, dict) else str(node)
        except (KeyError, TypeError):
            return match.group(0)  # leave unreplaced

    return _TOKEN_RE.sub(_lookup, template)


class OrchestratorError(Exception):
    pass


class OrchestratorService:
    """
    Synchronous workflow executor.

    For production workloads this could be driven by Celery or Django-Q,
    but the core algorithm is transport-agnostic — the caller decides
    whether to run execute() in a thread, task queue, or directly.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, workflow, *, inputs: dict | None = None, triggered_by: str = "system"):
        """
        Create a WorkflowRun for the given workflow.
        Does NOT execute it — call execute() separately.
        """
        from controlplane.models import WorkflowRun
        run = WorkflowRun.objects.create(
            workflow=workflow,
            status=WorkflowRun.Status.PENDING,
            triggered_by=triggered_by,
            inputs=inputs or {},
            outputs={},
        )
        logger.info("Workflow run %s created for '%s'", run.id, workflow.name)
        return run

    def execute(self, workflow_run):
        """
        Execute a pending WorkflowRun synchronously.
        Returns the WorkflowRun with final status set.
        """
        from controlplane.models import WorkflowRun

        workflow_run.status = WorkflowRun.Status.RUNNING
        workflow_run.save(update_fields=["status"])

        try:
            tasks = list(workflow_run.workflow.tasks.select_related("agent").all())
            if not tasks:
                raise OrchestratorError("Workflow has no tasks.")

            # Build step → task map
            task_map = {t.step_name: t for t in tasks}
            completed_steps: set[str] = set()
            failed_steps: set[str] = set()

            # Topological execution loop (max iterations = len(tasks) + 1 to guard loops)
            max_iters = len(tasks) + 1
            pending = set(task_map.keys())

            for _ in range(max_iters):
                if not pending:
                    break

                # Find ready tasks (all deps complete)
                ready = [
                    task_map[s] for s in pending
                    if all(d in completed_steps for d in (task_map[s].depends_on or []))
                    and not any(d in failed_steps for d in (task_map[s].depends_on or []))
                ]

                if not ready:
                    # Some tasks have failed deps — skip them
                    skippable = {
                        s for s in pending
                        if any(d in failed_steps for d in (task_map[s].depends_on or []))
                    }
                    if skippable:
                        for s in skippable:
                            self._skip_task(workflow_run, task_map[s])
                            pending.discard(s)
                        continue
                    # Circular dependency or no progress
                    remaining = list(pending)
                    raise OrchestratorError(
                        f"Workflow stalled — possible circular dependency among: {remaining}"
                    )

                for task in ready:
                    pending.discard(task.step_name)
                    success = self._execute_task(workflow_run, task)
                    if success:
                        completed_steps.add(task.step_name)
                    else:
                        failed_steps.add(task.step_name)

            # Determine final status
            if failed_steps:
                workflow_run.status = WorkflowRun.Status.FAILED
                workflow_run.error = f"Failed steps: {sorted(failed_steps)}"
            else:
                workflow_run.status = WorkflowRun.Status.COMPLETED

        except OrchestratorError as exc:
            workflow_run.status = WorkflowRun.Status.FAILED
            workflow_run.error = str(exc)
            logger.error("Workflow run %s failed: %s", workflow_run.id, exc)
        except Exception as exc:
            workflow_run.status = WorkflowRun.Status.FAILED
            workflow_run.error = f"Unexpected error: {exc}"
            logger.exception("Workflow run %s unexpected error", workflow_run.id)
        finally:
            workflow_run.completed_at = timezone.now()
            workflow_run.save(update_fields=["status", "error", "outputs", "completed_at"])

        logger.info(
            "Workflow run %s finished: %s (%s ms)",
            workflow_run.id, workflow_run.status,
            workflow_run.duration_ms,
        )
        return workflow_run

    # ── Task execution ────────────────────────────────────────────────────────

    def _execute_task(self, workflow_run, task, attempt: int = 1) -> bool:
        """Execute a single task.  Returns True on success, False on failure."""
        from controlplane.models import WorkflowRun, WorkflowTaskRun
        from controlplane.services.model_router import model_router

        # Build context for template substitution
        context = {"inputs": workflow_run.inputs, "outputs": workflow_run.outputs}
        resolved_input = _resolve_template(task.input_template or "", context)

        task_run = WorkflowTaskRun.objects.create(
            workflow_run=workflow_run,
            task=task,
            status=WorkflowTaskRun.Status.RUNNING,
            attempt=attempt,
            resolved_input=resolved_input,
        )

        agent = task.agent
        if agent is None:
            # No agent assigned — skip with a warning
            task_run.status = WorkflowTaskRun.Status.SKIPPED
            task_run.error = "No agent assigned to this task."
            task_run.completed_at = timezone.now()
            task_run.save(update_fields=["status", "error", "completed_at"])
            logger.warning("Task '%s' has no agent — skipping.", task.step_name)
            return True  # Treat skipped as non-blocking

        # Apply model routing
        routed_model = model_router.select(agent, task=task)
        original_model = agent.model_id

        try:
            # Temporarily override model for this invocation (thread-local copy)
            # We don't mutate the DB — the adapter reads agent.model_id at call time
            agent.model_id = routed_model or original_model

            output_text, agent_run = self._invoke_agent(agent, resolved_input, workflow_run)

            # Extract structured output (JSON if possible, else raw text)
            parsed_output = self._parse_output(output_text)

            task_run.status = WorkflowTaskRun.Status.COMPLETED
            task_run.raw_output = output_text
            task_run.output = parsed_output
            task_run.agent_run = agent_run
            task_run.completed_at = timezone.now()
            task_run.save(update_fields=[
                "status", "raw_output", "output", "agent_run", "completed_at"
            ])

            # Store in workflow_run.outputs for downstream template substitution
            workflow_run.outputs[task.step_name] = parsed_output
            workflow_run.save(update_fields=["outputs"])

            logger.info("Task '%s' completed in workflow run %s", task.step_name, workflow_run.id)
            return True

        except Exception as exc:
            logger.warning(
                "Task '%s' attempt %s failed: %s", task.step_name, attempt, exc
            )
            # Retry if within limit
            if attempt <= task.retry_limit:
                logger.info("Retrying task '%s' (attempt %s/%s)", task.step_name, attempt + 1, task.retry_limit + 1)
                task_run.status = WorkflowTaskRun.Status.FAILED
                task_run.error = str(exc)
                task_run.completed_at = timezone.now()
                task_run.save(update_fields=["status", "error", "completed_at"])
                return self._execute_task(workflow_run, task, attempt=attempt + 1)

            task_run.status = WorkflowTaskRun.Status.FAILED
            task_run.error = str(exc)
            task_run.completed_at = timezone.now()
            task_run.save(update_fields=["status", "error", "completed_at"])
            return False

        finally:
            agent.model_id = original_model  # Restore

    def _skip_task(self, workflow_run, task):
        from controlplane.models import WorkflowTaskRun
        WorkflowTaskRun.objects.create(
            workflow_run=workflow_run,
            task=task,
            status=WorkflowTaskRun.Status.SKIPPED,
            error="Upstream dependency failed.",
            completed_at=timezone.now(),
        )
        logger.info("Task '%s' skipped (upstream failure).", task.step_name)

    def _invoke_agent(self, agent, message: str, workflow_run):
        """
        Invoke an agent synchronously.  Collects SSE events from the streaming
        runtime and returns the final output text + AgentRun instance.
        """
        from controlplane.services.agent_runtime import PlatformAgentRuntime
        from controlplane.models import AgentRun

        runtime = PlatformAgentRuntime(
            agent=agent,
            user_label=f"workflow:{workflow_run.id}",
            channel="workflow",
        )

        output_parts: list[str] = []
        run_id: str | None = None

        for block in runtime.stream(message):
            event, payload = _parse_sse(block)
            if event is None:
                continue
            if event == "status":
                run_id = payload.get("run_id") or run_id
            elif event == "token":
                output_parts.append(payload.get("text", ""))
            elif event == "done":
                run_id = payload.get("run_id") or run_id
            elif event == "error":
                raise OrchestratorError(payload.get("message", "Agent run failed."))

        output_text = "".join(output_parts)
        agent_run = None
        if run_id:
            try:
                agent_run = AgentRun.objects.get(id=run_id)
                output_text = output_text or agent_run.output_text
            except AgentRun.DoesNotExist:
                pass

        return output_text, agent_run

    @staticmethod
    def _parse_output(text: str) -> dict:
        """
        Try to parse the agent output as JSON.
        If it fails, wrap the raw text under the key 'text'.
        """
        import json
        stripped = text.strip()
        # Find JSON object in response (agent may have surrounding prose)
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(stripped[start:end + 1])
            except Exception:
                pass
        return {"text": stripped}


# ── Delegation tool helper ────────────────────────────────────────────────────

def delegate_to_agent(
    *,
    agent_slug: str,
    message: str,
    workflow_run=None,
    caller_label: str = "agent:delegator",
) -> dict:
    """
    Invoke another agent by slug and return its output.
    Used as the implementation of the delegate_to_agent built-in tool.
    """
    from controlplane.models import Agent
    try:
        target = Agent.objects.get(slug=agent_slug)
    except Agent.DoesNotExist:
        return {"error": f"Agent '{agent_slug}' not found in the registry."}

    from controlplane.services.agent_runtime import PlatformAgentRuntime

    runtime = PlatformAgentRuntime(agent=target, user_label=caller_label, channel="delegation")
    output_parts: list[str] = []
    run_id: str | None = None

    for block in runtime.stream(message):
        event, payload = _parse_sse(block)
        if event is None:
            continue
        if event == "token":
            output_parts.append(payload.get("text", ""))
        elif event in ("status", "done"):
            run_id = payload.get("run_id") or run_id

    return {
        "agent_slug": agent_slug,
        "run_id": run_id,
        "output": "".join(output_parts),
    }


# Module-level singleton
orchestrator = OrchestratorService()
