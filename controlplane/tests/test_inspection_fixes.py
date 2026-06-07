"""
Regression tests for the inspection findings (#1–#11).

Each of these pins behaviour that previously regressed *silently* because the
relevant path was never exercised end-to-end (the orchestrator tests mocked the
agent invocation; the run endpoint, transition gate, SSRF guard, tenant scoping,
memory authz, and output guardrail had no coverage at all).

Run-based tests patch out the token-streaming sleep for speed and use the Echo
adapter (platform="embedded") so no external API is required.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from controlplane.models import (
    Agent, AgentRun, AuditLog, BusinessUnit, EvalSuite,
    GovernanceReview, SharedMemory, Workflow, WorkflowRun, WorkflowTask,
    WorkflowTaskRun,
)

# Patch the per-token sleep so streamed runs complete instantly.
NO_SLEEP = patch("controlplane.services.adapters.base.time.sleep", lambda *a, **k: None)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bu(name="Engineering"):
    bu, _ = BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})
    return bu


def _agent(slug, bu, *, platform="embedded", tier=1,
           status=Agent.Status.PRODUCTION, guardrail="off"):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose="p",
        business_unit=bu.name, owner="o", technical_owner="o", system_prompt="s",
        platform=platform, status=status, risk_tier=tier, org_unit=bu,
        guardrail_level=guardrail,
    )


def _user(username, *, staff=False, role=None, bu=None, group=None):
    u, _ = User.objects.get_or_create(username=username, defaults={"is_staff": staff})
    u.is_staff = staff
    u.save()
    if role is not None or bu is not None:
        p = u.profile
        if bu is not None:
            p.business_unit = bu
        if role is not None:
            p.role = role
        p.save()
    if group:
        g, _ = Group.objects.get_or_create(name=group)
        u.groups.add(g)
    return u


# ── #1 Core run path ─────────────────────────────────────────────────────────

@NO_SLEEP
class CoreRunPathTests(TestCase):
    """Regression: PlatformAgentRuntime.stream crashed on a UUID->'.id' bug."""

    def setUp(self):
        self.bu = _bu()
        self.user = _user("runner", staff=True)
        self.client.force_login(self.user)

    def test_run_completes_without_crash(self):
        a = _agent("run-a", self.bu)  # embedded -> EchoAdapter
        resp = self.client.post(
            f"/api/agents/{a.id}/run/",
            data=json.dumps({"message": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = b"".join(resp.streaming_content)
        self.assertIn(b"event: done", body)
        self.assertNotIn(b"Run failed", body)
        run = AgentRun.objects.filter(agent=a).latest("started_at")
        self.assertEqual(run.status, AgentRun.Status.COMPLETED)
        self.assertTrue(run.output_text)


# ── #2 Orchestrator end-to-end (no mocks) ────────────────────────────────────

@NO_SLEEP
class OrchestratorEndToEndTests(TestCase):
    """Regression: orchestrator imported a non-existent AgentRuntime and parsed
    SSE incorrectly, so real workflows failed and captured no output."""

    def test_two_step_workflow_completes_with_real_runtime(self):
        bu = _bu()
        a1 = _agent("o-a", bu)
        a2 = _agent("o-b", bu)
        wf = Workflow.objects.create(
            name="WF", slug="wf-e2e", status=Workflow.Status.ACTIVE, business_unit=bu
        )
        WorkflowTask.objects.create(
            workflow=wf, step_name="s1", agent=a1, depends_on=[],
            input_template="start", order=0,
        )
        WorkflowTask.objects.create(
            workflow=wf, step_name="s2", agent=a2, depends_on=["s1"],
            input_template="use {{outputs.s1.text}}", order=1,
        )
        from controlplane.services.orchestrator import orchestrator
        run = orchestrator.execute(orchestrator.start(wf, inputs={}, triggered_by="t"))

        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertIn("s1", run.outputs)
        self.assertIn("s2", run.outputs)
        task_runs = list(run.task_runs.all())
        self.assertEqual({tr.status for tr in task_runs}, {WorkflowTaskRun.Status.COMPLETED})
        # SSE parse fix: output text was actually captured from the stream.
        self.assertTrue(all(tr.raw_output for tr in task_runs))

    def test_delegate_to_agent_returns_output(self):
        bu = _bu()
        _agent("deleg-target", bu)
        from controlplane.services.orchestrator import delegate_to_agent
        result = delegate_to_agent(agent_slug="deleg-target", message="ping")
        self.assertNotIn("error", result)
        self.assertTrue(result["output"])


# ── #3 API production-gate enforcement ───────────────────────────────────────

class ProductionGateApiTests(TestCase):
    """Regression: the API transition path bypassed the Approval + Eval gates."""

    def setUp(self):
        self.bu = _bu()

    def _pilot_agent(self, slug):
        a = _agent(slug, self.bu, tier=3, status=Agent.Status.PILOT)
        GovernanceReview.objects.create(
            agent=a, reviewer="r", status=GovernanceReview.Status.APPROVED
        )
        return a

    def test_platform_admin_cannot_promote_without_approval_and_eval(self):
        a = self._pilot_agent("gate-a")
        EvalSuite.objects.create(agent=a, name="s", is_active=True)  # no passing run
        padmin = _user("padmin", staff=False, group="platform_admin")
        self.client.force_login(padmin)
        resp = self.client.post(
            f"/api/v1/agents/{a.id}/transition/",
            data=json.dumps({"status": "production"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.status, Agent.Status.PILOT)

    def test_staff_can_break_glass(self):
        a = self._pilot_agent("gate-b")
        staff = _user("staffx", staff=True)
        self.client.force_login(staff)
        resp = self.client.post(
            f"/api/v1/agents/{a.id}/transition/",
            data=json.dumps({"status": "production"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.status, Agent.Status.PRODUCTION)


# ── #4 SSRF guard ────────────────────────────────────────────────────────────

class SsrfGuardTests(TestCase):
    """Regression: HttpApiAdapter called agent.endpoint_url with no validation."""

    def test_blocks_dangerous_schemes_and_addresses(self):
        from controlplane.services.adapters.http_api import (
            _validate_endpoint, EndpointValidationError,
        )
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://127.0.0.1:8000/admin",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.assertRaises(EndpointValidationError, msg=url):
                _validate_endpoint(url)

    def test_allows_ordinary_http_host(self):
        from controlplane.services.adapters.http_api import _validate_endpoint
        # Numeric private IP — resolves without DNS and is permitted by design
        # (enterprise internal endpoints are legitimate).
        _validate_endpoint("http://10.0.0.5:8080/api")  # must not raise


# ── #5 Tenant scoping ────────────────────────────────────────────────────────

@NO_SLEEP
class TenantScopingTests(TestCase):
    """Regression: any user could list and run agents in any business unit."""

    def setUp(self):
        self.bu1 = _bu("BU One")
        self.bu2 = _bu("BU Two")
        self.a1 = _agent("t-a1", self.bu1)
        self.a2 = _agent("t-a2", self.bu2)
        self.viewer = _user("scopeview", staff=False, role="viewer", bu=self.bu1)

    def test_list_scoped_to_own_bu(self):
        self.client.force_login(self.viewer)
        data = json.loads(self.client.get("/api/v1/agents/").content)["agents"]
        slugs = {x["slug"] for x in data}
        self.assertIn("t-a1", slugs)
        self.assertNotIn("t-a2", slugs)

    def test_cannot_run_foreign_agent(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(
            f"/api/agents/{self.a2.id}/run/",
            data=json.dumps({"message": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_can_run_own_bu_agent(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(
            f"/api/agents/{self.a1.id}/run/",
            data=json.dumps({"message": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        b"".join(resp.streaming_content)

    def test_staff_sees_all_bus(self):
        staff = _user("scopestaff", staff=True)
        self.client.force_login(staff)
        data = json.loads(self.client.get("/api/v1/agents/").content)["agents"]
        slugs = {x["slug"] for x in data}
        self.assertTrue({"t-a1", "t-a2"} <= slugs)


# ── #6 Shared-memory authorization ───────────────────────────────────────────

class SharedMemoryAuthzTests(TestCase):
    """Regression: any user could write to any workflow run's shared memory."""

    def setUp(self):
        self.bu1 = _bu("M One")
        self.bu2 = _bu("M Two")
        self.wf = Workflow.objects.create(
            name="MW", slug="mw", status=Workflow.Status.ACTIVE, business_unit=self.bu2
        )
        self.run = WorkflowRun.objects.create(
            workflow=self.wf, status=WorkflowRun.Status.RUNNING, triggered_by="owner_user"
        )

    def _post(self, user, key):
        self.client.force_login(user)
        return self.client.post(
            f"/api/v1/workflow-runs/{self.run.id}/memory/",
            data=json.dumps({"key": key, "value": "v"}),
            content_type="application/json",
        )

    def test_unrelated_user_blocked(self):
        v = _user("memview", staff=False, role="viewer", bu=self.bu1)
        resp = self._post(v, "k")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(SharedMemory.objects.filter(workflow_run=self.run, key="k").exists())

    def test_triggerer_allowed(self):
        owner = _user("owner_user", staff=False, role="viewer", bu=self.bu1)
        self.assertEqual(self._post(owner, "k1").status_code, 200)

    def test_same_bu_member_allowed(self):
        mate = _user("bumate", staff=False, role="viewer", bu=self.bu2)
        self.assertEqual(self._post(mate, "k2").status_code, 200)


