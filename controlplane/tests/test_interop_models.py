"""
Phase 1 step 1 tests — MCP/A2A interop data model.

Covers the model-level guarantees introduced with RemoteMcpServer, the
AgentToolBinding either/or target invariant, and the AgentCard projection.
Behavioural wiring (mcp_client, card projection, A2A endpoints) lands in later
Phase 1 steps.
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from controlplane.models import (
    Agent,
    AgentCard,
    AgentToolBinding,
    BusinessUnit,
    DataConnector,
    RemoteMcpServer,
)


def _make_agent(slug="interop-agent", tier=2, status=Agent.Status.PILOT):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        purpose="Test agent",
        business_unit="Engineering",
        risk_tier=tier,
        status=status,
        platform="django_runtime",
    )


class RemoteMcpServerTests(TestCase):
    def test_defaults_and_str(self):
        srv = RemoteMcpServer.objects.create(name="Acme MCP", base_url="https://mcp.acme.test")
        self.assertEqual(srv.status, RemoteMcpServer.Status.REGISTERED)
        self.assertEqual(srv.transport, RemoteMcpServer.Transport.HTTP)
        self.assertEqual(srv.source, "manual")
        self.assertFalse(srv.is_usable)  # no catalog yet
        self.assertIn("MCP:registered", str(srv))

    def test_is_usable_requires_active_and_catalog(self):
        srv = RemoteMcpServer.objects.create(
            name="Acme MCP", base_url="https://mcp.acme.test",
            status=RemoteMcpServer.Status.ACTIVE,
            tool_catalog=[{"name": "search", "input_schema": {"type": "object"}}],
        )
        self.assertTrue(srv.is_usable)
        srv.is_active = False
        self.assertFalse(srv.is_usable)

    def test_tool_schema_lookup(self):
        srv = RemoteMcpServer.objects.create(
            name="Acme MCP", base_url="https://mcp.acme.test",
            tool_catalog=[{"name": "search", "input_schema": {"type": "object", "x": 1}}],
        )
        self.assertEqual(srv.tool_schema("search"), {"type": "object", "x": 1})
        self.assertIsNone(srv.tool_schema("missing"))

    def test_name_unique_per_business_unit(self):
        bu = BusinessUnit.objects.create(name="Eng", code="ENG")
        RemoteMcpServer.objects.create(name="Shared", base_url="https://a.test", business_unit=bu)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RemoteMcpServer.objects.create(
                    name="Shared", base_url="https://b.test", business_unit=bu,
                )


class AgentToolBindingTargetInvariantTests(TestCase):
    def setUp(self):
        self.agent = _make_agent()
        self.connector = DataConnector.objects.create(name="warehouse", connector_type="sql")
        self.mcp = RemoteMcpServer.objects.create(name="Acme MCP", base_url="https://mcp.acme.test")

    def test_proposed_binding_may_have_no_target(self):
        b = AgentToolBinding(agent=self.agent, tool_name="t1",
                             binding_status=AgentToolBinding.Status.PROPOSED)
        b.clean()  # should not raise
        self.assertEqual(b.target_kind, "none")

    def test_connector_only_is_valid(self):
        b = AgentToolBinding(agent=self.agent, tool_name="t2",
                             binding_status=AgentToolBinding.Status.SANDBOX,
                             connector=self.connector)
        b.clean()
        self.assertEqual(b.target_kind, "connector")

    def test_mcp_only_is_valid(self):
        b = AgentToolBinding(agent=self.agent, tool_name="t3",
                             binding_status=AgentToolBinding.Status.SANDBOX,
                             mcp_server=self.mcp, mcp_tool_name="search")
        b.clean()
        self.assertEqual(b.target_kind, "mcp")

    def test_dual_target_rejected(self):
        b = AgentToolBinding(agent=self.agent, tool_name="t4",
                             binding_status=AgentToolBinding.Status.SANDBOX,
                             connector=self.connector, mcp_server=self.mcp)
        with self.assertRaises(ValidationError):
            b.clean()

    def test_non_proposed_without_target_rejected(self):
        b = AgentToolBinding(agent=self.agent, tool_name="t5",
                             binding_status=AgentToolBinding.Status.LIVE)
        with self.assertRaises(ValidationError):
            b.clean()


class AgentCardTests(TestCase):
    def test_one_to_one_and_unpublished_default(self):
        agent = _make_agent()
        card = AgentCard.objects.create(agent=agent, card_json={"name": agent.name})
        self.assertFalse(card.is_published)
        self.assertEqual(agent.a2a_card, card)
        self.assertIn("unpublished", str(card))
