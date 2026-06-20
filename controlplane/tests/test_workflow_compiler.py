"""
Tests for M3 — WorkflowCompiler (blueprint → Workflow DAG, run via orchestrator).

Covers:
  - compile creates a DRAFT Workflow with one task per step
  - linear depends_on wiring and declared depends_on
  - input_template chaining (first reads inputs, later read upstream outputs)
  - requires a built agent
  - should_compile_workflow heuristic
  - the compiled workflow runs to completion via the orchestrator (fake engine)
"""
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentBlueprint,
    ProcessInsight,
    Workflow,
    WorkflowRun,
    WorkflowTask,
)
from controlplane.services.factory import blueprint_generator, build_compiler
from controlplane.services.workflow_compiler import (
    should_compile_workflow,
    workflow_compiler,
)


def _built_blueprint(systems=None, steps=None):
    insight = ProcessInsight.objects.create(
        source_reference=f"PI-{uuid.uuid4()}", process_name="Invoice Matching",
        finding_type="automation_opportunity", summary="s",
        systems_involved=systems if systems is not None else ["rest-api"],
    )
    bp = blueprint_generator.generate(insight)
    if steps is not None:
        bp.workflow_steps = steps
    bp.status = AgentBlueprint.Status.APPROVED
    bp.save()
    agent = build_compiler.build(bp, built_by="builder")
    bp.refresh_from_db()
    return bp, agent


class CompileStructureTests(TestCase):

    def test_compile_creates_workflow_and_tasks(self):
        bp, _ = _built_blueprint(steps=[
            {"step": "ingest", "description": "Receive invoice"},
            {"step": "match", "description": "Three-way match"},
            {"step": "report", "description": "Emit result"},
        ])
        wf = workflow_compiler.compile(bp, built_by="builder")
        self.assertIsInstance(wf, Workflow)
        self.assertEqual(wf.status, Workflow.Status.DRAFT)
        self.assertEqual(wf.tasks.count(), 3)

    def test_linear_depends_on(self):
        bp, _ = _built_blueprint(steps=[
            {"step": "a", "description": "step a"},
            {"step": "b", "description": "step b"},
        ])
        wf = workflow_compiler.compile(bp, built_by="builder")
        first = wf.tasks.get(step_name="a")
        second = wf.tasks.get(step_name="b")
        self.assertEqual(first.depends_on, [])
        self.assertEqual(second.depends_on, ["a"])

    def test_declared_depends_on_respected(self):
        bp, _ = _built_blueprint(steps=[
            {"step": "fetch", "description": "fetch"},
            {"step": "score", "description": "score"},
            {"step": "decide", "description": "decide", "depends_on": ["fetch", "score"]},
        ])
        wf = workflow_compiler.compile(bp, built_by="builder")
        decide = wf.tasks.get(step_name="decide")
        self.assertEqual(sorted(decide.depends_on), ["fetch", "score"])

    def test_input_template_chaining(self):
        bp, _ = _built_blueprint(steps=[
            {"step": "first", "description": "do first"},
            {"step": "second", "description": "do second"},
        ])
        wf = workflow_compiler.compile(bp, built_by="builder")
        first = wf.tasks.get(step_name="first")
        second = wf.tasks.get(step_name="second")
        self.assertIn("{{inputs.message}}", first.input_template)
        self.assertIn("{{outputs.first.text}}", second.input_template)

    def test_all_tasks_assigned_to_built_agent(self):
        bp, agent = _built_blueprint(steps=[
            {"step": "a", "description": "a"}, {"step": "b", "description": "b"},
        ])
        wf = workflow_compiler.compile(bp, agent=agent, built_by="builder")
        self.assertTrue(all(t.agent_id == agent.id for t in wf.tasks.all()))

    def test_unique_step_names_when_labels_collide(self):
        bp, _ = _built_blueprint(steps=[
            {"step": "review", "description": "x"},
            {"step": "review", "description": "y"},
        ])
        wf = workflow_compiler.compile(bp, built_by="builder")
        names = list(wf.tasks.values_list("step_name", flat=True))
        self.assertEqual(len(set(names)), 2)


class GuardTests(TestCase):

    def test_requires_built_agent(self):
        insight = ProcessInsight.objects.create(
            source_reference=f"PI-{uuid.uuid4()}", process_name="P",
            finding_type="other", summary="s", systems_involved=["rest-api"],
        )
        bp = blueprint_generator.generate(insight)  # not built → no built_agent
        with self.assertRaises(ValueError):
            workflow_compiler.compile(bp, built_by="builder")

    def test_requires_steps(self):
        bp, agent = _built_blueprint(steps=[])
        with self.assertRaises(ValueError):
            workflow_compiler.compile(bp, agent=agent, built_by="builder")

    def test_should_compile_workflow_heuristic(self):
        bp_multi, _ = _built_blueprint(steps=[{"step": "a"}, {"step": "b"}])
        bp_single, _ = _built_blueprint(steps=[{"step": "only"}])
        self.assertTrue(should_compile_workflow(bp_multi))
        self.assertFalse(should_compile_workflow(bp_single))


class OrchestratorRunTests(TestCase):

    def test_compiled_workflow_runs_to_completion(self):
        # No ANTHROPIC_API_KEY in tests → DjangoRuntimeAdapter fake engine runs.
        bp, _ = _built_blueprint(steps=[
            {"step": "analyse", "description": "Analyse the case"},
            {"step": "summarise", "description": "Summarise findings"},
        ])
        wf, run = workflow_compiler.compile_and_run(
            bp, inputs={"message": "Process invoice 123"}, triggered_by="tester"
        )
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        # Both steps produced output captured under their step_name.
        self.assertIn("analyse", run.outputs)
        self.assertIn("summarise", run.outputs)