# ── #7 Model router Tier-4 capability floor ──────────────────────────────────

class ModelRouterTier4Tests(TestCase):
    """Regression: budget pressure silently downgraded Tier-4 agents to haiku."""

    def _agent(self, platform, tier, budget):
        a = _agent(f"r-{platform[:4]}-{tier}-{int(budget)}", _bu(), platform=platform, tier=tier)
        a.model_id = ""
        a.budget_alert = budget
        return a

    def test_tier4_not_downgraded_by_budget(self):
        from controlplane.services.model_router import model_router
        self.assertEqual(
            model_router.select(self._agent("django_runtime", 4, True)), "claude-opus-4-8"
        )

    def test_openai_tier4_not_downgraded_by_budget(self):
        from controlplane.services.model_router import model_router
        self.assertEqual(
            model_router.select(self._agent("azure_ai_foundry", 4, True)), "gpt-4o"
        )

    def test_tier3_budget_still_downgrades(self):
        from controlplane.services.model_router import model_router
        self.assertEqual(
            model_router.select(self._agent("django_runtime", 3, True)), "claude-haiku-4-5"
        )


# ── #8 Registration authorization status code ────────────────────────────────

class RegistrationAuthzTests(TestCase):
    """Regression: unauthorized registration returned 500 instead of 403."""

    def test_viewer_register_forbidden(self):
        bu = _bu()
        v = _user("regview", staff=False, role="viewer", bu=bu)
        self.client.force_login(v)
        resp = self.client.post(
            "/api/v1/agents/register/",
            data=json.dumps({"name": "X", "platform": "django_runtime",
                             "owner": "o", "purpose": "p", "system_prompt": "s"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_builder_register_ok(self):
        bu = _bu()
        b = _user("regbuild", staff=False, role="agent_builder", bu=bu)
        self.client.force_login(b)
        resp = self.client.post(
            "/api/v1/agents/register/",
            data=json.dumps({"name": "X2", "platform": "django_runtime",
                             "owner": "o", "purpose": "p", "system_prompt": "s",
                             "org_unit_id": str(bu.id)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)


# ── #9 Pricing accuracy ──────────────────────────────────────────────────────

class PricingTests(TestCase):
    """Regression: unknown model_id priced at $0 with no warning."""

    def test_unknown_model_warns_and_returns_zero(self):
        from controlplane.services import pricing
        with self.assertLogs("controlplane.services.pricing", level="WARNING") as cm:
            val = pricing.price_run(1000, 1000, "totally-bogus-model")
        self.assertEqual(val, 0)
        self.assertTrue(any("unknown model_id" in line for line in cm.output))

    def test_free_model_no_warning(self):
        from controlplane.services import pricing
        with self.assertNoLogs("controlplane.services.pricing", level="WARNING"):
            self.assertEqual(pricing.price_run(10, 10, "fake"), 0)


# ── #10 seed_demo does not create an insecure admin in production ────────────

class SeedDemoSecurityTests(TestCase):
    """Regression: seed_demo always created admin/admin."""

    @override_settings(DEBUG=False)
    def test_no_insecure_admin_when_not_debug(self):
        with patch.dict("os.environ", {"DJANGO_SUPERUSER_PASSWORD": ""}, clear=False):
            call_command("seed_demo")
        admin = User.objects.filter(username="admin").first()
        # Either not created, or at least not usable with the 'admin' password.
        if admin is not None:
            self.assertFalse(admin.check_password("admin"))


# ── #11 Output guardrail scanning ────────────────────────────────────────────

class OutputGuardrailTests(TestCase):
    """Regression: guardrails never scanned model output."""

    def test_scan_output_detects_pii_and_audits(self):
        a = _agent("g-out", _bu())
        from controlplane.services.guardrails import guardrails
        before = AuditLog.objects.filter(action="guardrail.output_finding").count()
        findings = guardrails.scan_output(
            text="The customer SSN is 123-45-6789.", agent=a, actor="x", run_id="r"
        )
        self.assertTrue(findings)
        after = AuditLog.objects.filter(action="guardrail.output_finding").count()
        self.assertEqual(after, before + 1)

    def test_scan_output_clean_text(self):
        a = _agent("g-out2", _bu())
        from controlplane.services.guardrails import guardrails
        self.assertEqual(
            guardrails.scan_output(text="A normal helpful answer.", agent=a,
                                   actor="x", run_id="r"),
            [],
        )


@NO_SLEEP
class OutputGuardrailRuntimeTests(TestCase):

    def test_block_level_withholds_leaking_output(self):
        a = _agent("g-block", _bu(), guardrail="block")
        from controlplane.services.agent_runtime import PlatformAgentRuntime
        from controlplane.services.adapters.echo import EchoAdapter

        def _leak(self, run, message, history, meta):
            meta["output_text"] = "Sure — the customer SSN is 123-45-6789."
            meta["model_id"] = "echo"
            return
            yield  # pragma: no cover  (makes this a generator)

        with patch.object(EchoAdapter, "execute", _leak):
            "".join(PlatformAgentRuntime(agent=a, user_label="u").stream("benign question"))

        run = AgentRun.objects.filter(agent=a).latest("started_at")
        self.assertNotIn("123-45-6789", run.output_text)
        self.assertIn("withheld", run.output_text.lower())
