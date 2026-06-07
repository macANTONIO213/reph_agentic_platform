"""
Phase D tests — Observability & Cost Controls
Covers: TelemetryService (OTel spans), compute_budgets command,
        Prometheus metrics renderer, budget/span API endpoints.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentRun,
    AuditLog,
    BudgetAlert,
    BusinessUnit,
    OtelSpan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bu(name="Engineering"):
    bu, _ = BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:4].upper()})
    return bu


def _make_agent(slug="test-agent", bu=None, budget=None):
    kw = {}
    if budget is not None:
        kw["budget_usd_monthly"] = budget
    return Agent.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        purpose="Automate reports",
        business_unit=(bu.name if bu else "Engineering"),
        risk_tier=1,
        status=Agent.Status.PRODUCTION,
        org_unit=bu,
        **kw,
    )


def _make_run(agent, status="completed", cost=Decimal("0.01"), latency=200):
    return AgentRun.objects.create(
        agent=agent,
        user_label="tester",
        channel="web",
        input_text="test",
        output_text="result",
        status=status,
        cost_usd=cost,
        latency_ms=latency,
        input_tokens=100,
        output_tokens=50,
        model_id="claude-opus-4-8",
        completed_at=timezone.now(),
    )


def _make_user(username="testuser", staff=True):
    u, _ = User.objects.get_or_create(username=username, defaults={"is_staff": staff})
    return u


# ---------------------------------------------------------------------------
# TelemetryService — OTel span creation
# ---------------------------------------------------------------------------

class TelemetryServiceTests(TestCase):

    def setUp(self):
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)

    def test_open_root_span_creates_otelspan(self):
        from controlplane.services.telemetry import telemetry_service
        run = _make_run(self.agent)
        telemetry_service._open_root_span(run, self.agent)
        span = OtelSpan.objects.filter(run=run, name="agent.run").first()
        self.assertIsNotNone(span)
        self.assertEqual(span.kind, OtelSpan.Kind.SERVER)
        self.assertEqual(span.status_code, "UNSET")

    def test_close_root_span_updates_status(self):
        from controlplane.services.telemetry import telemetry_service
        run = _make_run(self.agent)
        telemetry_service._open_root_span(run, self.agent)
        telemetry_service._close_root_span(run)
        span = OtelSpan.objects.filter(run=run, name="agent.run").first()
        self.assertEqual(span.status_code, "OK")
        self.assertIsNotNone(span.end_time)
        self.assertGreater(span.duration_ms, 0)

    def test_close_root_span_with_error_sets_error_status(self):
        from controlplane.services.telemetry import telemetry_service
        run = _make_run(self.agent, status="failed")
        telemetry_service._open_root_span(run, self.agent)
        telemetry_service._close_root_span(run, error="LLM timeout")
        span = OtelSpan.objects.filter(run=run, name="agent.run").first()
        self.assertEqual(span.status_code, "ERROR")
        self.assertIn("timeout", span.status_message)

    def test_record_child_span_stores_attributes(self):
        from controlplane.services.telemetry import telemetry_service
        run = _make_run(self.agent)
        telemetry_service.record_child_span(
            run, self.agent, "agent.guardrail_scan",
            duration_ms=5,
            attributes={"guardrail.findings": 0},
        )
        span = OtelSpan.objects.filter(run=run, name="agent.guardrail_scan").first()
        self.assertIsNotNone(span)
        self.assertEqual(span.duration_ms, 5)
        self.assertEqual(span.attributes["guardrail.findings"], 0)

    def test_trace_id_derived_from_run_id(self):
        from controlplane.services.telemetry import _trace_id_from_run
        run = _make_run(self.agent)
        trace_id = _trace_id_from_run(run.id)
        self.assertEqual(len(trace_id), 32)
        self.assertTrue(trace_id.isalnum())

    def test_open_close_sets_cost_attribute(self):
        from controlplane.services.telemetry import telemetry_service
        run = _make_run(self.agent, cost=Decimal("0.042"))
        telemetry_service._open_root_span(run, self.agent)
        telemetry_service._close_root_span(run)
        span = OtelSpan.objects.filter(run=run, name="agent.run").first()
        self.assertAlmostEqual(span.attributes["cost.usd"], 0.042, places=4)


# ---------------------------------------------------------------------------
# TelemetryService — Budget check
# ---------------------------------------------------------------------------

class BudgetCheckTests(TestCase):

    def setUp(self):
        self.bu = _make_bu("Finance")
        self.agent = _make_agent("budget-agent", bu=self.bu, budget=Decimal("1.00"))

    def test_no_breach_when_under_budget(self):
        from controlplane.services.telemetry import telemetry_service
        _make_run(self.agent, cost=Decimal("0.50"))
        breached = telemetry_service.check_budget(self.agent)
        self.assertFalse(breached)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.budget_alert)

    def test_breach_detected_when_over_budget(self):
        from controlplane.services.telemetry import telemetry_service
        _make_run(self.agent, cost=Decimal("1.50"))
        breached = telemetry_service.check_budget(self.agent)
        self.assertTrue(breached)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.budget_alert)
        alert = BudgetAlert.objects.filter(agent=self.agent).first()
        self.assertIsNotNone(alert)
        self.assertGreater(alert.overage_usd, 0)

    def test_breach_creates_audit_log(self):
        from controlplane.services.telemetry import telemetry_service
        _make_run(self.agent, cost=Decimal("2.00"))
        telemetry_service.check_budget(self.agent)
        log = AuditLog.objects.filter(action="budget.breach_detected", resource_id=str(self.agent.id)).first()
        self.assertIsNotNone(log)

    def test_no_action_when_no_budget_set(self):
        from controlplane.services.telemetry import telemetry_service
        agent_no_budget = _make_agent("no-budget-agent", bu=self.bu)
        _make_run(agent_no_budget, cost=Decimal("999.00"))
        breached = telemetry_service.check_budget(agent_no_budget)
        self.assertFalse(breached)

    def test_second_check_does_not_duplicate_alert(self):
        from controlplane.services.telemetry import telemetry_service
        _make_run(self.agent, cost=Decimal("2.00"))
        telemetry_service.check_budget(self.agent)
        telemetry_service.check_budget(self.agent)  # Second call
        self.assertEqual(BudgetAlert.objects.filter(agent=self.agent).count(), 1)


# ---------------------------------------------------------------------------
# compute_budgets management command
# ---------------------------------------------------------------------------

class ComputeBudgetsCommandTests(TestCase):

    def setUp(self):
        self.bu = _make_bu("Legal")
        self.agent = _make_agent("legal-agent", bu=self.bu, budget=Decimal("0.50"))

    def test_command_sets_budget_alert_flag(self):
        _make_run(self.agent, cost=Decimal("0.75"))
        from django.core.management import call_command
        call_command("compute_budgets", verbosity=0)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.budget_alert)

    def test_command_dry_run_does_not_write(self):
        _make_run(self.agent, cost=Decimal("0.75"))
        from django.core.management import call_command
        call_command("compute_budgets", dry_run=True, verbosity=0)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.budget_alert)

    def test_command_resolves_flag_when_under_budget(self):
        # Flag already set from previous breach
        self.agent.budget_alert = True
        self.agent.save(update_fields=["budget_alert"])
        # But this month's spend is low
        _make_run(self.agent, cost=Decimal("0.10"))
        from django.core.management import call_command
        call_command("compute_budgets", verbosity=0)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.budget_alert)


# ---------------------------------------------------------------------------
# Prometheus metrics renderer
# ---------------------------------------------------------------------------

class MetricsRendererTests(TestCase):

    def setUp(self):
        self.bu = _make_bu()
        self.agent = _make_agent("metrics-agent", bu=self.bu)
        _make_run(self.agent, cost=Decimal("0.02"), latency=300)

    def test_render_returns_string(self):
        from controlplane.services.metrics import render_metrics
        output = render_metrics()
        self.assertIsInstance(output, str)

    def test_render_contains_agent_slug(self):
        from controlplane.services.metrics import render_metrics
        output = render_metrics()
        self.assertIn("metrics-agent", output)

    def test_render_contains_required_metric_names(self):
        from controlplane.services.metrics import render_metrics
        output = render_metrics()
        for metric in [
            "relx_agent_runs_total",
            "relx_platform_agents_total",
            "relx_platform_runs_24h",
            "relx_platform_cost_usd_24h",
            "relx_agent_cost_usd_month",
            "relx_agent_budget_alert",
            "relx_agent_quality_alert",
        ]:
            self.assertIn(metric, output, f"Missing metric: {metric}")

    def test_render_valid_prometheus_format(self):
        from controlplane.services.metrics import render_metrics
        output = render_metrics()
        # Every non-comment, non-empty line should have at least one space
        for line in output.splitlines():
            if line and not line.startswith("#"):
                self.assertIn(" ", line, f"Invalid Prometheus line: {line!r}")


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class MetricsApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)

    def test_metrics_endpoint_returns_200(self):
        resp = self.client.get("/api/v1/metrics/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp["Content-Type"])

    def test_metrics_requires_auth(self):
        anon = Client()
        resp = anon.get("/api/v1/metrics/")
        self.assertIn(resp.status_code, [302, 401, 403])


class OtelSpanApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)
        self.run = _make_run(self.agent)
        from controlplane.services.telemetry import telemetry_service
        telemetry_service._open_root_span(self.run, self.agent)

    def test_spans_by_run_id(self):
        resp = self.client.get(f"/api/v1/spans/?run_id={self.run.id}")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("spans", data)
        self.assertGreater(len(data["spans"]), 0)

    def test_spans_by_agent_id(self):
        resp = self.client.get(f"/api/v1/spans/?agent_id={self.agent.id}")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertGreater(data["count"], 0)

    def test_spans_require_auth(self):
        anon = Client()
        resp = anon.get("/api/v1/spans/")
        self.assertIn(resp.status_code, [302, 401, 403])


class BudgetAlertApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.agent = _make_agent("alert-agent", bu=self.bu, budget=Decimal("0.01"))

    def test_budget_alerts_returns_active(self):
        BudgetAlert.objects.create(
            agent=self.agent,
            period_month="2026-06",
            budget_usd=Decimal("0.01"),
            actual_usd=Decimal("0.05"),
            overage_usd=Decimal("0.04"),
            resolved=False,
        )
        resp = self.client.get("/api/v1/budget-alerts/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertGreater(data["count"], 0)

    def test_budget_alerts_excludes_resolved_by_default(self):
        BudgetAlert.objects.create(
            agent=self.agent,
            period_month="2026-05",
            budget_usd=Decimal("0.01"),
            actual_usd=Decimal("0.05"),
            overage_usd=Decimal("0.04"),
            resolved=True,
        )
        resp = self.client.get("/api/v1/budget-alerts/")
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 0)

    def test_budget_alerts_requires_auth(self):
        anon = Client()
        resp = anon.get("/api/v1/budget-alerts/")
        self.assertIn(resp.status_code, [302, 401, 403])
