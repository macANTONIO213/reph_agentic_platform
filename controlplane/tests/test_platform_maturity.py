import json
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.utils import timezone

from controlplane.models import BusinessUnit, Workflow, WorkflowRun


def _platform_admin(username="platform-admin", staff=False):
    user, _ = User.objects.get_or_create(username=username)
    user.is_staff = staff
    user.save(update_fields=["is_staff"])
    if not staff:
        group, _ = Group.objects.get_or_create(name="platform_admin")
        user.groups.add(group)
    return user


class PlatformMaturityApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = _platform_admin()
        self.viewer = User.objects.create_user(username="viewer")

    def test_health_endpoint_is_public(self):
        resp = self.client.get("/api/v1/platform/health/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "ok")

    def test_readiness_requires_platform_admin(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/v1/platform/readiness/")
        self.assertEqual(resp.status_code, 403)

    def test_maturity_returns_scorecard_for_admin(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/api/v1/platform/maturity/?window_hours=12")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("summary", data)
        self.assertIn("checks", data)
        self.assertEqual(data["window_hours"], 12)

    def test_success_criteria_returns_enterprise_grade_for_admin(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/api/v1/platform/success-criteria/?window_hours=12")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("enterprise_grade", data)
        self.assertIn("criteria", data)

    def test_success_criteria_requires_platform_admin(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/v1/platform/success-criteria/")
        self.assertEqual(resp.status_code, 403)

    def test_readiness_returns_503_when_stale_running_workflow_exists(self):
        bu = BusinessUnit.objects.create(name="Ops", code="OPS")
        wf = Workflow.objects.create(name="Stale WF", slug="stale-wf", status=Workflow.Status.ACTIVE, business_unit=bu)
        run = WorkflowRun.objects.create(workflow=wf, status=WorkflowRun.Status.RUNNING, triggered_by="system")
        WorkflowRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=2)
        )

        self.client.force_login(self.admin)
        resp = self.client.get("/api/v1/platform/readiness/")
        self.assertEqual(resp.status_code, 503)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "degraded")


class PlatformMaturityCommandTests(TestCase):
    def test_command_outputs_json(self):
        out = StringIO()
        call_command("platform_maturity_report", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertIn("summary", payload)
        self.assertIn("checks", payload)

    def test_fail_on_unready_raises_command_error(self):
        bu = BusinessUnit.objects.create(name="Risk", code="RISK")
        wf = Workflow.objects.create(name="Risk WF", slug="risk-wf", status=Workflow.Status.ACTIVE, business_unit=bu)
        run = WorkflowRun.objects.create(workflow=wf, status=WorkflowRun.Status.RUNNING, triggered_by="system")
        WorkflowRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        with self.assertRaises(CommandError):
            call_command("platform_maturity_report", "--fail-on-unready")

    def test_enterprise_success_criteria_command_outputs_json(self):
        out = StringIO()
        call_command("enterprise_success_criteria", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertIn("enterprise_grade", payload)
        self.assertIn("criteria", payload)

    def test_enterprise_success_criteria_fail_on_miss_raises(self):
        bu = BusinessUnit.objects.create(name="Fail", code="FAIL")
        wf = Workflow.objects.create(name="Fail WF", slug="fail-wf", status=Workflow.Status.ACTIVE, business_unit=bu)
        run = WorkflowRun.objects.create(workflow=wf, status=WorkflowRun.Status.RUNNING, triggered_by="system")
        WorkflowRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        with self.assertRaises(CommandError):
            call_command("enterprise_success_criteria", "--fail-on-miss")
