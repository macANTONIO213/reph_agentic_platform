"""
Production hardening — orchestrator parallel task fan-out.

Independent tasks in a DAG wave now run concurrently. These use TransactionTestCase
(not TestCase) because worker threads open their own DB connections and must see
committed data — a plain TestCase's per-test transaction is invisible to them.
"""
import time
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from controlplane.models import (
    Agent, BusinessUnit, Workflow, WorkflowRun, WorkflowTask, WorkflowTaskRun,
)
from controlplane.services.orchestrator import orchestrator


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug, bu):
    return Agent.objects.create(
        name=slug, slug=slug, purpose="p", business_unit=bu.name, owner="o",
        technical_owner="o", system_prompt="s", platform="embedded",
        status=Agent.Status.PRODUCTION, risk_tier=1, org_unit=bu,
    )


def _wf(bu, slug="wf-par"):
    return Workflow.objects.create(
        slug=slug, name=slug, business_unit=bu, status=Workflow.Status.ACTIVE, owner="t",
    )


def _task(wf, step, agent, *, deps=None, order=0):
    return WorkflowTask.objects.create(
        workflow=wf, step_name=step, agent=agent, depends_on=deps or [],
        input_template=f"run {step}", order=order,
    )


def _delayed_invoke(delay=0.02, output='{"ok":true}'):
    def _fn(self, agent, message, workflow_run):
        time.sleep(delay)
        return output, None
    return patch(
        "controlplane.services.orchestrator.OrchestratorService._invoke_agent", new=_fn,
    )


@override_settings(ORCHESTRATOR_MAX_PARALLEL=4)
class ParallelFanOutTests(TransactionTestCase):
    def setUp(self):
        self.bu = _bu()
        self.agents = [_agent(f"pa{i}", self.bu) for i in range(6)]

    def test_all_independent_outputs_captured(self):
        wf = _wf(self.bu, "wf-fan")
        for i in range(5):
            _task(wf, f"s{i}", self.agents[i], order=i)
        with _delayed_invoke(0.02, '{"v":1}'):
            run = orchestrator.execute(orchestrator.start(wf, inputs={}))
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        # every step's output survived the concurrent wave (no lost updates)
        self.assertEqual(set(run.outputs.keys()), {f"s{i}" for i in range(5)})
        self.assertEqual(
            WorkflowTaskRun.objects.filter(
                workflow_run=run, status=WorkflowTaskRun.Status.COMPLETED).count(),
            5,
        )

    def test_wave_runs_concurrently(self):
        wf = _wf(self.bu, "wf-time")
        for i in range(4):
            _task(wf, f"s{i}", self.agents[i], order=i)
        with _delayed_invoke(0.15):
            run = orchestrator.start(wf, inputs={})
            t0 = time.perf_counter()
            orchestrator.execute(run)
            elapsed = time.perf_counter() - t0
        # 4 × 0.15s = 0.6s if sequential; concurrent should be well under.
        self.assertLess(elapsed, 0.45, f"fan-out not concurrent (elapsed={elapsed:.2f}s)")

    def test_diamond_dag(self):
        wf = _wf(self.bu, "wf-diamond")
        _task(wf, "A", self.agents[0], order=0)
        _task(wf, "B", self.agents[1], deps=["A"], order=1)
        _task(wf, "C", self.agents[2], deps=["A"], order=2)
        _task(wf, "D", self.agents[3], deps=["B", "C"], order=3)
        with _delayed_invoke(0.02):
            run = orchestrator.execute(orchestrator.start(wf, inputs={}))
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertEqual(set(run.outputs.keys()), {"A", "B", "C", "D"})


@override_settings(ORCHESTRATOR_MAX_PARALLEL=1)
class SequentialModeTests(TransactionTestCase):
    def test_sequential_still_completes(self):
        bu = _bu()
        wf = _wf(bu, "wf-seq")
        for i in range(3):
            _task(wf, f"s{i}", _agent(f"sa{i}", bu), order=i)
        with _delayed_invoke(0.01):
            run = orchestrator.execute(orchestrator.start(wf, inputs={}))
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertEqual(len(run.outputs), 3)
