"""
Phase 2 step 1 tests — federated registry (RegistryEntry + projection + API).

Covers projection of first-party agents (via publish) and MCP servers into the
unified catalog, deactivation on unpublish/disable, sync_all backfill, and the
list/detail/sync API.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase

from controlplane.models import (
    Agent, BusinessUnit, RegistryEntry, RemoteMcpServer,
)
from controlplane.services.interop import federation
from controlplane.services.interop.a2a_cards import publish_card, unpublish_card


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug="reg-agent", bu=None, tools=None, status=Agent.Status.PILOT):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose="Answers questions",
        business_unit=(bu.name if bu else "Engineering"), owner="o", technical_owner="o",
        system_prompt="s", platform="django_runtime", status=status, risk_tier=2,
        org_unit=bu, tool_names=tools or [],
    )


def _server(name="Acme MCP", usable=True, bu=None):
    kw = dict(name=name, base_url="https://mcp.acme.test/rpc", business_unit=bu)
    if usable:
        kw.update(status=RemoteMcpServer.Status.ACTIVE,
                  tool_catalog=[{"name": "search", "description": "Search", "input_schema": {}}])
    return RemoteMcpServer.objects.create(**kw)


def _user(username, *, role=None, bu=None):
    u, _ = User.objects.get_or_create(username=username)
    if role or bu:
        p = u.profile
        if bu:
            p.business_unit = bu
        if role:
            p.role = role
        p.save()
    return u


# ── projection ──────────────────────────────────────────────────────────────────

class ProjectionTests(TestCase):
    def setUp(self):
        self.bu = _bu()

    def test_publishing_agent_projects_entry(self):
        agent = _agent(bu=self.bu, tools=["registry_search"])
        publish_card(agent, base_url="https://platform.test")  # auto-projects
        entry = RegistryEntry.objects.get(kind=RegistryEntry.Kind.FIRST_PARTY_AGENT, identifier=agent.slug)
        self.assertTrue(entry.is_active)
        self.assertEqual(entry.protocol, "a2a")
        self.assertEqual(entry.endpoint_url, "https://platform.test/a2a/agents/reg-agent/rpc/")
        self.assertEqual({c["id"] for c in entry.capabilities}, {"registry_search"})
        self.assertEqual(entry.governance["risk_tier"], 2)

    def test_unpublishing_deactivates_entry(self):
        agent = _agent(bu=self.bu)
        publish_card(agent)
        unpublish_card(agent)
        entry = RegistryEntry.objects.get(kind=RegistryEntry.Kind.FIRST_PARTY_AGENT, identifier=agent.slug)
        self.assertFalse(entry.is_active)

    def test_project_usable_mcp_server(self):
        server = _server(bu=self.bu)
        entry = federation.project_mcp_server(server)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.kind, RegistryEntry.Kind.MCP_SERVER)
        self.assertEqual(entry.protocol, "mcp")
        self.assertEqual({c["id"] for c in entry.capabilities}, {"search"})

    def test_project_unusable_mcp_server_returns_none(self):
        server = _server(usable=False, bu=self.bu)  # REGISTERED, no catalog
        self.assertIsNone(federation.project_mcp_server(server))
        self.assertFalse(
            RegistryEntry.objects.filter(kind=RegistryEntry.Kind.MCP_SERVER, identifier=str(server.id)).exists()
        )

    def test_sync_all_backfills(self):
        a = _agent(slug="a1", bu=self.bu)
        publish_card(a)
        # unpublished agent should NOT be catalogued
        _agent(slug="a2", bu=self.bu)
        _server(name="S1", bu=self.bu)  # not yet projected
        result = federation.sync_all()
        self.assertEqual(result["agents"], 1)
        self.assertEqual(result["mcp_servers"], 1)
        self.assertEqual(RegistryEntry.objects.filter(is_active=True).count(), 2)


# ── API ──────────────────────────────────────────────────────────────────────────

class RegistryApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.user = _user("viewer", role="viewer", bu=self.bu)
        self.admin = _user("admin1", role="platform_admin", bu=self.bu)
        self.agent = _agent(bu=self.bu, tools=["registry_search"])
        publish_card(self.agent, base_url="https://platform.test")
        federation.project_mcp_server(_server(bu=self.bu))

    def test_list_all_entries(self):
        self.client.force_login(self.user)
        resp = self.client.get("/api/v1/registry/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 2)

    def test_filter_by_kind(self):
        self.client.force_login(self.user)
        resp = self.client.get("/api/v1/registry/?kind=mcp_server")
        kinds = {e["kind"] for e in resp.json()["entries"]}
        self.assertEqual(kinds, {"mcp_server"})

    def test_text_search(self):
        self.client.force_login(self.user)
        resp = self.client.get(f"/api/v1/registry/?q={self.agent.name}")
        self.assertEqual(resp.json()["count"], 1)

    def test_detail_includes_card(self):
        self.client.force_login(self.user)
        entry = RegistryEntry.objects.get(kind=RegistryEntry.Kind.FIRST_PARTY_AGENT, identifier=self.agent.slug)
        resp = self.client.get(f"/api/v1/registry/{entry.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card_json", resp.json())

    def test_sync_requires_platform_admin(self):
        self.client.force_login(self.user)
        resp = self.client.post("/api/v1/registry/sync/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_sync(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/api/v1/registry/sync/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "synced")
