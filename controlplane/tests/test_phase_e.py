"""
Phase E tests — Multi-Agent Orchestration
Covers: ModelRouter, MemoryService, OrchestratorService (DAG execution),
        template substitution, API endpoints.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentRun,
    BusinessUnit,
    SharedMemory,
    Workflow,
    WorkflowRun,
    WorkflowTask,
    WorkflowTaskRun,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bu(name="Engineering"):
    bu, _ = BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:4].upper()})
    return bu


def _make_agent(slug="agent-a", bu=None, risk_tier=1, platform="django_runtime"):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        purpose="Test agent",
        business_unit=(bu.name if bu else "Engineering"),
        risk_tier=risk_tier,
        status=Agent.Status.PRODUCTION,
        platform=platform,
        org_unit=bu,
    )


def _make_workflow(slug="wf-1", bu=None):
    wf, _ = Workflow.objects.get_or_create(
        slug=slug,
        defaults={
            "name": slug.replace("-", " ").title(),
            "business_unit": bu,
            "status": Workflow.Status.ACTIVE,
            "owner": "tester",
        },
    )
    return wf


def _make_user(username="testuser", staff=True):
    u, _ = User.objects.get_or_create(username=username, defaults={"is_staff": staff})
    return u


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouterTests(TestCase):

    def setUp(self):
        self.bu = _make_bu()

    def test_explicit_agent_override_wins(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(risk_tier=1)
        agent.model_id = "claude-haiku-4-5"
        result = model_router.select(agent)
        self.assertEqual(result, "claude-haiku-4-5")

    def test_step_override_beats_agent_override(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(risk_tier=1)
        agent.model_id = "claude-haiku-4-5"
        wf = _make_workflow(bu=self.bu)
        task = WorkflowTask(
            workflow=wf, step_name="step1", agent=agent,
            model_override="claude-opus-4-8", depends_on=[], order=0
        )
        result = model_router.select(agent, task=task)
        self.assertEqual(result, "claude-opus-4-8")

    def test_tier4_routes_to_opus(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(slug="tier4-agent", risk_tier=4)
        agent.model_id = ""
        result = model_router.select(agent)
        self.assertEqual(result, "claude-opus-4-8")

    def test_budget_alert_routes_to_haiku(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(slug="budget-agent", risk_tier=3)
        agent.model_id = ""
        agent.budget_alert = True
        result = model_router.select(agent)
        self.assertEqual(result, "claude-haiku-4-5")

    def test_fast_timeout_routes_to_haiku(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(slug="fast-agent", risk_tier=2)
        agent.model_id = ""
        agent.budget_alert = False
        wf = _make_workflow(slug="wf-fast", bu=self.bu)
        task = WorkflowTask(
            workflow=wf, step_name="step1", agent=agent,
            model_override="", depends_on=[], timeout_seconds=20
        )
        result = model_router.select(agent, task=task)
        self.assertEqual(result, "claude-haiku-4-5")

    def test_explain_returns_dict(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(slug="explain-agent", risk_tier=2)
        agent.model_id = ""
        result = model_router.explain(agent)
        self.assertIn("model_id", result)
        self.assertIn("risk_tier", result)
        self.assertIn("budget_alert", result)

    def test_openai_platform_tier4_routes_gpt4o(self):
        from controlplane.services.model_router import model_router
        agent = _make_agent(slug="openai-agent", risk_tier=4, platform="azure_ai_foundry")
        agent.model_id = ""
        result = model_router.select(agent)
        self.assertEqual(result, "gpt-4o")


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------

class MemoryServiceTests(TestCase):

    def setUp(self):
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)
        self.wf = _make_workflow(bu=self.bu)
        self.run = WorkflowRun.objects.create(
            workflow=self.wf,
            status=WorkflowRun.Status.RUNNING,
            triggered_by="tester",
        )

    def test_write_and_read_workflow_scope(self):
        from controlplane.services.memory import memory_service
        memory_service.write(key="summary", value={"text": "Q2 revenue up 10%"}, workflow_run=self.run)
        val = memory_service.read(key="summary", workflow_run=self.run)
        self.assertEqual(val["text"], "Q2 revenue up 10%")

    def test_read_returns_default_for_missing_key(self):
        from controlplane.services.memory import memory_service
        val = memory_service.read(key="nonexistent", workflow_run=self.run, default="fallback")
        self.assertEqual(val, "fallback")

    def test_write_and_read_agent_scope(self):
        from controlplane.services.memory import memory_service
        memory_service.write(key="config", value={"threshold": 0.8}, agent=self.agent)
        val = memory_service.read(key="config", agent=self.agent)
        self.assertEqual(val["threshold"], 0.8)

    def test_list_keys(self):
        from controlplane.services.memory import memory_service
        memory_service.write(key="key1", value="v1", workflow_run=self.run)
        memory_service.write(key="key2", value="v2", workflow_run=self.run)
        keys = memory_service.list_keys(workflow_run=self.run)
        self.assertIn("key1", keys)
        self.assertIn("key2", keys)

    def test_delete_removes_entry(self):
        from controlplane.services.memory import memory_service
        memory_service.write(key="temp", value="delete_me", workflow_run=self.run)
        memory_service.delete(key="temp", workflow_run=self.run)
        val = memory_service.read(key="temp", workflow_run=self.run)
        self.assertIsNone(val)

    def test_expired_entries_not_returned(self):
        from controlplane.services.memory import memory_service
        from datetime import timedelta
        memory_service.write(key="expired", value="old", workflow_run=self.run, ttl_seconds=1)
        # Manually expire it
        entry = SharedMemory.objects.get(workflow_run=self.run, key="expired")
        entry.expires_at = timezone.now() - timedelta(seconds=10)
        entry.save(update_fields=["expires_at"])
        val = memory_service.read(key="expired", workflow_run=self.run)
        self.assertIsNone(val)

    def test_write_requires_scope(self):
        from controlplane.services.memory import memory_service
        with self.assertRaises(ValueError):
            memory_service.write(key="k", value="v")  # no workflow_run or agent


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------

class TemplateSubstitutionTests(TestCase):

    def test_basic_input_substitution(self):
        from controlplane.services.orchestrator import _resolve_template
        result = _resolve_template("Analyse {{inputs.topic}}", {"inputs": {"topic": "revenue"}})
        self.assertEqual(result, "Analyse revenue")

    def test_output_substitution(self):
        from controlplane.services.orchestrator import _resolve_template
        ctx = {"inputs": {}, "outputs": {"step_a": {"summary": "15% growth"}}}
        result = _resolve_template("Based on: {{outputs.step_a.summary}}", ctx)
        self.assertEqual(result, "Based on: 15% growth")

    def test_missing_token_left_as_is(self):
        from controlplane.services.orchestrator import _resolve_template
        result = _resolve_template("{{inputs.missing}}", {"inputs": {}})
        self.assertEqual(result, "{{inputs.missing}}")

    def test_multiple_tokens(self):
        from controlplane.services.orchestrator import _resolve_template
        ctx = {"inputs": {"name": "Alice"}, "outputs": {"step1": {"score": "90"}}}
        result = _resolve_template("{{inputs.name}} scored {{outputs.step1.score}}", ctx)
        self.assertEqual(result, "Alice scored 90")


# ---------------------------------------------------------------------------
# OrchestratorService — DAG execution
# ---------------------------------------------------------------------------

class OrchestratorTests(TestCase):

    def setUp(self):
        self.bu = _make_bu("Analytics")
        self.agent_a = _make_agent("orch-agent-a", bu=self.bu)
        self.agent_b = _make_agent("orch-agent-b", bu=self.bu)
        self.wf = _make_workflow("orch-wf", bu=self.bu)

    def _add_task(self, step_name, agent, depends_on=None, order=0):
        return WorkflowTask.objects.create(
            workflow=self.wf,
            step_name=step_name,
            agent=agent,
            depends_on=depends_on or [],
            input_template=f"Run task {step_name}",
            order=order,
        )

    def _mock_invoke(self, output="result text"):
        """Patch _invoke_agent to return (text, None) — no real AgentRun needed."""
        return patch(
            "controlplane.services.orchestrator.OrchestratorService._invoke_agent",
            return_value=(output, None),
        )

    def test_single_task_workflow_completes(self):
        from controlplane.services.orchestrator import orchestrator
        self._add_task("step1", self.agent_a)
        with self._mock_invoke("Done"):
            run = orchestrator.start(self.wf, inputs={})
            result = orchestrator.execute(run)
        self.assertEqual(result.status, WorkflowRun.Status.COMPLETED)

    def test_two_step_sequential_workflow(self):
        from controlplane.services.orchestrator import orchestrator
        self._add_task("step1", self.agent_a, depends_on=[], order=0)
        self._add_task("step2", self.agent_b, depends_on=["step1"], order=1)
        with self._mock_invoke("output"):
            run = orchestrator.start(self.wf, inputs={})
            result = orchestrator.execute(run)
        self.assertEqual(result.status, WorkflowRun.Status.COMPLETED)
        task_runs = WorkflowTaskRun.objects.filter(workflow_run=result)
        self.assertEqual(task_runs.count(), 2)
        statuses = set(task_runs.values_list("status", flat=True))
        self.assertEqual(statuses, {WorkflowTaskRun.Status.COMPLETED})

    def test_outputs_accumulated(self):
        from controlplane.services.orchestrator import orchestrator
        self._add_task("extract", self.agent_a, order=0)
        with self._mock_invoke('{"revenue": "15%"}'):
            run = orchestrator.start(self.wf, inputs={"period": "Q2"})
            result = orchestrator.execute(run)
        self.assertIn("extract", result.outputs)

    def test_failed_task_marks_run_failed(self):
        from controlplane.services.orchestrator import orchestrator
        self._add_task("failing_step", self.agent_a)
        with patch(
            "controlplane.services.orchestrator.OrchestratorService._invoke_agent",
            side_effect=Exception("LLM error"),
        ):
            run = orchestrator.start(self.wf, inputs={})
            result = orchestrator.execute(run)
        self.assertEqual(result.status, WorkflowRun.Status.FAILED)

    def test_downstream_task_skipped_on_upstream_failure(self):
        from controlplane.services.orchestrator import orchestrator
        self._add_task("root", self.agent_a, depends_on=[], order=0)
        self._add_task("dependent", self.agent_b, depends_on=["root"], order=1)
        call_count = [0]

        def _failing_invoke(agent, message, workflow_run):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Root failed")
            return ("output", None)

        with patch(
            "controlplane.services.orchestrator.OrchestratorService._invoke_agent",
            side_effect=_failing_invoke,
        ):
            run = orchestrator.start(self.wf, inputs={})
            result = orchestrator.execute(run)

        skipped = WorkflowTaskRun.objects.filter(
            workflow_run=result, status=WorkflowTaskRun.Status.SKIPPED
        )
        self.assertGreater(skipped.count(), 0)

    def test_empty_workflow_raises(self):
        from controlplane.services.orchestrator import orchestrator
        empty_wf = _make_workflow("empty-wf")
        run = orchestrator.start(empty_wf, inputs={})
        result = orchestrator.execute(run)
        self.assertEqual(result.status, WorkflowRun.Status.FAILED)
        self.assertIn("no tasks", result.error.lower())

    def test_retry_on_transient_failure(self):
        from controlplane.services.orchestrator import orchestrator
        task = self._add_task("retry_step", self.agent_a)
        task.retry_limit = 1
        task.save(update_fields=["retry_limit"])

        call_count = [0]

        def _flaky(agent, message, workflow_run):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Transient error")
            return ("ok", None)

        with patch(
            "controlplane.services.orchestrator.OrchestratorService._invoke_agent",
            side_effect=_flaky,
        ):
            run = orchestrator.start(self.wf, inputs={})
            result = orchestrator.execute(run)

        self.assertEqual(result.status, WorkflowRun.Status.COMPLETED)
        self.assertEqual(call_count[0], 2)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class WorkflowApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.wf = _make_workflow("api-wf", bu=self.bu)
        self.agent = _make_agent("api-agent", bu=self.bu)

    def test_workflows_list(self):
        resp = self.client.get("/api/v1/workflows/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("workflows", data)

    def test_workflow_detail(self):
        resp = self.client.get(f"/api/v1/workflows/{self.wf.id}/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["slug"], "api-wf")

    def test_workflow_trigger_returns_202(self):
        # Add a task so the workflow isn't empty (execution is async)
        WorkflowTask.objects.create(
            workflow=self.wf, step_name="step1", agent=self.agent,
            depends_on=[], input_template="test", order=0,
        )
        with patch("controlplane.services.orchestrator.OrchestratorService.execute"):
            resp = self.client.post(
                f"/api/v1/workflows/{self.wf.id}/run/",
                data=json.dumps({"inputs": {"topic": "revenue"}}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        data = json.loads(resp.content)
        self.assertIn("workflow_run_id", data)

    def test_workflow_run_detail(self):
        run = WorkflowRun.objects.create(
            workflow=self.wf, status=WorkflowRun.Status.COMPLETED, triggered_by="tester"
        )
        resp = self.client.get(f"/api/v1/workflow-runs/{run.id}/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "completed")

    def test_workflow_run_tasks(self):
        run = WorkflowRun.objects.create(
            workflow=self.wf, status=WorkflowRun.Status.COMPLETED, triggered_by="tester"
        )
        resp = self.client.get(f"/api/v1/workflow-runs/{run.id}/tasks/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("tasks", data)


class SharedMemoryApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.wf = _make_workflow("mem-wf", bu=self.bu)
        self.run = WorkflowRun.objects.create(
            workflow=self.wf, status=WorkflowRun.Status.RUNNING, triggered_by="tester"
        )

    def test_memory_write_and_read_via_api(self):
        resp = self.client.post(
            f"/api/v1/workflow-runs/{self.run.id}/memory/",
            data=json.dumps({"key": "summary", "value": "Revenue up 15%"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.get(f"/api/v1/workflow-runs/{self.run.id}/memory/")
        self.assertEqual(resp2.status_code, 200)
        data = json.loads(resp2.content)
        keys = [e["key"] for e in data["entries"]]
        self.assertIn("summary", keys)


class ModelRouteApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)

    def test_model_route_explain(self):
        resp = self.client.get(f"/api/v1/agents/{self.agent.id}/model-route/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("model_id", data)
        self.assertIn("risk_tier", data)
