"""
Tests for M2 — BuildCompiler v2 / package build materialise tool bindings.

Covers:
  - create_bindings_from_plan: status, connector attach, dedup, never live
  - BuildCompiler.build creates sandbox bindings + sets agent.tool_names
  - built DRAFT agent runs tools in sandbox (dry-run) via the runtime toolset
  - PackageIngestor creates binding rows for the sandbox agent (never live)
  - idempotent rebuild
"""
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from controlplane.models import (
    Agent,
    AgentBlueprint,
    AgentToolBinding,
    DataConnector,
    ProcessInsight,
)
from controlplane.services.factory import blueprint_generator, build_compiler
from controlplane.services.tools.bindings import (
    create_bindings_from_plan,
    resolve_bindings,
    toolset_for,
)


def _agent(slug="a1", risk_tier=2):
    return Agent.objects.create(
        slug=slug, name="A", kind=Agent.Kind.CUSTOM, platform=Agent.Platform.DJANGO,
        business_unit="Finance", owner="x", technical_owner="x", purpose="p",
        system_prompt="p", status=Agent.Status.DRAFT, risk_tier=risk_tier,
    )


class CreateBindingsFromPlanTests(TestCase):

    def test_proposed_when_no_connector(self):
        agent = _agent()
        bindings = create_bindings_from_plan(agent, [{"name": "rest_connector", "target": "Unknown System"}])
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].binding_status, AgentToolBinding.Status.PROPOSED)

    def test_sandbox_when_connector_matches(self):
        DataConnector.objects.create(name="Finance DW", connector_type="sql", config={"url": "x"})
        agent = _agent()
        bindings = create_bindings_from_plan(agent, [{"name": "Finance DW", "target": "Finance DW"}])
        self.assertEqual(bindings[0].binding_status, AgentToolBinding.Status.SANDBOX)
        self.assertIsNotNone(bindings[0].connector)
        self.assertEqual(bindings[0].operation, "query")

    def test_never_creates_live(self):
        DataConnector.objects.create(name="Finance DW", connector_type="sql", config={"url": "x"})
        agent = _agent()
        bindings = create_bindings_from_plan(agent, [{"name": "Finance DW"}])
        self.assertNotEqual(bindings[0].binding_status, AgentToolBinding.Status.LIVE)

    def test_dedup_collapses_unique_tool_names(self):
        agent = _agent()
        bindings = create_bindings_from_plan(agent, [
            {"name": "rest_connector", "target": "SAP"},
            {"name": "rest_connector", "target": "SAP"},  # same → second suffixed
        ])
        names = [b.tool_name for b in bindings]
        self.assertEqual(len(set(names)), 2)

    def test_idempotent_rebuild(self):
        agent = _agent()
        create_bindings_from_plan(agent, [{"name": "rest_connector", "target": "SAP"}])
        create_bindings_from_plan(agent, [{"name": "rest_connector", "target": "SAP"}])
        self.assertEqual(AgentToolBinding.objects.filter(agent=agent).count(), 1)

    def test_dict_shape_tools_and_data_sources(self):
        agent = _agent()
        bindings = create_bindings_from_plan(agent, {"tools": ["sql_tool"], "data_sources": ["warehouse"]})
        self.assertEqual(len(bindings), 2)


class BuildCompilerBindingTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="builder", password="x")

    def _approved_blueprint(self, systems):
        insight = ProcessInsight.objects.create(
            source_reference=f"PI-{uuid.uuid4()}", process_name="Invoicing",
            finding_type="automation_opportunity", summary="s",
            systems_involved=systems,
        )
        bp = blueprint_generator.generate(insight)
        bp.status = AgentBlueprint.Status.APPROVED
        bp.approved_by = self.user
        bp.approved_at = timezone.now()
        bp.save()
        return bp

    def test_build_creates_sandbox_bindings(self):
        DataConnector.objects.create(name="postgres-db", connector_type="sql", config={"url": "x"})
        bp = self._approved_blueprint(["postgres-db"])
        agent = build_compiler.build(bp, built_by="builder")
        bindings = AgentToolBinding.objects.filter(agent=agent)
        self.assertGreaterEqual(bindings.count(), 1)
        # connector matched → sandbox; none are live
        self.assertTrue(any(b.binding_status == AgentToolBinding.Status.SANDBOX for b in bindings))
        self.assertFalse(any(b.binding_status == AgentToolBinding.Status.LIVE for b in bindings))

    def test_build_sets_tool_names_to_binding_names(self):
        bp = self._approved_blueprint(["rest-api"])
        agent = build_compiler.build(bp, built_by="builder")
        binding_names = set(AgentToolBinding.objects.filter(agent=agent).values_list("tool_name", flat=True))
        self.assertEqual(set(agent.tool_names), binding_names)

    def test_built_agent_runs_tools_in_sandbox(self):
        """A bound connector tool dry-runs (no external call) for the DRAFT agent."""
        DataConnector.objects.create(name="postgres-db", connector_type="sql", config={"url": "x"})
        bp = self._approved_blueprint(["postgres-db"])
        agent = build_compiler.build(bp, built_by="builder")
        specs, bindings = toolset_for(agent, "sandbox")
        # find the connector-backed tool
        name = next(iter(specs))
        from controlplane.services.tools.registry import ToolContext
        ctx = ToolContext(agent=agent, mode="sandbox", bindings=bindings)
        out = specs[name].handler({"sql": "SELECT 1"}, ctx)
        self.assertEqual(out["mode"], "sandbox")


class PackageBuildBindingTests(TestCase):

    def _package(self, **ov):
        from controlplane.tests.test_factory import _make_package
        return _make_package(**ov)

    def test_package_ingest_creates_binding_rows(self):
        from controlplane.services.package_ingestor import package_ingestor
        pkg = package_ingestor.ingest(self._package(), ingested_by="alice")
        bindings = AgentToolBinding.objects.filter(agent=pkg.sandbox_agent)
        self.assertGreaterEqual(bindings.count(), 1)

    def test_package_bindings_never_live(self):
        from controlplane.services.package_ingestor import package_ingestor
        pkg = package_ingestor.ingest(self._package(), ingested_by="alice")
        statuses = set(
            AgentToolBinding.objects.filter(agent=pkg.sandbox_agent)
            .values_list("binding_status", flat=True)
        )
        self.assertNotIn(AgentToolBinding.Status.LIVE, statuses)

    def test_package_agent_tool_names_match_bindings(self):
        from controlplane.services.package_ingestor import package_ingestor
        pkg = package_ingestor.ingest(self._package(), ingested_by="alice")
        binding_names = set(
            AgentToolBinding.objects.filter(agent=pkg.sandbox_agent).values_list("tool_name", flat=True)
        )
        self.assertEqual(set(pkg.sandbox_agent.tool_names), binding_names)
