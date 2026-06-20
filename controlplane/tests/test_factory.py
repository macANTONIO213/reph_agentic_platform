"""
Tests for Agent Factory — Phase F

Covers:
  - ProcessInsight ingest (create + upsert dedup)
  - OpportunityScorer dimensions
  - BlueprintGenerator derivations and status
  - Blueprint lifecycle transitions
  - Approval gate
  - BuildCompiler → Agent creation
  - API endpoints (insights, blueprints, approve, build)
"""
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentBlueprint,
    AgentFactoryPackage,
    AuditLog,
    BusinessUnit,
    EvalSuite,
    ProcessInsight,
    TelemetryEvent,
)
from controlplane.services.factory import (
    BlueprintGenerator,
    BuildCompiler,
    OpportunityScorer,
    blueprint_generator,
    build_compiler,
    opportunity_scorer,
)
from controlplane.services.package_ingestor import (
    PackageValidationError,
    package_ingestor,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_insight(**kwargs) -> ProcessInsight:
    defaults = dict(
        source_reference="PI-001",
        process_name="Invoice Processing",
        finding_type="automation_opportunity",
        summary="High-volume manual invoice matching can be automated.",
        impact="Reduce processing time by 60%.",
        frequency="500 invoices/day",
        systems_involved=["SAP", "Oracle Financials"],
        recommended_action="Deploy invoice matching agent.",
        risk_notes="",
    )
    defaults.update(kwargs)
    return ProcessInsight.objects.create(**defaults)


def _make_blueprint(insight=None, **kwargs) -> AgentBlueprint:
    if insight is None:
        insight = _make_insight(source_reference=f"PI-{uuid.uuid4()}")
    return blueprint_generator.generate(insight)


# ── OpportunityScorer ─────────────────────────────────────────────────────────

class OpportunityScorerTests(TestCase):

    def setUp(self):
        self.scorer = OpportunityScorer()

    def _insight(self, **kw):
        defaults = dict(
            finding_type="automation_opportunity",
            impact="Major revenue impact",
            frequency="daily",
            summary="test",
            systems_involved=["SystemA"],
            risk_notes="",
        )
        defaults.update(kw)
        # Use a plain object (not DB) to keep scorer tests fast
        class FakeInsight:
            pass
        obj = FakeInsight()
        for k, v in defaults.items():
            setattr(obj, k, v)
        return obj

    def test_automation_fit_automation_opportunity(self):
        scores = self.scorer.score(self._insight(finding_type="automation_opportunity"))
        self.assertEqual(scores["automation_fit_score"], 9)

    def test_automation_fit_bottleneck(self):
        scores = self.scorer.score(self._insight(finding_type="bottleneck"))
        self.assertEqual(scores["automation_fit_score"], 7)

    def test_automation_fit_other(self):
        scores = self.scorer.score(self._insight(finding_type="other"))
        self.assertEqual(scores["automation_fit_score"], 4)

    def test_complexity_scales_with_systems(self):
        s1 = self.scorer.score(self._insight(systems_involved=["A"]))
        s3 = self.scorer.score(self._insight(systems_involved=["A", "B", "C"]))
        self.assertLess(s1["complexity_score"], s3["complexity_score"])

    def test_complexity_capped_at_9(self):
        scores = self.scorer.score(self._insight(systems_involved=["A"] * 20))
        self.assertLessEqual(scores["complexity_score"], 9)

    def test_business_value_elevated_by_keywords(self):
        low  = self.scorer.score(self._insight(impact="minor issue"))
        high = self.scorer.score(self._insight(impact="critical revenue compliance sla"))
        self.assertGreater(high["business_value_score"], low["business_value_score"])

    def test_risk_score_elevated_by_keywords(self):
        low  = self.scorer.score(self._insight(risk_notes=""))
        high = self.scorer.score(self._insight(risk_notes="gdpr personal data financial regulated"))
        self.assertGreater(high["risk_score"], low["risk_score"])

    def test_opportunity_score_formula(self):
        scores = self.scorer.score(self._insight())
        expected = round(
            scores["business_value_score"] * 0.35
            + scores["automation_fit_score"] * 0.35
            + (10 - scores["complexity_score"]) * 0.20
            + (10 - scores["risk_score"]) * 0.10,
            2,
        )
        self.assertAlmostEqual(scores["opportunity_score"], expected, places=2)

    def test_risk_level_low(self):
        from controlplane.services.factory import BlueprintGenerator
        gen = BlueprintGenerator()
        self.assertEqual(gen._derive_risk_level(2), "low")

    def test_risk_level_medium(self):
        from controlplane.services.factory import BlueprintGenerator
        gen = BlueprintGenerator()
        self.assertEqual(gen._derive_risk_level(5), "medium")

    def test_risk_level_high(self):
        from controlplane.services.factory import BlueprintGenerator
        gen = BlueprintGenerator()
        self.assertEqual(gen._derive_risk_level(7), "high")

    def test_risk_level_blocked(self):
        from controlplane.services.factory import BlueprintGenerator
        gen = BlueprintGenerator()
        self.assertEqual(gen._derive_risk_level(10), "blocked")


# ── BlueprintGenerator ────────────────────────────────────────────────────────

class BlueprintGeneratorTests(TestCase):

    def test_generate_creates_blueprint(self):
        insight = _make_insight()
        bp = blueprint_generator.generate(insight)
        self.assertIsInstance(bp, AgentBlueprint)
        self.assertEqual(bp.insight, insight)

    def test_agent_name_contains_process_name(self):
        insight = _make_insight(process_name="Claims Review")
        bp = blueprint_generator.generate(insight)
        self.assertIn("Claims Review", bp.agent_name)

    def test_mission_contains_recommended_action(self):
        insight = _make_insight(recommended_action="Deploy claims agent.")
        bp = blueprint_generator.generate(insight)
        self.assertIn("Deploy claims agent.", bp.mission)

    def test_scores_persisted(self):
        insight = _make_insight()
        bp = blueprint_generator.generate(insight)
        self.assertGreater(bp.opportunity_score, 0)
        self.assertGreater(bp.automation_fit_score, 0)

    def test_workflow_steps_generated(self):
        insight = _make_insight()
        bp = blueprint_generator.generate(insight)
        self.assertIsInstance(bp.workflow_steps, list)
        self.assertGreater(len(bp.workflow_steps), 0)

    def test_tools_derived_from_systems(self):
        insight = _make_insight(systems_involved=["postgres-db", "rest-api"])
        bp = blueprint_generator.generate(insight)
        tool_names = [t["name"] for t in bp.tools]
        self.assertIn("sql_connector", tool_names)
        self.assertIn("rest_connector", tool_names)

    def test_status_needs_tool_when_enterprise_system(self):
        insight = _make_insight(systems_involved=["SAP", "Salesforce"])
        bp = blueprint_generator.generate(insight)
        self.assertEqual(bp.status, AgentBlueprint.Status.NEEDS_TOOL)
        self.assertTrue(len(bp.missing_tools) > 0)

    def test_status_needs_data_when_no_systems(self):
        insight = _make_insight(systems_involved=[], finding_type="bottleneck")
        bp = blueprint_generator.generate(insight)
        self.assertEqual(bp.status, AgentBlueprint.Status.NEEDS_DATA)

    def test_status_draft_when_no_missing(self):
        insight = _make_insight(systems_involved=["rest-api"], finding_type="other")
        bp = blueprint_generator.generate(insight)
        # No enterprise system → no missing_tools; has systems → no missing_data
        self.assertNotIn(bp.status, (
            AgentBlueprint.Status.NEEDS_TOOL,
            AgentBlueprint.Status.NEEDS_DATA,
        ))

    def test_version_increments(self):
        insight = _make_insight()
        bp1 = blueprint_generator.generate(insight)
        bp2 = blueprint_generator.generate(insight)
        self.assertEqual(bp1.version, 1)
        self.assertEqual(bp2.version, 2)

    def test_high_risk_gets_approval_points(self):
        insight = _make_insight(risk_notes="gdpr financial regulated personal data audit")
        bp = blueprint_generator.generate(insight)
        # High risk_level means approval points should be non-empty
        if bp.risk_level in ("high", "blocked"):
            self.assertGreater(len(bp.human_approval_points), 0)

    def test_guardrails_always_include_audit_log(self):
        insight = _make_insight()
        bp = blueprint_generator.generate(insight)
        rules = [g["rule"] for g in bp.guardrails]
        self.assertIn("audit_log", rules)


# ── Blueprint lifecycle transitions ──────────────────────────────────────────

class BlueprintTransitionTests(TestCase):

    def test_draft_can_approve(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.DRAFT
        bp.save()
        self.assertTrue(bp.can_transition_to(AgentBlueprint.Status.APPROVED))

    def test_approved_can_build(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.save()
        self.assertTrue(bp.can_transition_to(AgentBlueprint.Status.BUILT))

    def test_built_can_deploy(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.BUILT
        bp.save()
        self.assertTrue(bp.can_transition_to(AgentBlueprint.Status.DEPLOYED))

    def test_deployed_can_retire(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.DEPLOYED
        bp.save()
        self.assertTrue(bp.can_transition_to(AgentBlueprint.Status.RETIRED))

    def test_retired_cannot_transition(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.RETIRED
        bp.save()
        for status in AgentBlueprint.Status.values:
            self.assertFalse(bp.can_transition_to(status))

    def test_built_cannot_approve(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.BUILT
        bp.save()
        self.assertFalse(bp.can_transition_to(AgentBlueprint.Status.APPROVED))


# ── BuildCompiler ─────────────────────────────────────────────────────────────

class BuildCompilerTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="builder", password="x")

    def _approved_blueprint(self, **kw) -> AgentBlueprint:
        insight = _make_insight(systems_involved=["rest-api"], **kw)
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.approved_by = self.user
        bp.approved_at = timezone.now()
        bp.save()
        return bp

    def test_build_creates_agent(self):
        bp = self._approved_blueprint()
        agent = build_compiler.build(bp, built_by="builder")
        self.assertIsInstance(agent, Agent)
        self.assertEqual(agent.name, bp.agent_name)

    def test_build_sets_agent_draft(self):
        bp = self._approved_blueprint()
        agent = build_compiler.build(bp, built_by="builder")
        self.assertEqual(agent.status, Agent.Status.DRAFT)

    def test_build_links_agent_to_blueprint(self):
        bp = self._approved_blueprint()
        agent = build_compiler.build(bp, built_by="builder")
        bp.refresh_from_db()
        self.assertEqual(bp.built_agent, agent)
        self.assertEqual(bp.status, AgentBlueprint.Status.BUILT)

    def test_build_creates_audit_log(self):
        bp = self._approved_blueprint()
        build_compiler.build(bp, built_by="builder")
        log = AuditLog.objects.filter(action="blueprint_built").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, "builder")

    def test_build_raises_if_not_approved(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        with self.assertRaises(ValueError):
            build_compiler.build(bp, built_by="builder")

    def test_build_raises_if_blocked(self):
        bp = self._approved_blueprint()
        bp.risk_level = AgentBlueprint.RiskLevel.BLOCKED
        bp.save()
        with self.assertRaises(ValueError):
            build_compiler.build(bp, built_by="builder")

    def test_agent_slug_unique(self):
        bp1 = self._approved_blueprint()
        bp2 = self._approved_blueprint(source_reference="PI-999")
        agent1 = build_compiler.build(bp1, built_by="builder")
        agent2 = build_compiler.build(bp2, built_by="builder")
        self.assertNotEqual(agent1.slug, agent2.slug)

    def test_agent_risk_tier_derived_from_risk_level(self):
        bp = self._approved_blueprint(risk_notes="")
        bp.risk_level = AgentBlueprint.RiskLevel.HIGH
        bp.save()
        agent = build_compiler.build(bp, built_by="builder")
        self.assertEqual(agent.risk_tier, 3)

    def test_agent_business_unit_from_insight(self):
        bu = BusinessUnit.objects.create(name="Finance", code="fin")
        insight = _make_insight(
            source_reference="PI-BU",
            systems_involved=["rest-api"],
            business_unit=bu,  # this is a FK — won't work directly; need create
        )
        # Re-create with FK
        insight.business_unit = bu
        insight.save()
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.approved_by = self.user
        bp.approved_at = timezone.now()
        bp.save()
        agent = build_compiler.build(bp, built_by="builder")
        self.assertEqual(agent.business_unit, "Finance")


# ── API tests ─────────────────────────────────────────────────────────────────

class FactoryInsightAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(username="admin", password="admin")
        self.client.login(username="admin", password="admin")

    def _post_insight(self, **overrides):
        payload = {
            "source_reference":   "PI-API-001",
            "process_name":       "Expense Approval",
            "finding_type":       "automation_opportunity",
            "summary":            "Manual expense approval takes 3 days on average.",
            "impact":             "Cost savings of £200k/year",
            "frequency":          "200 requests/week",
            "systems_involved":   ["Workday", "SAP"],
            "recommended_action": "Automate pre-approval for low-value claims",
            "risk_notes":         "",
        }
        payload.update(overrides)
        return self.client.post(
            "/api/v1/factory/insights/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_ingest_creates_insight(self):
        resp = self._post_insight()
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["process_name"], "Expense Approval")

    def test_ingest_deduplicates_on_source_reference(self):
        self._post_insight()
        resp = self._post_insight(summary="Updated summary")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], "Updated summary")
        self.assertEqual(ProcessInsight.objects.count(), 1)

    def test_ingest_requires_source_reference(self):
        resp = self._post_insight(source_reference="")
        self.assertEqual(resp.status_code, 400)

    def test_ingest_requires_process_name(self):
        resp = self._post_insight(process_name="")
        self.assertEqual(resp.status_code, 400)

    def test_list_insights(self):
        _make_insight()
        resp = self.client.get("/api/v1/factory/insights/")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()["insights"]), 1)

    def test_list_filter_by_finding_type(self):
        _make_insight(source_reference="PI-A", finding_type="bottleneck")
        _make_insight(source_reference="PI-B", finding_type="exception")
        resp = self.client.get("/api/v1/factory/insights/?finding_type=bottleneck")
        results = resp.json()["insights"]
        self.assertTrue(all(i["finding_type"] == "bottleneck" for i in results))

    def test_get_insight_detail(self):
        insight = _make_insight()
        resp = self.client.get(f"/api/v1/factory/insights/{insight.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], str(insight.id))

    def test_patch_insight(self):
        insight = _make_insight()
        resp = self.client.patch(
            f"/api/v1/factory/insights/{insight.id}/",
            data=json.dumps({"summary": "Updated via PATCH"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], "Updated via PATCH")

    def test_insight_not_found_returns_404(self):
        resp = self.client.get(f"/api/v1/factory/insights/{uuid.uuid4()}/")
        self.assertEqual(resp.status_code, 404)

    def test_generate_blueprint_from_insight(self):
        insight = _make_insight()
        resp = self.client.post(f"/api/v1/factory/insights/{insight.id}/generate-blueprint/")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("agent_name", data)
        self.assertIn("opportunity_score", data)
        self.assertEqual(data["insight_id"], str(insight.id))

    def test_generate_blueprint_404_for_missing_insight(self):
        resp = self.client.post(f"/api/v1/factory/insights/{uuid.uuid4()}/generate-blueprint/")
        self.assertEqual(resp.status_code, 404)

    def test_blueprint_count_in_insight_dict(self):
        insight = _make_insight()
        resp = self.client.post(f"/api/v1/factory/insights/{insight.id}/generate-blueprint/")
        self.assertEqual(resp.status_code, 201)
        resp2 = self.client.get(f"/api/v1/factory/insights/{insight.id}/")
        self.assertEqual(resp2.json()["blueprint_count"], 1)


class FactoryBlueprintAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(username="admin", password="admin")
        self.client.login(username="admin", password="admin")

    def _make_approved_bp(self) -> AgentBlueprint:
        insight = _make_insight(
            source_reference=f"PI-{uuid.uuid4()}",
            systems_involved=["rest-api"],
        )
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.approved_by = self.user
        bp.approved_at = timezone.now()
        bp.save()
        return bp

    def test_list_blueprints(self):
        _make_blueprint()
        resp = self.client.get("/api/v1/factory/blueprints/")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()["blueprints"]), 1)

    def test_list_filter_by_status(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.approved_by = self.user
        bp.approved_at = timezone.now()
        bp.save()
        resp = self.client.get("/api/v1/factory/blueprints/?status=approved")
        statuses = [b["status"] for b in resp.json()["blueprints"]]
        self.assertTrue(all(s == "approved" for s in statuses))

    def test_get_blueprint_detail(self):
        bp = _make_blueprint()
        resp = self.client.get(f"/api/v1/factory/blueprints/{bp.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], str(bp.id))

    def test_patch_blueprint_in_draft(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.DRAFT
        bp.save()
        resp = self.client.patch(
            f"/api/v1/factory/blueprints/{bp.id}/",
            data=json.dumps({"agent_name": "Updated Agent Name"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["agent_name"], "Updated Agent Name")

    def test_patch_approved_blueprint_rejected(self):
        bp = self._make_approved_bp()
        resp = self.client.patch(
            f"/api/v1/factory/blueprints/{bp.id}/",
            data=json.dumps({"agent_name": "Should fail"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_clears_missing_requirements_updates_status(self):
        insight = _make_insight(systems_involved=["SAP"])
        bp = blueprint_generator.generate(insight)
        self.assertEqual(bp.status, AgentBlueprint.Status.NEEDS_TOOL)

        resp = self.client.patch(
            f"/api/v1/factory/blueprints/{bp.id}/",
            data=json.dumps({"missing_tools": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], AgentBlueprint.Status.DRAFT)

    def test_approve_blueprint(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.DRAFT
        bp.save()

        resp = self.client.post(
            f"/api/v1/factory/blueprints/{bp.id}/approve/",
            data=json.dumps({"notes": "LGTM"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["approved_by"], "admin")
        self.assertEqual(data["approval_notes"], "LGTM")

    def test_approve_already_approved_returns_400(self):
        bp = self._make_approved_bp()
        resp = self.client.post(f"/api/v1/factory/blueprints/{bp.id}/approve/")
        self.assertEqual(resp.status_code, 400)

    def test_approve_creates_audit_log(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.DRAFT
        bp.save()
        self.client.post(f"/api/v1/factory/blueprints/{bp.id}/approve/")
        log = AuditLog.objects.filter(action="blueprint_approved").first()
        self.assertIsNotNone(log)

    def test_build_approved_blueprint(self):
        bp = self._make_approved_bp()
        resp = self.client.post(f"/api/v1/factory/blueprints/{bp.id}/build/")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("agent", data)
        self.assertEqual(data["blueprint"]["status"], "built")
        agent_id = data["agent"]["id"]
        self.assertTrue(Agent.objects.filter(id=agent_id).exists())

    def test_build_non_approved_returns_400(self):
        insight = _make_insight(systems_involved=["rest-api"])
        bp = blueprint_generator.generate(insight)
        resp = self.client.post(f"/api/v1/factory/blueprints/{bp.id}/build/")
        self.assertEqual(resp.status_code, 400)

    def test_blueprint_not_found_returns_404(self):
        resp = self.client.get(f"/api/v1/factory/blueprints/{uuid.uuid4()}/")
        self.assertEqual(resp.status_code, 404)

    def test_full_lifecycle(self):
        # Ingest → generate → approve → build
        resp = self.client.post(
            "/api/v1/factory/insights/",
            data=json.dumps({
                "source_reference":   "PI-LIFECYCLE",
                "process_name":       "HR Onboarding",
                "finding_type":       "automation_opportunity",
                "summary":            "Manual HR onboarding takes 2 weeks.",
                "systems_involved":   ["rest-api"],
                "recommended_action": "Automate document collection and account provisioning",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        insight_id = resp.json()["id"]

        resp = self.client.post(f"/api/v1/factory/insights/{insight_id}/generate-blueprint/")
        self.assertEqual(resp.status_code, 201)
        bp_id = resp.json()["id"]

        # Patch to DRAFT if needed
        bp = AgentBlueprint.objects.get(id=bp_id)
        if bp.status != AgentBlueprint.Status.DRAFT:
            bp.status = AgentBlueprint.Status.DRAFT
            bp.missing_tools = []
            bp.missing_data = []
            bp.save()

        resp = self.client.post(
            f"/api/v1/factory/blueprints/{bp_id}/approve/",
            data=json.dumps({"notes": "Full lifecycle test approval"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(f"/api/v1/factory/blueprints/{bp_id}/build/")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["blueprint"]["status"], "built")
        self.assertIsNotNone(resp.json()["agent"]["id"])


# ── Agent Factory Package ingestion ───────────────────────────────────────────

def _make_package(**overrides) -> dict:
    """Build a complete, valid agent_factory_package dict."""
    pkg = {
        "package_id":      "AFP-2026-001",
        "blueprint_id":    "BP-2026-077",
        "package_version": "agent-factory-package-v1",
        "package_type":    "sandbox_agent_build",
        "source": {
            "process_insight": {
                "source_reference":   "PI-PKG-001",
                "process_name":       "Invoice Matching",
                "finding_type":       "automation_opportunity",
                "summary":            "Manual 3-way invoice matching is slow.",
                "impact":             "Reduce cycle time 60%",
                "frequency":          "500/day",
                "systems_involved":   ["SAP", "Oracle"],
                "recommended_action": "Automate matching",
                "risk_notes":         "Financial data",
                "business_unit":      "Finance",
            },
            "process_intelligence_output": {"normalized": True, "score": 0.82},
        },
        "agent_blueprint": {
            "agent_name":     "Invoice Matching Agent",
            "purpose":        "Automate 3-way invoice matching.",
            "users":          ["AP clerks"],
            "business_value": 8,
            "risk_tier":      2,
            "business_unit":  "Finance",
        },
        "agent_build_manifest": {
            "runtime":  "django",
            "trigger":  "Inbound invoice event",
            "inputs":   [{"source": "SAP"}],
            "outputs":  [{"artifact": "match_result"}],
            "workflow_steps": [
                {"step": "ingest", "description": "Receive invoice"},
                {"step": "match",  "description": "3-way match"},
            ],
        },
        "tool_binding_plan": [
            {"name": "sap_connector", "kind": "tool", "binding_status": "available"},
            {"name": "oracle_db",     "kind": "data", "binding_status": "pending"},
        ],
        "decision_policy": {
            "rules": [{"rule": "audit_log", "description": "Log all actions"}],
            "human_approval_points": [{"step": "match", "description": "Review mismatches"}],
        },
        "evaluation_pack": {
            "test_cases": [
                {"name": "Happy path", "input": "Match invoice 123", "expected_keywords": ["matched"]},
                {"name": "Mismatch",   "input": "Match invoice 999", "must_not_contain": ["error"]},
            ],
            "confidence": 0.8,
        },
        "approval_route": {"name": "Finance risk board", "type": "policy"},
        "approval_progress": {"state": "pending"},
        "telemetry_contract": {"events": ["run_started", "run_completed"]},
        "telemetry_feedback_plan": {"feeds": "blueprint"},
        "safety_boundary": {
            "can_build_sandbox_agent":           True,
            "can_bind_production_tools":         False,
            "can_deploy_to_production":          False,
            "requires_human_or_policy_approval": True,
        },
    }
    pkg.update(overrides)
    return pkg


class PackageIngestorServiceTests(TestCase):

    def test_ingest_valid_package_creates_record(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertIsInstance(pkg, AgentFactoryPackage)
        self.assertEqual(pkg.package_id, "AFP-2026-001")
        self.assertEqual(pkg.external_blueprint_id, "BP-2026-077")
        self.assertTrue(pkg.validation_report["ok"])

    def test_accepts_wrapped_payload(self):
        pkg = package_ingestor.ingest({"agent_factory_package": _make_package()}, ingested_by="alice")
        self.assertEqual(pkg.package_id, "AFP-2026-001")

    def test_creates_sandbox_agent_draft(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertIsNotNone(pkg.sandbox_agent)
        self.assertEqual(pkg.sandbox_agent.status, Agent.Status.DRAFT)
        self.assertEqual(pkg.status, AgentFactoryPackage.Status.SANDBOX_CREATED)

    def test_traceability_links_to_source_insight(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertIsNotNone(pkg.insight)
        self.assertEqual(pkg.insight.source_reference, "PI-PKG-001")
        self.assertIsNotNone(pkg.blueprint)
        self.assertEqual(pkg.blueprint.insight, pkg.insight)

    def test_bindings_are_proposed_never_live(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        for b in pkg.tool_binding_plan:
            self.assertFalse(b["live"])
            self.assertNotIn(b["binding_status"], ("live", "production", "active", "connected"))
        # No live data sources bound on the sandbox agent.
        self.assertEqual(pkg.sandbox_agent.data_sources, [])

    def test_live_binding_status_downgraded_to_proposed(self):
        p = _make_package()
        p["tool_binding_plan"] = [{"name": "prod_db", "binding_status": "live"}]
        pkg = package_ingestor.ingest(p, ingested_by="alice")
        self.assertEqual(pkg.tool_binding_plan[0]["binding_status"], "proposed")

    def test_risk_tier_assigned_from_blueprint(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertEqual(pkg.risk_tier, 2)
        self.assertEqual(pkg.sandbox_agent.risk_tier, 2)

    def test_eval_cases_generated(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        suite = EvalSuite.objects.filter(agent=pkg.sandbox_agent).first()
        self.assertIsNotNone(suite)
        self.assertEqual(suite.cases.count(), 2)

    def test_telemetry_emitted(self):
        package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertTrue(
            TelemetryEvent.objects.filter(event_type="factory_package_ingested").exists()
        )

    def test_audit_log_written(self):
        package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertTrue(
            AuditLog.objects.filter(action="factory_package_ingested").exists()
        )

    def test_blueprint_not_auto_approved(self):
        pkg = package_ingestor.ingest(_make_package(), ingested_by="alice")
        self.assertNotEqual(pkg.blueprint.status, AgentBlueprint.Status.APPROVED)
        self.assertIsNone(pkg.blueprint.approved_by)

    def test_no_sandbox_when_safety_boundary_forbids(self):
        p = _make_package()
        p["safety_boundary"]["can_build_sandbox_agent"] = False
        pkg = package_ingestor.ingest(p, ingested_by="alice")
        self.assertIsNone(pkg.sandbox_agent)
        self.assertEqual(pkg.status, AgentFactoryPackage.Status.RECEIVED)

    def test_empty_safety_boundary_rejected_as_critical(self):
        p = _make_package()
        p["safety_boundary"] = {}  # empty section is untrusted → rejected
        with self.assertRaises(PackageValidationError) as cm:
            package_ingestor.ingest(p, ingested_by="alice")
        self.assertIn("safety_boundary", cm.exception.report["missing_sections"])

    def test_safety_boundary_defaults_restrictive_when_missing_flags(self):
        p = _make_package()
        # Section present but individual flags omitted → restrictive defaults.
        p["safety_boundary"] = {"note": "flags omitted on purpose"}
        pkg = package_ingestor.ingest(p, ingested_by="alice")
        self.assertFalse(pkg.can_build_sandbox_agent)
        self.assertFalse(pkg.can_bind_production_tools)
        self.assertFalse(pkg.can_deploy_to_production)
        self.assertTrue(pkg.requires_human_or_policy_approval)

    def test_invalid_version_raises(self):
        p = _make_package(package_version="agent-factory-package-v2")
        with self.assertRaises(PackageValidationError) as cm:
            package_ingestor.ingest(p, ingested_by="alice")
        self.assertFalse(cm.exception.report["ok"])
        self.assertTrue(any("package_version" in e for e in cm.exception.report["errors"]))

    def test_invalid_type_raises(self):
        p = _make_package(package_type="production_agent_build")
        with self.assertRaises(PackageValidationError):
            package_ingestor.ingest(p, ingested_by="alice")

    def test_missing_critical_section_raises(self):
        p = _make_package()
        del p["agent_build_manifest"]
        with self.assertRaises(PackageValidationError) as cm:
            package_ingestor.ingest(p, ingested_by="alice")
        self.assertIn("agent_build_manifest", cm.exception.report["missing_sections"])

    def test_missing_optional_section_warns_but_ingests(self):
        p = _make_package()
        del p["telemetry_feedback_plan"]
        pkg = package_ingestor.ingest(p, ingested_by="alice")
        self.assertTrue(pkg.validation_report["ok"])
        self.assertIn("telemetry_feedback_plan", pkg.validation_report["missing_sections"])

    def test_reingest_same_package_id_updates(self):
        package_ingestor.ingest(_make_package(), ingested_by="alice")
        package_ingestor.ingest(_make_package(blueprint_id="BP-NEW"), ingested_by="bob")
        self.assertEqual(AgentFactoryPackage.objects.filter(package_id="AFP-2026-001").count(), 1)
        pkg = AgentFactoryPackage.objects.get(package_id="AFP-2026-001")
        self.assertEqual(pkg.external_blueprint_id, "BP-NEW")


class PackageIngestAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(username="admin", password="admin")
        self.client.login(username="admin", password="admin")

    def _post(self, payload):
        return self.client.post(
            "/api/v1/factory/packages/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_ingest_endpoint_creates_package(self):
        resp = self._post(_make_package())
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["package_id"], "AFP-2026-001")
        self.assertIsNotNone(data["sandbox_agent_id"])
        self.assertEqual(data["sandbox_agent_status"], "draft")

    def test_ingest_endpoint_reports_validation_errors(self):
        resp = self._post(_make_package(package_version="bad"))
        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertIn("validation_report", data)
        self.assertFalse(data["validation_report"]["ok"])

    def test_safety_flags_visible_in_response(self):
        data = self._post(_make_package()).json()
        self.assertTrue(data["can_build_sandbox_agent"])
        self.assertFalse(data["can_deploy_to_production"])
        self.assertTrue(data["requires_human_or_policy_approval"])
        self.assertIn("approval_route", data)

    def test_list_packages(self):
        self._post(_make_package())
        resp = self.client.get("/api/v1/factory/packages/")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()["packages"]), 1)

    def test_package_detail(self):
        pkg_id = self._post(_make_package()).json()["id"]
        resp = self.client.get(f"/api/v1/factory/packages/{pkg_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], pkg_id)

    def test_requires_login(self):
        self.client.logout()
        resp = self._post(_make_package())
        self.assertIn(resp.status_code, (302, 403))
