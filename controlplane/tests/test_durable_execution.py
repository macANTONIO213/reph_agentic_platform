"""
Phase 0 tests — durable execution (Celery adapter + AsyncAgentTask primitive).

These exercise the *routing and durability* seam, not real LLM calls: the agent
runtime's ``stream`` is mocked to yield deterministic SSE, so the tests are fast
and offline.  Celery runs in eager mode during tests (settings sets
CELERY_TASK_ALWAYS_EAGER when "test" is in argv), so ``.delay()`` executes inline
and the celery-backend paths are covered without a Redis broker.
"""
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from controlplane.models import Agent, AsyncAgentTask, BusinessUnit, Workflow, WorkflowRun


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_agent(slug="task-agent"):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        purpose="Test agent",
        business_unit="Engineering",
        risk_tier=1,
        status=Agent.Status.PRODUCTION,
        platform="django_runtime",
    )


def _make_workflow(slug="wf-durable"):
    wf, _ = Workflow.objects.get_or_create(
        slug=slug,
        defaults={
            "name": slug.replace("-", " ").title(),
            "status": Workflow.Status.ACTIVE,
            "owner": "tester",
        },
    )
    return wf


def _fake_stream(output="hello world", run_id=None, error=None):
    """Return a stream() replacement yielding RuntimeEvent-shaped SSE blocks."""
    def stream(self, message, session=None):
        if run_id:
            yield f"event: status\ndata: {json.dumps({'run_id': run_id})}\n\n"
        if error is not None:
            yield f"event: error\ndata: {json.dumps({'message': error})}\n\n"
            return
        yield f"event: token\ndata: {json.dumps({'text': output})}\n\n"
        yield f"event: done\ndata: {json.dumps({'run_id': run_id})}\n\n"
    return stream


def _patch_stream(**kwargs):
    return patch(
        "controlplane.services.agent_runtime.PlatformAgentRuntime.stream",
        new=_fake_stream(**kwargs),
    )


# ── AsyncAgentTask model ────────────────────────────────────────────────────────

class AsyncAgentTaskModelTests(TestCase):
    def setUp(self):
        self.agent = _make_agent()

    def test_state_helpers(self):
        task = AsyncAgentTask.objects.create(agent=self.agent, input_message="hi")
        self.assertEqual(task.state, AsyncAgentTask.State.SUBMITTED)
        self.assertFalse(task.is_terminal)

        task.mark_working()
        self.assertEqual(task.state, AsyncAgentTask.State.WORKING)
        self.assertIsNotNone(task.started_at)

        task.mark_completed("done")
        self.assertEqual(task.state, AsyncAgentTask.State.COMPLETED)
        self.assertEqual(task.output_text, "done")
        self.assertTrue(task.is_terminal)
        self.assertIsNotNone(task.completed_at)

    def test_mark_failed_truncates(self):
        task = AsyncAgentTask.objects.create(agent=self.agent, input_message="hi")
        task.mark_failed("boom" * 2000)
        self.assertEqual(task.state, AsyncAgentTask.State.FAILED)
        self.assertLessEqual(len(task.error), 4000)


# ── AgentTaskService (db backend, synchronous fallback) ─────────────────────────

class AgentTaskServiceTests(TestCase):
    def setUp(self):
        self.agent = _make_agent()

    def test_submit_runs_inline_and_completes(self):
        from controlplane.services.agent_tasks import agent_tasks
        with _patch_stream(output="the answer"):
            task = agent_tasks.submit(self.agent, "question?", submitted_by="a2a:acme")
        # db backend executes inline, so the returned task is already terminal.
        self.assertEqual(task.state, AsyncAgentTask.State.COMPLETED)
        self.assertEqual(task.output_text, "the answer")
        self.assertEqual(task.submitted_by, "a2a:acme")

    def test_submit_records_failure(self):
        from controlplane.services.agent_tasks import agent_tasks
        with _patch_stream(error="guardrail blocked"):
            task = agent_tasks.submit(self.agent, "bad prompt")
        self.assertEqual(task.state, AsyncAgentTask.State.FAILED)
        self.assertIn("guardrail blocked", task.error)

    def test_terminal_task_not_reexecuted(self):
        from controlplane.services.agent_tasks import run_agent_task_inline
        task = AsyncAgentTask.objects.create(agent=self.agent, input_message="hi")
        task.mark_completed("already done")
        with _patch_stream(output="SHOULD NOT RUN"):
            state = run_agent_task_inline(str(task.id))
        task.refresh_from_db()
        self.assertEqual(state, AsyncAgentTask.State.COMPLETED)
        self.assertEqual(task.output_text, "already done")

    def test_get_returns_task(self):
        from controlplane.services.agent_tasks import agent_tasks
        with _patch_stream():
            task = agent_tasks.submit(self.agent, "hi")
        self.assertEqual(agent_tasks.get(task.id).id, task.id)
        self.assertIsNone(agent_tasks.get("00000000-0000-0000-0000-000000000000"))


# ── Backend routing ─────────────────────────────────────────────────────────────

class ExecutionBackendRoutingTests(TestCase):
    def setUp(self):
        self.agent = _make_agent()
        self.wf = _make_workflow()

    @override_settings(EXECUTION_BACKEND="db")
    def test_celery_disabled_under_db_backend(self):
        from controlplane.services.agent_tasks import _celery_enabled
        self.assertFalse(_celery_enabled())

    @override_settings(EXECUTION_BACKEND="celery")
    def test_celery_enabled_when_configured(self):
        from controlplane.services.agent_tasks import _celery_enabled
        self.assertTrue(_celery_enabled())  # celery is installed in this env

    @override_settings(EXECUTION_BACKEND="db")
    def test_enqueue_stays_pending_under_db(self):
        from controlplane.services.workflow_queue import workflow_queue
        with patch("controlplane.services.orchestrator.OrchestratorService.execute") as ex:
            run = workflow_queue.enqueue(self.wf)
        ex.assert_not_called()
        self.assertEqual(run.status, WorkflowRun.Status.PENDING)

    @override_settings(EXECUTION_BACKEND="celery")
    def test_enqueue_dispatches_to_worker_under_celery(self):
        from controlplane.services.workflow_queue import workflow_queue
        with patch("controlplane.services.orchestrator.OrchestratorService.execute") as ex:
            workflow_queue.enqueue(self.wf)
        # eager mode → the dispatched task runs execute() inline.
        ex.assert_called_once()

    @override_settings(EXECUTION_BACKEND="celery")
    def test_submit_dispatches_under_celery(self):
        from controlplane.services.agent_tasks import agent_tasks
        with _patch_stream(output="via celery"):
            task = agent_tasks.submit(self.agent, "hi")
        task.refresh_from_db()
        self.assertEqual(task.state, AsyncAgentTask.State.COMPLETED)
        self.assertEqual(task.output_text, "via celery")


# ── Celery app wiring ───────────────────────────────────────────────────────────

class CeleryAppTests(TestCase):
    def test_ping_task_runs_eagerly(self):
        from agentic_platform.celery import ping
        self.assertEqual(ping.delay().get(timeout=5), "pong")

    def test_tasks_registered(self):
        from controlplane import tasks
        self.assertEqual(tasks.execute_workflow_run.name, "controlplane.execute_workflow_run")
        self.assertEqual(tasks.execute_agent_task.name, "controlplane.execute_agent_task")
